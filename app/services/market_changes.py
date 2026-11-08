"""快照和变化对比：把每天的结论存成一小份，再和「上次看 / 昨天 / 一周前」比。纯计算，不取数。"""

from __future__ import annotations

from datetime import date, datetime, timedelta

DOWN_MARGIN = ("在降", "加速下降")
VERDICT_DETAIL = {
    "三层同向": "钱、热度、杠杆同向，信号最强",
    "有钱没热度": "钱在进，热度或杠杆还没跟上",
    "只热不增": "热度还在，但 ETF 在被卖：情绪驱动，可交易不可持有",
    "在流出": "ETF 在被赎回，增量为负",
    "没方向": "钱进出不明显，价格也没方向",
}
STAGE_DETAIL = {
    "冰点": "亏钱效应集中，追涨要谨慎",
    "修复": "跌停回落，市场在修复",
    "发酵": "追涨开始有收益，赚钱效应起来了",
    "高潮": "最热的阶段，也最容易回落",
    "退潮": "追涨开始亏钱，「好不好做」转弱",
}
SIGNALS = (("money", "① 钱"), ("team", "② 国家队"), ("mood", "③ 好不好做"))
TONE_OF_TIER = {"顺": "good", "平": "flat", "逆": "bad"}


def etf_streak(deltas: list[float]) -> int:
    """最近连续几天份额同向变化：正数连续净申购，负数连续净赎回。"""
    if not deltas or not deltas[-1]:
        return 0
    sign = 1 if deltas[-1] > 0 else -1
    count = 0
    for value in reversed(deltas):
        if value * sign > 0:
            count += 1
        else:
            break
    return sign * count


def snapshot_of(summary: dict, parts: dict) -> dict:
    con = summary["conclusion"]
    etf = parts.get("etf") or {}
    deltas = [point.get("delta") or 0.0 for point in etf.get("series") or []][1:]
    return {
        "day": summary["today"], "generated_at": summary["generated_at"],
        "level": con["level"], "tilt": con["tilt"], "gate": con["gate"], "headline": con["headline"],
        "signals": {item["key"]: {"label": item["label"], "tier": item["tier"]} for item in con["signals"]},
        "etf": {"label": etf.get("label"), "streak": etf_streak(deltas), "ratio": etf.get("ratio")},
        "margin": {"label": (parts.get("margin") or {}).get("label"), "ratio4": (parts.get("margin") or {}).get("ratio4")},
        "stage": summary["mood"].get("stage"),
        "broke_low": [row["name"] for row in summary["mood"]["indexes"] if row.get("broke_low")],
        "sectors": {row["name"]: row["verdict"] for row in summary["sectors"]["rows"]},
        "macro": {"score": summary["macro"]["score"], "tier": summary["macro"]["tier"],
                  "policy": next((row["score"] for row in summary["macro"]["rows"] if row["key"] == "policy"), None)},
        "team": (parts.get("team") or {}).get("label"),
        "watch": {row["ts_code"]: {"name": row["name"], "sector": row.get("sector"), "verdict": row.get("verdict")} for row in summary["watch"]["rows"]},
    }


def level_text(snap: dict) -> str:
    return snap["level"] + (snap["tilt"] if snap.get("tilt") else "")


def _item(kind: str, title: str, tag: str, detail: str, tone: str = "warn", tab: str = "") -> dict:
    return {"kind": kind, "title": title, "tag": tag, "detail": detail, "tone": tone, "tab": tab}


def run_start(snaps: list[dict], getter) -> str:
    """当前值从哪天开始没变过。snaps 升序，最后一个是今天。"""
    if not snaps:
        return ""
    current = getter(snaps[-1])
    start = snaps[-1]["day"]
    for snap in reversed(snaps[:-1]):
        if getter(snap) != current:
            break
        start = snap["day"]
    return start


