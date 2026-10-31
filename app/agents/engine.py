"""用 JSON 配置拼 LangGraph：取数、整理、多方辩论、主 Agent 总结。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Annotated, Any, Callable

from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from app import config
from app.agents.fetchers import resolve_fetch
from app.agents.llm import ModelError, complete_json, get_chat_model
from app.agents.schema import apply_schema, empty_object, filter_citations
from app.agents.spec import compile_spec
from app.tools import cache
from app.tools.stock import Stock

_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_CONFIG_DIR = Path(__file__).resolve().parent / "configs"


def merge_dicts(left, right):
    merged = dict(left or {})
    merged.update(right or {})
    return merged


class AgentState(TypedDict, total=False):
    material: list | dict
    facts: list
    sides: Annotated[dict, merge_dicts]
    summary: dict


_FOLLOWUP = (
    "\n这是后续轮次。previous 是其他分析员上一轮的论点。"
    "只回应这些论点，不要新增事实，引用规则与第一轮相同。"
)


def load_spec(name: str) -> dict:
    if not _NAME.match(name or ""):
        raise ModelError(f"非法 Agent 名称: {name}")
    path = _CONFIG_DIR / f"{name}.json"
    if not path.exists():
        raise ModelError(f"未找到 Agent 配置: {name}")
    return json.loads(path.read_text(encoding="utf-8"))


def _dump(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _lookup(data: dict, path: str):
    current = data
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _messages(spec: dict) -> dict:
    return spec.get("messages") or {}


def map_material(payload: dict, spec: dict):
    input_spec = spec.get("input") or {}
    items_key = input_spec.get("items_key")
    if not items_key:
        return payload if isinstance(payload, dict) else {}
    rows = payload.get(items_key) or []
    limit = input_spec.get("limit") or len(rows)
    require = input_spec.get("require")
    prefix = input_spec.get("id_prefix") or "N"
    mapped = []
    for item in rows[:limit]:
        if not isinstance(item, dict):
            continue
        row = {}
        for dest, sources in (input_spec.get("fields") or {}).items():
            row[dest] = ""
            for source in sources:
                value = item.get(source)
                if value not in (None, ""):
                    row[dest] = value
                    break
        if require and not str(row.get(require) or "").strip():
            continue
        row["id"] = f"{prefix}{len(mapped) + 1}"
        mapped.append(row)
    return mapped


def _context_fields(spec: dict, key: str, default: list[str]) -> list[str]:
    return (spec.get("context") or {}).get(key) or default


def project_rows(rows, fields: list[str] | None):
    if not isinstance(rows, list):
        return rows
    projected = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        allowed = fields or list(row)
        item = {}
        for field in allowed:
            value = row.get(field)
            if value not in (None, "", [], {}):
                item[field] = value
        if item:
            projected.append(item)
    return projected


def project_previous(side: dict, fields: list[str]) -> dict:
    points = []
    for point in (side or {}).get("points") or []:
        if not isinstance(point, dict):
            continue
        item = {}
        for field in fields:
            value = point.get(field)
            if value not in (None, "", [], {}):
                item[field] = value
        if item:
            points.append(item)
    return {"points": points}


def bind_article_ids(facts: list[dict], material) -> list[dict]:
    """事实里指向材料编号的字段，对不上就清空。新闻用 article_id，财报用 row_id。"""
    if not isinstance(material, list):
        return facts
    valid = {row.get("id") for row in material if isinstance(row, dict)}
    for fact in facts:
        for key, value in list(fact.items()):
            if key.endswith("_id") and key != "id" and value not in valid:
                fact[key] = ""
    return facts


def _has_material(material) -> bool:
    if isinstance(material, list):
        return bool(material)
    if isinstance(material, dict):
        return bool(material)
    return material not in (None, "")


def _empty_side(spec: dict, note: str) -> dict:
    side = empty_object(spec["side_schema"])
    if "sufficient" in side:
        side["sufficient"] = False
    if "note" in side:
        side["note"] = note
    if "points" in side:
        side["points"] = []
    return side


def _empty_summary(spec: dict, note: str) -> dict:
    summary = empty_object(spec["chief"]["schema"])
    field = (spec.get("messages") or {}).get("summary_field") or "gaps"
    if isinstance(summary.get(field), list):
        summary[field] = [note]
    return summary


def _fill_sides(spec: dict, note: str) -> dict:
    return {side["id"]: _empty_side(spec, note) for side in spec.get("sides") or []}


def _normalize_side(raw: dict, spec: dict, side_spec: dict, facts: list[dict]) -> dict:
    schema = json.loads(json.dumps(spec["side_schema"]))
    max_points = side_spec.get("max_points")
    points_schema = schema.get("fields", {}).get("points")
    if max_points and isinstance(points_schema, dict):
        points_schema["max"] = max_points
    side = apply_schema(raw, schema)
    cite = side_spec.get("cite")
    if cite:
        valid_ids = {fact.get(cite.get("id_field", "id")) for fact in facts}
        side = filter_citations(side, valid_ids, cite, max_points or 0, _messages(spec))
    elif max_points:
        side["points"] = (side.get("points") or [])[:max_points]
    return side


def build_graph(
    spec: dict,
    model,
    fetch: Callable[[], dict],
    stock_name: str,
    extra: dict | None = None,
    after_extract: Callable[[dict], None] | None = None,
):
    rounds = int(spec.get("debate_rounds") or 0)
    sides = spec.get("sides") or []
    messages = _messages(spec)
    extra = extra or {}

    def fetch_node(_state: AgentState) -> dict:
        return {"material": map_material(fetch(), spec)}

    def route_after_fetch(state: AgentState) -> str:
        return "extract" if _has_material(state.get("material")) else "empty"

    def extract_node(state: AgentState) -> dict:
        role = spec["extract"]
        payload_key = (spec.get("input") or {}).get("payload_key") or "material"
        material = state.get("material")
        visible = project_rows(material, _context_fields(spec, "extract", []))
        try:
            raw = complete_json(
                model,
                role["prompt"],
                _dump({"stock_name": stock_name, payload_key: visible}),
                temperature=role.get("temperature", 0),
            )
            facts = apply_schema(raw, role["schema"]).get(role.get("output_key") or "facts") or []
            facts = bind_article_ids(facts, material)
            if after_extract:
                after_extract({"facts": facts, "material": material})
        except Exception as exc:
            raise ModelError(f"{role.get('name') or '整理'}失败: {exc}") from exc
        return {"facts": facts}

    def make_side_node(side_spec: dict, round_index: int):
        side_id = side_spec["id"]

        def node(state: AgentState) -> dict:
            facts = state.get("facts") or []
            if not facts:
                result = _empty_side(spec, messages.get("no_facts") or "没有可引用的事实")
            else:
                payload = {
                    "stock_name": stock_name,
                    "facts": project_rows(facts, _context_fields(spec, "debate", ["id"])),
                }
                prompt = side_spec["prompt"]
                if round_index > 1:
                    prompt = prompt.rstrip() + _FOLLOWUP
                    payload["previous"] = {
                        other["id"]: project_previous(
                            (state.get("sides") or {}).get(other["id"]) or {},
                            _context_fields(spec, "previous", ["fact_ids", "claim"]),
                        )
                        for other in sides
                        if other["id"] != side_id
                    }
                try:
                    raw = complete_json(
                        model,
                        prompt,
                        _dump(payload),
                        temperature=side_spec.get("temperature", 0.3),
                    )
                    result = _normalize_side(raw, spec, side_spec, facts)
                except Exception as exc:
                    raise ModelError(f"{side_spec.get('name') or side_id}失败: {exc}") from exc
            return {"sides": {side_id: result}}

        return node

    def chief_node(state: AgentState) -> dict:
        role = spec["chief"]
        facts = state.get("facts") or []
        if not facts:
            return {"summary": _empty_summary(spec, messages.get("no_facts") or "没有可引用的事实")}
        chief_extra = dict(extra)
        if after_extract:
            chief_extra.update(getattr(after_extract, "chief_extra", {}) or {})
        payload = {"stock_name": stock_name, "facts": facts, "today": chief_extra}
        material = state.get("material")
        if isinstance(material, list):
            payload["records"] = material
            payload["articles"] = [
                {
                    key: row.get(key)
                    for key in ("id", "title", "source", "time", "url")
                    if row.get(key)
                }
                for row in material
            ]
        current = state.get("sides") or {}
        for side in sides:
            payload[side["id"]] = current.get(side["id"]) or _empty_side(spec, "没有论点")
        try:
            raw = complete_json(
                model,
                role["prompt"],
                _dump(payload),
                temperature=role.get("temperature", 0),
            )
            summary = apply_schema(raw, role["schema"])
        except Exception as exc:
            raise ModelError(f"{role.get('name') or '总结'}失败: {exc}") from exc
        return {"summary": summary}

    def empty_node(_state: AgentState) -> dict:
        note = messages.get("no_material") or "没有可分析的材料"
        return {
            "facts": [],
            "sides": _fill_sides(spec, note),
            "summary": _empty_summary(spec, note),
        }

    graph = StateGraph(AgentState)
    graph.add_node("fetch", fetch_node)
    graph.add_node("extract", extract_node)
    graph.add_node("chief", chief_node)
    graph.add_node("empty", empty_node)
    graph.add_edge(START, "fetch")
    graph.add_conditional_edges("fetch", route_after_fetch, {"extract": "extract", "empty": "empty"})
    graph.add_edge("empty", END)

    if rounds > 0 and sides:
        previous = []
        for round_index in range(1, rounds + 1):
            current = []
            for side in sides:
                node_name = f"{side['id']}_{round_index}"
                graph.add_node(node_name, make_side_node(side, round_index))
                current.append(node_name)
                if previous:
                    graph.add_edge(previous, node_name)
                else:
                    graph.add_edge("extract", node_name)
            previous = current
        graph.add_edge(previous, "chief")
    else:
        graph.add_edge("extract", "chief")
    graph.add_edge("chief", END)
    return graph.compile()


def public_result(state: dict, spec: dict) -> dict:
    result = {}
    note = (_messages(spec).get("no_material")) or "没有可分析的材料"
    for key, path in (spec.get("result") or {}).items():
        value = _lookup(state, path)
        if value is None and path.startswith("sides."):
            value = _empty_side(spec, note)
        if value is None and path == "summary":
            value = _empty_summary(spec, note)
        if value is None and path == "facts":
            value = []
        result[key] = value
    return result


def _default_fetch(spec: dict, stock: Stock) -> Callable[[], dict]:
    return resolve_fetch((spec.get("input") or {}).get("fetcher"), stock)


def run_agent(
    name: str,
    stock: Stock,
    model: Any = None,
    fetch: Callable[[], dict] | None = None,
    spec: dict | None = None,
    extra: dict | None = None,
    after_extract: Callable[[dict], None] | None = None,
) -> dict:
    spec = compile_spec(spec or load_spec(name))
    injected = model is not None or fetch is not None
    extra_key = ""
    if extra:
        extra_key = ":" + json.dumps(extra, ensure_ascii=False, sort_keys=True)
    cache_key = f"{spec.get('cache_prefix') or name}:analysis:{stock.ts_code}{extra_key}"
    ttl_name = spec.get("cache_ttl") or "analysis"
    if not injected:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
    if fetch is None:
        fetch = _default_fetch(spec, stock)
    graph = build_graph(
        spec,
        model or get_chat_model(),
        fetch,
        stock.name,
        extra=extra,
        after_extract=after_extract,
    )
    result = public_result(graph.invoke({}), spec)
    if not injected:
        cache.set(cache_key, result, config.CACHE_TTL[ttl_name])
    return result
