"""把可读的 Agent JSON 编译成引擎使用的内部结构。"""

from __future__ import annotations

import json


def compile_spec(raw: dict) -> dict:
    if "extract" in raw and "chief" in raw:
        return raw
    debate = raw.get("debate") or {}
    roles = debate.get("roles") or []
    max_points = debate.get("max_points") or 3
    cite_field = debate.get("cite") or ""
    point_fields = debate.get("point") or {}
    fact_fields = (raw.get("facts") or {}).get("fields") or {}
    summary_fields = (raw.get("summary") or {}).get("fields") or {}
    read = raw.get("read") or {}
    read_fields = read.get("fields") or {}
    context = raw.get("context") or {}
    record_name = raw.get("records") or "records"
    must = (raw.get("facts") or {}).get("must") or _first_string_field(fact_fields)
    extract_fields = context.get("facts") or ["id", *read_fields]
    debate_fields = context.get("debate") or (["id", must] if must else ["id"])
    previous_fields = context.get("previous") or _previous_fields(cite_field, point_fields)

    cite = None
    if cite_field:
        cite = {"from": "facts", "id_field": "id", "point_field": cite_field}

    side_prompt_extra = _output_line({
        "sufficient": True,
        "note": "",
        "points": [_sample(point_fields)],
    })
    side_prompt_extra += f"\n最多 {max_points} 条。"
    if cite_field:
        side_prompt_extra += f"\n{cite_field} 必须来自事实编号。"

    summary_field = raw.get("empty_field") or ("gaps" if "gaps" in summary_fields else next(iter(summary_fields), "gaps"))
    return {
        "cache_prefix": raw.get("cache_prefix"),
        "cache_ttl": raw.get("cache_ttl") or "analysis",
        "debate_rounds": int(raw.get("rounds") or 0),
        "context": {
            "extract": extract_fields,
            "debate": debate_fields,
            "previous": previous_fields,
        },
        "messages": {
            "no_material": raw.get("when_empty") or "没有可分析的材料",
            "no_facts": raw.get("when_no_facts") or "没有可引用的事实",
            "no_citation": raw.get("when_no_citation") or "论点没有对应到事实清单",
            "insufficient": raw.get("when_insufficient") or "材料不足以形成观点",
            "summary_field": summary_field,
        },
        "input": {
            "fetcher": raw.get("source"),
            "items_key": read.get("list"),
            "limit": raw.get("take"),
            "require": read.get("require"),
            "id_prefix": read.get("id_prefix") or "N",
            "payload_key": record_name,
            "fields": {
                name: [source] if isinstance(source, str) else list(source)
                for name, source in read_fields.items()
            },
        },
        "extract": {
            "name": (raw.get("facts") or {}).get("name") or "整理",
            "temperature": (raw.get("facts") or {}).get("temperature", 0),
            "output_key": "facts",
            "prompt": _join((raw.get("facts") or {}).get("prompt"), _facts_extra(fact_fields, (raw.get("facts") or {}).get("max") or 8)),
            "schema": {
                "type": "object",
                "fields": {
                    "facts": {
                        "type": "array",
                        "max": (raw.get("facts") or {}).get("max") or 8,
                        "require_field": must,
                        "id_prefix": "F",
                        "item": {"type": "object", "fields": fact_fields},
                    }
                },
            },
        },
        "side_schema": {
            "type": "object",
            "fields": {
                "sufficient": "boolean",
                "note": "string",
                "points": {
                    "type": "array",
                    "max": max_points,
                    "item": {"type": "object", "fields": point_fields},
                },
            },
        },
        "sides": [
            {
                "id": role["id"],
                "name": role.get("name") or role["id"],
                "temperature": role.get("temperature", 0.3),
                "max_points": role.get("max_points") or max_points,
                "cite": cite,
                "prompt": _join(role.get("prompt"), side_prompt_extra),
            }
            for role in roles
        ],
        "chief": {
            "name": (raw.get("summary") or {}).get("name") or "总结",
            "temperature": (raw.get("summary") or {}).get("temperature", 0),
            "prompt": _join((raw.get("summary") or {}).get("prompt"), _output_line(_sample_display(summary_fields))),
            "schema": {"type": "object", "fields": _compile_fields(summary_fields)},
        },
        "result": {
            record_name: "material",
            "facts": "facts",
            "summary": "summary",
            **{role["id"]: f"sides.{role['id']}" for role in roles},
        },
    }


def _first_string_field(fields: dict) -> str | None:
    for name, kind in fields.items():
        if kind == "string":
            return name
    return None


def _previous_fields(cite_field: str, point_fields: dict) -> list[str]:
    fields = []
    if cite_field:
        fields.append(cite_field)
    if "claim" in point_fields:
        fields.append("claim")
        return fields
    for name, kind in point_fields.items():
        if name != cite_field and kind == "string":
            fields.append(name)
            break
    return fields


def _join(prompt, extra: str) -> str:
    if isinstance(prompt, list):
        text = "\n".join(str(line) for line in prompt if str(line).strip())
    else:
        text = str(prompt or "").strip()
    if extra:
        text = f"{text}\n{extra}" if text else extra
    return text


def _compile_fields(fields: dict) -> dict:
    return {key: _compile_field(kind) for key, kind in fields.items()}


def _compile_field(kind):
    if isinstance(kind, str):
        return kind
    if isinstance(kind, dict) and "items" in kind:
        field = {
            "type": "array",
            "item": {"type": "object", "fields": _compile_fields(kind["items"])},
        }
        if kind.get("max"):
            field["max"] = kind["max"]
        return field
    if isinstance(kind, dict) and kind.get("type"):
        return kind
    if isinstance(kind, dict):
        return {"type": "object", "fields": _compile_fields(kind)}
    return "string"


def _sample_display(kind):
    if isinstance(kind, str):
        if kind == "string[]":
            return []
        if kind == "boolean":
            return True
        if kind == "number":
            return 0
        return ""
    if not isinstance(kind, dict):
        return ""
    if "items" in kind:
        return [_sample_display(kind["items"])]
    return {
        key: _sample_display(value)
        for key, value in kind.items()
        if key != "max"
    }


def _sample(fields: dict) -> dict:
    sample = {}
    for key, kind in fields.items():
        if kind == "string[]":
            sample[key] = []
        elif kind == "boolean":
            sample[key] = True
        elif kind == "number":
            sample[key] = 0
        elif isinstance(kind, dict):
            sample[key] = _sample(kind)
        else:
            sample[key] = ""
    return sample


def _output_line(example: dict) -> str:
    return "输出 JSON：" + json.dumps(example, ensure_ascii=False)


def _facts_extra(fields: dict, limit: int) -> str:
    return _output_line({"facts": [_sample(fields)]}) + f"\n最多 {limit} 条。"
