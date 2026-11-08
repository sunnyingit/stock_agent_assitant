"""有没有人跟：价格往一个方向走时，成交量、涨跌日量比、大资金是不是配合。全部由代码按数字判断，不调用模型。"""

from __future__ import annotations

from app.services.volume_flow.rules import FLOW_EDGE, FLOW_Z_EDGE, FOLLOW_MIN_MOVE, RATIO_EDGE, REL_LOUD, REL_QUIET

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
        return _plain("flat", "涨跌幅度很小，看不出谁在主导", "flat", [_short_windows(metrics)])
    move = "up" if ret > 0 else "down"
    sign = 1 if ret > 0 else -1
    # 结论仍按主窗口判；短窗对照只作依据，不参与计分
    items = [_volume(metrics), _updown(metrics, sign), _big(metrics, sign), _short_windows(metrics)]
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
    """大资金：近主窗口的净流入率，和这只股票自己过去的同长度窗口比。历史不够比时，退回数天数。"""
    daily = metrics["daily"]
    big = metrics["flow"].get("big")
    if big is None:
        return _item("big_money", "大资金", "unknown", "", "缺少大资金（超大单 + 大单）的逐日数据")
    window = len(metrics["price"]["bars"])
    net = f"近 {window} 天合计净{'流入' if big > 0 else '流出'} {_num(abs(big) / 1e8, 1)} 亿元"
    z = metrics["flow"].get("rate_z")
    if z is not None:
        if z >= FLOW_Z_EDGE:
            side, brief, text = 1, "大资金比平时明显更流入", f"{net}，比这只股票自己平时更偏流入"
        elif z <= -FLOW_Z_EDGE:
            side, brief, text = -1, "大资金比平时明显更流出", f"{net}，比这只股票自己平时更偏流出"
        else:
            return _item("big_money", "大资金", "neutral", "大资金和平时差不多", f"{net}，和这只股票自己平时差不多")
        return _item("big_money", "大资金", "agree" if side == sign else "disagree", brief, text)
    total = daily["in_days"] + daily["out_days"]
    if not total:
        return _item("big_money", "大资金", "unknown", "", "缺少大资金（超大单 + 大单）的逐日数据")
    inflow, outflow = daily["in_days"], daily["out_days"]
    net = f"，{net}"
    if inflow / total >= FLOW_EDGE and big > 0:
        side, brief, text = 1, f"大资金 {total} 天里 {inflow} 天流入", f"大资金近 {total} 天有 {inflow} 天净流入{net}（历史太短，还没法和自己平时比）"
    elif outflow / total >= FLOW_EDGE and big < 0:
        side, brief, text = -1, f"大资金 {total} 天里 {outflow} 天流出", f"大资金近 {total} 天有 {outflow} 天净流出{net}（历史太短，还没法和自己平时比）"
    else:
        return _item("big_money", "大资金", "neutral", "大资金流入流出各半", f"大资金近 {total} 天流入 {inflow} 天、流出 {outflow} 天{net}")
    return _item("big_money", "大资金", "agree" if side == sign else "disagree", brief, text)


def _short_windows(metrics: dict) -> dict:
    """主窗口外的对照：近 3 日看拐头，近 20 日看波段底色。"""
    rows = {row.get("days"): row for row in metrics.get("windows") or []}
    parts = []
    for days in (3, 20):
        row = rows.get(days)
        if not row:
            continue
        bits = []
        if row.get("ret") is not None:
            bits.append(f"涨跌 {_num(row['ret'], 1)}%")
        if row.get("rel_vol") is not None:
            bits.append(f"相对量能 {_num(row['rel_vol'], 2)} 倍")
        if row.get("net_rate") is not None:
            bits.append(f"大资金净流入率 {_num(row['net_rate'], 2)}%")
        if bits:
            parts.append(f"近 {days} 日" + "、".join(bits))
    if not parts:
        return _item("short_windows", "多窗对照", "info", "", "缺少对照窗口数据")
    return _item("short_windows", "多窗对照", "info", "多窗对照", "；".join(parts))


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