def diff(prev: dict, cur: dict, snaps: list[dict] | None = None) -> list[dict]:
    """prev → cur 的变化。snaps（升序，含 cur）用来算「从哪天起」。"""
    snaps = snaps or [cur]
    out: list[dict] = []

    def since(getter) -> str:
        return run_start(snaps, getter)[5:]

    if (prev["level"], prev.get("tilt")) != (cur["level"], cur.get("tilt")):
        out.append(_item("level", f"结论由 {level_text(prev)} 变为 {level_text(cur)}", since(level_text) + " 起", cur["headline"],
                         {"顺风": "good", "逆风": "bad"}.get(cur["level"], "warn")))
    if prev.get("gate") != cur.get("gate"):
        out.append(_item("gate", f"闸门：{prev.get('gate')} → {cur.get('gate')}", "今天", "「钱」的闸门变了，决定能不能加仓", "warn", "fund"))
    for key, name in SIGNALS:
        old, new = (prev["signals"].get(key) or {}), (cur["signals"].get(key) or {})
        if old.get("label") != new.get("label") and new.get("label") and old.get("label"):
            tone = TONE_OF_TIER.get(new.get("tier"), "flat")
            out.append(_item("signal", f"{name}：{old['label']} → {new['label']}", since(lambda s, k=key: (s["signals"].get(k) or {}).get("label")) + " 起",
                             "信号变化会改变总档位", tone, {"money": "fund", "team": "team", "mood": "mood"}[key]))
    for name, verdict in cur["sectors"].items():
        old = prev["sectors"].get(name)
        # 「没方向」和别的状态来回切换是常态，不提醒；只有变成或离开「三层同向」才值得说
        quiet = "没方向" in (old, verdict) and "三层同向" not in (old, verdict)
        if old and old != verdict and not quiet:
            out.append(_item("sector", f"{name}：{old} → {verdict}", since(lambda s, n=name: s["sectors"].get(n)) + " 起", VERDICT_DETAIL.get(verdict, ""),
                             {"三层同向": "good", "有钱没热度": "good", "在流出": "bad"}.get(verdict, "warn"), "sector"))
    if cur.get("stage") and prev.get("stage") and cur["stage"] != prev["stage"]:
        out.append(_item("mood", f"短线情绪：{prev['stage']} → {cur['stage']}", since(lambda s: s.get("stage")) + " 起", STAGE_DETAIL.get(cur["stage"], ""), "warn", "mood"))
    new_broken = [name for name in cur.get("broke_low") or [] if name not in (prev.get("broke_low") or [])]
    if new_broken:
        out.append(_item("index", "、".join(new_broken) + "跌破前低", "第 1 天", "连续几天都在前低下面，再加上两融加速减少，结论会变差", "bad", "mood"))
    if prev["margin"].get("label") in DOWN_MARGIN and cur["margin"].get("label") not in DOWN_MARGIN and cur["margin"].get("label"):
        out.append(_item("margin_turn", "两融止跌回升", "今天", f"30 日变化回到 {cur['margin'].get('label')}，借钱炒股的人不再减少", "good", "fund"))
    if prev.get("team") != cur.get("team") and cur.get("team") and prev.get("team"):
        out.append(_item("team", f"国家队：{prev['team']} → {cur['team']}", "今天", "只看合并后的宽基份额，不确认是谁", {"在出手": "good", "托市中": "good", "异动中": "warn", "可能在撤": "bad", "已表态": "warn"}.get(cur["team"], "flat"), "team"))
    if prev["macro"].get("policy") != cur["macro"].get("policy"):
        out.append(_item("macro", f"政策分 {prev['macro'].get('policy')} → {cur['macro'].get('policy')}", "今天", "新闻里出现或不再出现降准降息、汇金增持的官方标题", "warn", "macro"))
    for code, row in cur["watch"].items():
        old = (prev["watch"].get(code) or {}).get("verdict")
        if old and row.get("verdict") and old != row["verdict"]:
            out.append(_item("watch_sector", f"{row['name']} 所在板块 {row.get('sector') or ''}：{old} → {row['verdict']}", "自选", VERDICT_DETAIL.get(row["verdict"], ""), "warn", "sector"))
    return out


