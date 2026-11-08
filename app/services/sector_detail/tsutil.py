"""板块取数共用的小工具：容错调用、DataFrame 转记录、日期和数字转换、成分行解析。"""

from __future__ import annotations


def call(fn, required: bool = False):
    try:
        return fn()
    except Exception:
        if required:
            raise
        return None


def records(frame) -> list[dict]:
    if frame is None:
        return []
    empty = getattr(frame, "empty", None)
    if empty is True:
        return []
    if hasattr(frame, "to_dict"):
        return frame.to_dict("records")
    return []


def first(frame) -> dict:
    rows = records(frame)
    return rows[0] if rows else {}


def iso(value) -> str | None:
    text = str(value or "")
    if len(text) < 8:
        return None
    text = text[:8]
    if not text.isdigit():
        return None
    return f"{text[:4]}-{text[4:6]}-{text[6:8]}"


def num(value):
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def ret(value):
    number = num(value)
    if number is None:
        return None
    return number / 100.0


def si(value) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text if text.endswith(".SI") else f"{text}.SI"


def one_member(row: dict) -> dict | None:
    code = ""
    for key in ("con_code", "ts_code"):
        text = str(row.get(key) or "")
        if text.endswith((".SH", ".SZ", ".BJ")):
            code = text
            break
    if not code:
        return None
    name = str(row.get("con_name") or row.get("name") or code)
    return {"ts_code": code, "name": name}


def member_rows(frame) -> list[dict]:
    rows = []
    seen = set()
    for row in records(frame):
        item = one_member(row)
        if item is None or item["ts_code"] in seen:
            continue
        seen.add(item["ts_code"])
        rows.append(item)
    return rows
