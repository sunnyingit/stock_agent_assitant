"""结论那一句：按已经算好的数字用模板拼。AI 解读在 app/flow_review。

诊断 / 定时任务：不超过 DIAGNOSIS_MAX_CHARS（50）字。
用户 reply 场景不走这里（见 flow_reply）。
"""

from __future__ import annotations

from app.services.volume_flow.rules import (
    DIAGNOSIS_MAX_CHARS,
    PATH_TEXT,
    PRICE_FLOW_TEXT,
    STATE_TEXT,
    _PATTERNS,
)


def write_text(metrics: dict, verdict: dict, follow: dict | None = None) -> str:
    """结论那一句。只用模板，不调用模型。诊断长度上限 50 字。"""
    if not verdict.get("lean"):
        return "数据已停在更早的交易日，这次不判断方向。"[:DIAGNOSIS_MAX_CHARS]
    return _template(metrics, verdict, follow or {})


def _template(metrics: dict, verdict: dict, follow: dict) -> str:
    path = verdict.get("path") or {}
    path_text = path.get("text") or PATH_TEXT.get(verdict.get("trend") or "", "")
    state = verdict.get("flow_state") or ""
    state_text = STATE_TEXT.get(state, "")
    structure = (verdict.get("structure") or {}).get("text") or ""
    if structure.startswith("加权后"):
        structure = structure[3:]
    relation = PRICE_FLOW_TEXT.get(verdict.get("price_flow") or "", "")
    bits = [bit for bit in (path_text, state_text, structure, relation) if bit]
    if bits:
        sentence = "；".join(bits)
    else:
        # 降级：旧句式
        window = len(metrics["price"]["bars"])
        head = f"近 {window} 天{_move(metrics['price'].get('ret'))}"
        label = _PATTERNS.get(verdict.get("pattern") or "")
        parts = [head]
        if label and verdict.get("pattern") != "none":
            parts.append(label)
        parts.append(_relation(metrics["relative"]))
        sentence = "，".join(parts)
    return sentence[:DIAGNOSIS_MAX_CHARS]


def _move(ret) -> str:
    if ret is None:
        return "涨跌还没法算"
    if ret > 0:
        return f"涨了 {_num(ret)}%"
    if ret < 0:
        return f"跌了 {_num(abs(ret))}%"
    return "价格基本没变"


def _relation(relative: dict) -> str:
    stock = (relative.get("stock") or {}).get("ret")
    sector = relative.get("sector") or {}
    sector_ret = sector.get("ret")
    name = sector.get("name") or "板块"
    if stock is None or sector_ret is None:
        return f"和{name}还没法比"
    gap = stock - sector_ret
    if gap > 0.5:
        return f"跑赢{name}"
    if gap < -0.5:
        return f"跑输{name}"
    return f"与{name}基本持平"


def _num(value, digits: int = 1) -> str:
    text = f"{round(float(value), digits):.{digits}f}"
    return text.rstrip("0").rstrip(".")