def state_items(cur: dict, snaps: list[dict], n: int = 5) -> list[dict]:
    """不靠前后比较、当前就值得看的状态：ETF 连续同向几天、指数在前低下面。"""
    out = []
    streak = cur["etf"].get("streak") or 0
    if abs(streak) >= 3:
        word = "净申购" if streak > 0 else "被赎回"
        left = max(0, n - abs(streak))
        out.append(_item("etf_streak", f"宽基 ETF 连续 {abs(streak)} 天{word}", f"第 {abs(streak)} 天",
                         f"再连续 {left} 天会提醒你" if left else f"已经满 {n} 天", "good" if streak > 0 else "warn", "fund"))
    for name in cur.get("broke_low") or []:
        days = 0
        for snap in reversed(snaps):
            if name in (snap.get("broke_low") or []):
                days += 1
            else:
                break
        out.append(_item("index", f"{name}在前低下面", f"第 {days} 天", "连续 3 天、同时两融加速减少，结论会变差", "bad", "mood"))
    return out


def baseline(mode: str, snaps: list[dict], since_ts: float | None, today: str) -> dict | None:
    """snaps 升序，最后一个是今天。返回要拿来比的那一份快照。"""
    earlier = [snap for snap in snaps if snap["day"] < today]
    if not earlier:
        return None
    if mode == "today":
        return earlier[-1]
    if mode == "week":
        edge = (date.fromisoformat(today) - timedelta(days=7)).isoformat()
        old = [snap for snap in earlier if snap["day"] <= edge]
        return old[-1] if old else earlier[0]
    if since_ts:
        seen = datetime.fromtimestamp(since_ts).date().isoformat()
        old = [snap for snap in earlier if snap["day"] <= seen]
        return old[-1] if old else earlier[0]
    return earlier[-1]


def view(mode: str, snaps: list[dict], since_ts: float | None = None, n: int = 5) -> dict:
    """三种视角：last 自上次看、today 今天、week 近一周。"""
    cur = snaps[-1]
    base = baseline(mode, snaps, since_ts, cur["day"])
    label = {"last": "自你上次看以来", "today": "和上一个交易日比", "week": "近一周"}[mode]
    if base is None:
        return {"mode": mode, "label": label, "base_day": None, "items": [], "headline": "",
                "empty": "快照从今天开始积累，等有了前一个交易日的快照才能比。"}
    items = diff(base, cur, snaps)
    kinds = {item["kind"] for item in items}
    items += [item for item in state_items(cur, snaps, n) if item["kind"] not in kinds or item["kind"] == "etf_streak"]
    same = (base["level"], base.get("tilt")) == (cur["level"], cur.get("tilt"))
    start = run_start(snaps, level_text)
    headline = (f"结论没变，还是{level_text(cur)}（{start[5:]} 起）。" if same else f"结论变了：{level_text(base)} → {level_text(cur)}。")
    return {"mode": mode, "label": label, "base_day": base["day"], "headline": headline, "items": items,
            "since": start, "empty": "" if items else "这段时间没有值得说的变化。"}


def history(snaps: list[dict]) -> list[dict]:
    """历次结论：连续相同的档位合成一行，新的在前。"""
    rows: list[dict] = []
    for snap in snaps:
        if rows and rows[-1]["text"] == level_text(snap):
            rows[-1]["until"] = snap["day"]
            rows[-1]["days"] += 1
            continue
        rows.append({"text": level_text(snap), "level": snap["level"], "tilt": snap.get("tilt"), "since": snap["day"], "until": snap["day"], "days": 1, "headline": snap["headline"]})
    return list(reversed(rows))
