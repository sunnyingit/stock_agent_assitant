"""有没有人跟：价格往一个方向走时，成交量、涨跌日量比、大资金是不是配合。全部由代码按数字判断，不调用模型。"""

from __future__ import annotations

from app.services.volume_flow.rules import FLOW_EDGE, FOLLOW_MIN_MOVE, RATIO_EDGE, REL_LOUD, REL_QUIET

_LEVELS = {
    "up": {
        "strong": "有人跟，量价资金都配合",
        "yes": "有人跟",
        "weak": "跟的人不多",
        "mixed": "有人跟也有人不跟，信号不一致",
        "none": "没人跟，涨势缺支撑",
    },
    "down": {
        "strong": "抛压明显，量价资金都在配合下跌",
        "yes": "有人在卖",
        "weak": "抛压不大",
        "mixed": "卖压和承接都有，信号不一致",
        "none": "没人追杀，有资金在接",
    },
}


def follow(metrics: dict) -> dict:
    """返回 {level, label, brief, move, items}。level: strong/yes/weak/mixed/none/flat/unknown。
    brief 是一句话（给 verdict.text 用），items[].text 是各项证据的完整说法。"""
    ret = metrics["price"].get("ret")
    if ret is None or abs(ret) < FOLLOW_MIN_MOVE:
        return _plain("flat", "涨跌幅度很小，看不出谁在主导", "flat", [])
    move = "up" if ret > 0 else "down"
    sign = 1 if ret > 0 else -1
    items = [_volume(metrics), _updown(metrics, sign), _big(metrics, sign)]
    scored = [item for item in items if item["status"] in {"agree", "neutral", "disagree"}]
    if len(scored) < 2:
        return _plain("unknown", "资金数据不全，看不出有没有人跟", move, items)
    agree = sum(1 for item in scored if item["status"] == "agree")
    disagree = sum(1 for item in scored if item["status"] == "disagree")
    level = _level(agree, disagree, len(scored))
    label = _LEVELS[move][level]
    pros = [item for item in scored if item["status"] == "agree"]
    cons = [item for item in scored if item["status"] != "agree"]
    return {
        "level": level,
        "label": label,
        "move": move,
        "brief": _join(label, pros, cons),
        "items": items,
    }


def refine(verdict: dict, block: dict | None, metrics: dict) -> dict:
    """量能平稳（没放量也没缩量）时形态规则不判方向。这时若涨跌日量比和大资金都和价格同向，改判为低把握的偏多/偏空。"""
    if not block or verdict.get("pattern") != "none" or verdict.get("lean") != "unclear":
        return verdict
    rel = metrics["price"].get("rel_vol")
    if rel is None or not REL_QUIET <= rel <= REL_LOUD:
        return verdict
    if block["level"] not in {"yes", "strong"}:
        return verdict
    return {**verdict, "lean": "bullish" if block["move"] == "up" else "bearish", "level": 1, "certainty": "low"}


def _level(agree: int, disagree: int, total: int) -> str:
    if agree == total:
        return "strong"
    if disagree and agree:
        return "mixed"
    if disagree:
        return "none"
    if agree >= 2:
        return "yes"
    return "weak"


def _volume(metrics: dict) -> dict:
    rel = metrics["price"].get("rel_vol")
    if rel is None:
        return _item("volume", "总成交量", "unknown", "缺对比", "缺少之前 60 天的成交量，没法比")
    if rel > REL_LOUD:
        status, brief, text = "agree", "放量", f"总成交量放大到之前 60 天平均的 {_num(rel, 2)} 倍"
    elif rel < REL_QUIET:
        status, brief, text = "disagree", "缩量", f"总成交量缩到之前 60 天平均的 {_num(rel, 2)} 倍"
    else:
        status, brief, text = "neutral", "总量没放大", f"总成交量没放大（之前 60 天平均的 {_num(rel, 2)} 倍）"
    # 放量对涨跌都算配合价格方向（有人参与），缩量都算没人参与
    return _item("volume", "总成交量", status, brief, text)


def _updown(metrics: dict, sign: int) -> dict:
    ratio = metrics["price"].get("up_down_vol_ratio")
    if ratio is None:
        return _item("up_down", "涨跌日量比", "unknown", "", "窗口里没有同时出现涨日和跌日，没法比")
    if ratio >= RATIO_EDGE:
        side, brief, text = 1, "涨时量更大", f"上涨日成交量是下跌日的 {_num(ratio, 2)} 倍"
    elif ratio <= 1 / RATIO_EDGE:
        side, brief, text = -1, "跌时量更大", f"下跌日成交量是上涨日的 {_num(1 / ratio, 2)} 倍"
    else:
        return _item("up_down", "涨跌日量比", "neutral", "涨跌日量差不多", f"上涨日和下跌日成交量差不多（{_num(ratio, 2)} 倍）")
    status = "agree" if side == sign else "disagree"
    return _item("up_down", "涨跌日量比", status, brief, text)


def _big(metrics: dict, sign: int) -> dict:
    daily = metrics["daily"]
    total = daily["in_days"] + daily["out_days"]
    big = metrics["flow"].get("big")
    if not total or big is None:
        return _item("big_money", "大资金", "unknown", "", "缺少大资金（超大单 + 大单）的逐日数据")
    inflow, outflow = daily["in_days"], daily["out_days"]
    net = f"，近 {len(metrics['price']['bars'])} 天合计净{'流入' if big > 0 else '流出'} {_num(abs(big) / 1e8, 1)} 亿元"
    if inflow / total >= FLOW_EDGE and big > 0:
        side, brief, text = 1, f"大资金 {total} 天里 {inflow} 天流入", f"大资金近 {total} 天有 {inflow} 天净流入{net}"
    elif outflow / total >= FLOW_EDGE and big < 0:
        side, brief, text = -1, f"大资金 {total} 天里 {outflow} 天流出", f"大资金近 {total} 天有 {outflow} 天净流出{net}"
    else:
        return _item("big_money", "大资金", "neutral", "大资金流入流出各半", f"大资金近 {total} 天流入 {inflow} 天、流出 {outflow} 天{net}")
    status = "agree" if side == sign else "disagree"
    return _item("big_money", "大资金", status, brief, text)


def _item(key: str, name: str, status: str, brief: str, text: str) -> dict:
    return {"key": key, "name": name, "status": status, "brief": brief, "text": text}


def _join(label: str, pros: list[dict], cons: list[dict]) -> str:
    body = "，".join(item["brief"] for item in pros)
    other = "，".join(item["brief"] for item in cons)
    if body and other:
        return f"{label}：{body}；但{other}"
    if body:
        return f"{label}：{body}"
    return f"{label}：{other}" if other else label


def _plain(level: str, label: str, move: str, items: list[dict]) -> dict:
    return {"level": level, "label": label, "move": move, "brief": label, "items": items}


def _num(value, digits: int = 1) -> str:
    text = f"{round(float(value), digits):.{digits}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text
