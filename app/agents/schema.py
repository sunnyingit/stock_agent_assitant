"""按 JSON 字段说明整理模型输出。不认识的字段会被丢掉。"""

from __future__ import annotations


def _string(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _string_list(value) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def _boolean(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


def _number(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def apply_schema(value, schema):
    if isinstance(schema, str):
        return _coerce(value, schema)
    kind = schema.get("type")
    if kind == "object":
        raw = value if isinstance(value, dict) else {}
        return {
            name: apply_schema(raw.get(name), field)
            for name, field in schema.get("fields", {}).items()
        }
    if kind == "array":
        rows = value if isinstance(value, list) else []
        limit = schema.get("max")
        result = []
        for row in rows:
            item = apply_schema(row, schema.get("item", "string"))
            require = schema.get("require_field")
            if require and not _string(item.get(require) if isinstance(item, dict) else item):
                continue
            if isinstance(item, dict) and schema.get("id_prefix"):
                item[schema.get("id_field", "id")] = f"{schema['id_prefix']}{len(result) + 1}"
            result.append(item)
            if limit and len(result) >= limit:
                break
        return result
    return _coerce(value, kind or "string")


def _coerce(value, kind: str):
    if kind == "string":
        return _string(value)
    if kind == "string[]":
        return _string_list(value)
    if kind == "boolean":
        return _boolean(value)
    if kind == "number":
        return _number(value)
    return value


def empty_object(schema: dict) -> dict:
    return apply_schema({}, schema)


def filter_citations(side: dict, valid_ids: set[str], cite: dict, max_points: int, messages: dict) -> dict:
    field = cite.get("point_field", "fact_ids")
    points = []
    for point in side.get("points") or []:
        ids = [fact_id for fact_id in point.get(field) or [] if fact_id in valid_ids]
        claim = _string(point.get("claim"))
        if not ids or not claim:
            continue
        point[field] = ids
        point["claim"] = claim
        points.append(point)
        if max_points and len(points) >= max_points:
            break
    side["points"] = points
    if side.get("sufficient") and not points:
        side["sufficient"] = False
        side["note"] = _string(side.get("note")) or messages.get("no_citation") or "论点没有对应到事实清单"
    if not side.get("sufficient") and not _string(side.get("note")):
        side["note"] = messages.get("insufficient") or "材料不足以形成观点"
    return side
