"""大盘维度：把市场页 summary / tab 投影成分析师可用的 facts 与追问分片。

不替代 market.html；不编造国家队申购金额。数字只来自 market_page。
"""

from __future__ import annotations

from typing import Any

from app.services import market_page

PARTS = ("fund", "team", "sector", "mood", "cal", "macro")


def build_detail(stock: Any | None = None) -> dict:
    summary = market_page.build_summary()
    facts = project_facts(summary)
    concl = summary.get("conclusion") if isinstance(summary.get("conclusion"), dict) else {}
    sectors = summary.get("sectors") if isinstance(summary.get("sectors"), dict) else {}
    return {
        "ts_code": getattr(stock, "ts_code", "") or "",
        "stock_name": getattr(stock, "name", "") or "",
        "dimension": "market",
        "status": "ok",
        "review_state": "ok",
        "meta": {
            "as_of": summary.get("generated_at") or summary.get("today") or "",
            "state": "ok",
            "today": summary.get("today") or "",
        },
        "review": {
            "as_of": summary.get("today") or "",
            "facts": facts,
            "items": [],
            "missing": [],
        },
        "conclusion": {
            "level": concl.get("level") or "",
            "tilt": concl.get("tilt") or "",
            "headline": concl.get("headline") or "",
            "action": concl.get("action") or "",
            "note": concl.get("note") or "",
            "gate_text": concl.get("gate_text") or "",
        },
        "indexes": summary.get("indexes") or [],
        "sectors": {
            "headline": sectors.get("headline") or "",
            "date": sectors.get("date") or "",
            "buckets": [
                {"name": b.get("name"), "count": b.get("count")}
                for b in (sectors.get("buckets") or [])
                if isinstance(b, dict)
            ][:8],
        },
    }


def failed_payload(stock: Any | None, error: str) -> dict:
    return {
        "ts_code": getattr(stock, "ts_code", "") or "",
        "stock_name": getattr(stock, "name", "") or "",
        "dimension": "market",
        "status": "error",
        "review_state": "unavailable",
        "review_error": error,
        "meta": {"state": "error"},
        "review": {"facts": [], "items": [], "missing": [], "as_of": ""},
    }


def build_reply(part: str) -> dict:
    if part not in PARTS:
        raise ValueError("part 必须是 " + "、".join(PARTS))
    tab = market_page.build_tab(part)
    return slim_tab(part, tab if isinstance(tab, dict) else {})


def project_facts(summary: dict) -> list[dict]:
    facts: list[dict] = []
    concl = summary.get("conclusion") if isinstance(summary.get("conclusion"), dict) else {}
    level = str(concl.get("level") or "")
    tilt = str(concl.get("tilt") or "")
    level_text = level + (f"（{tilt}）" if tilt else "")
    if level_text:
        facts.append(_fact("mk.conclusion.level", "市场总档", "结论", level_text))
    if concl.get("headline"):
        facts.append(_fact("mk.conclusion.headline", "结论要点", "结论", str(concl["headline"])))
    if concl.get("action"):
        facts.append(_fact("mk.conclusion.action", "怎么做", "结论", str(concl["action"])))
    if concl.get("gate_text"):
        facts.append(_fact("mk.conclusion.gate", "钱的门槛", "结论", str(concl["gate_text"])))

    for sig in concl.get("signals") or []:
        if not isinstance(sig, dict):
            continue
        key = str(sig.get("key") or "")
        if not key:
            continue
        label = str(sig.get("name") or key)
        bits = [str(sig.get("label") or ""), str(sig.get("text") or "")]
        text = "：".join(bit for bit in bits if bit)
        if not text:
            continue
        facts.append(_fact(f"mk.signal.{key}", label, "信号", text))

    indexes = summary.get("indexes") or []
    if isinstance(indexes, list) and indexes:
        bits = []
        for row in indexes[:4]:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "")
            pct = row.get("pct")
            if not name:
                continue
            if pct is None:
                bits.append(name)
            else:
                bits.append(f"{name} {float(pct) * 100:+.1f}%".replace("+", "+").replace("-", "−"))
        if bits:
            facts.append(_fact("mk.index.tape", "主要指数", "指数", "；".join(bits)))

    money = summary.get("money") if isinstance(summary.get("money"), dict) else {}
    if money.get("summary"):
        facts.append(_fact(
            "mk.money.summary",
            "市场资金",
            "资金",
            f"{money.get('label') or ''}：{money['summary']}".strip("："),
        ))

    team = summary.get("team") if isinstance(summary.get("team"), dict) else {}
    team_text = team.get("summary") or team.get("text") or team.get("headline")
    if team_text:
        facts.append(_fact(
            "mk.team.summary",
            "国家队",
            "国家队",
            f"{team.get('label') or ''}：{team_text}".strip("："),
        ))
    elif team.get("pending") or team.get("ok") is False:
        facts.append(_fact("mk.team.summary", "国家队", "国家队", "国家队数据没取到，不参与总档位"))

    mood = summary.get("mood") if isinstance(summary.get("mood"), dict) else {}
    if mood.get("summary"):
        facts.append(_fact(
            "mk.mood.summary",
            "好不好做",
            "情绪",
            f"{mood.get('label') or ''}：{mood['summary']}".strip("："),
        ))

    sectors = summary.get("sectors") if isinstance(summary.get("sectors"), dict) else {}
    if sectors.get("headline"):
        facts.append(_fact("mk.sector.headline", "市场板块", "板块", str(sectors["headline"])))

    return facts


def slim_tab(part: str, tab: dict) -> dict:
    """追问用：保留人话块，去掉重图与长序列。"""
    out: dict[str, Any] = {
        "part": part,
        "dimension": "market",
        "source": "market_tab",
        "title": tab.get("title") or _PART_TITLE.get(part, part),
        "as_of": tab.get("as_of") or tab.get("date") or "",
        "facts": [],
        "blocks": [],
        "text": "",
    }
    # 各 tab 结构略有不同，尽量抽取 judge/summary/blocks
    if part == "fund":
        out["facts"], out["blocks"], out["text"] = _slim_fund(tab)
    elif part == "team":
        out["facts"], out["blocks"], out["text"] = _slim_team(tab)
    elif part == "sector":
        out["facts"], out["blocks"], out["text"] = _slim_sector(tab)
    elif part == "mood":
        out["facts"], out["blocks"], out["text"] = _slim_mood(tab)
    elif part == "cal":
        out["facts"], out["blocks"], out["text"] = _slim_cal(tab)
    else:
        out["facts"], out["blocks"], out["text"] = _slim_macro(tab)
    return out


_PART_TITLE = {
    "fund": "市场资金",
    "team": "国家队",
    "sector": "市场板块",
    "mood": "好不好做",
    "cal": "日历",
    "macro": "宏观",
}


def _fact(fid: str, label: str, group: str, text: str) -> dict:
    return {"id": fid, "label": label, "group": group, "text": text, "value": None, "unit": ""}


def _iter_blocks(tab: dict):
    for section in tab.get("sections") or []:
        if not isinstance(section, dict):
            continue
        for block in section.get("blocks") or []:
            if isinstance(block, dict):
                yield block
    for block in tab.get("blocks") or []:
        if isinstance(block, dict):
            yield block


def _slim_fund(tab: dict) -> tuple[list, list, str]:
    facts, blocks = [], []
    if tab.get("summary"):
        label = str(tab.get("label") or "")
        summary = str(tab["summary"])
        facts.append(_fact("mk.fund.summary", "市场资金摘要", "市场资金", f"{label}：{summary}".strip("：")))
    for block in _iter_blocks(tab):
        title = str(block.get("title") or block.get("key") or "")
        judge = str(block.get("judge") or "")
        big = str(block.get("big") or "")
        story = str(block.get("story") or "")
        line = "；".join(bit for bit in (title, judge, big, story) if bit)
        if not line:
            continue
        blocks.append({"title": title, "judge": judge, "big": big, "story": story})
        key = str(block.get("key") or title or len(facts))
        facts.append(_fact(f"mk.fund.{key}", title or "资金项", "市场资金", line))
    return facts, blocks[:12], "；".join(f["text"] for f in facts[:6])


def _slim_team(tab: dict) -> tuple[list, list, str]:
    facts, blocks = [], []
    if tab.get("pending"):
        facts.append(_fact("mk.team.detail", "国家队", "国家队", str(tab.get("summary") or "国家队数据没取到，不参与总档位")))
        return facts, blocks, facts[0]["text"]
    label = str(tab.get("label") or "")
    summary = str(tab.get("summary") or tab.get("lead") or "")
    if label or summary:
        facts.append(_fact("mk.team.detail", "国家队", "国家队", f"{label}：{summary}".strip("：")))
        blocks.append({"title": "国家队", "judge": label, "text": summary})
    for block in _iter_blocks(tab):
        title = str(block.get("title") or block.get("key") or "")
        judge = str(block.get("judge") or "")
        story = str(block.get("story") or "")
        line = "；".join(bit for bit in (title, judge, story) if bit)
        if not line:
            continue
        blocks.append({"title": title, "judge": judge, "story": story})
        key = str(block.get("key") or title or len(facts))
        facts.append(_fact(f"mk.team.{key}", title or "国家队项", "国家队", line))
    return facts, blocks[:10], "；".join(f["text"] for f in facts[:5])


def _slim_sector(tab: dict) -> tuple[list, list, str]:
    facts, blocks = [], []
    headline = str(tab.get("headline") or "")
    if headline:
        facts.append(_fact("mk.sector.detail_headline", "市场板块摘要", "市场板块", headline))
    for row in (tab.get("rows") or [])[:12]:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "")
        bucket = str(row.get("bucket") or "")
        phase = str(row.get("phase") or row.get("verdict") or "")
        line = "；".join(bit for bit in (name, bucket, phase) if bit)
        if not line:
            continue
        blocks.append({"name": name, "bucket": bucket, "phase": phase})
        facts.append(_fact(f"mk.sector.row.{name or len(facts)}", name or "板块", "市场板块", line))
    for bucket in (tab.get("buckets") or [])[:6]:
        if isinstance(bucket, dict) and bucket.get("name"):
            facts.append(_fact(
                f"mk.sector.bucket.{bucket['name']}",
                str(bucket["name"]),
                "市场板块",
                f"{bucket['name']} {bucket.get('count') or ''} 个".strip(),
            ))
    return facts, blocks, "；".join(f["text"] for f in facts[:8])


def _slim_mood(tab: dict) -> tuple[list, list, str]:
    facts, blocks = [], []
    label = str(tab.get("label") or "")
    summary = str(tab.get("summary") or "")
    if label or summary:
        facts.append(_fact("mk.mood.detail", "好不好做", "情绪", f"{label}：{summary}".strip("：")))
        blocks.append({"title": "好不好做", "label": label, "summary": summary})
    for block in _iter_blocks(tab):
        title = str(block.get("title") or block.get("key") or "")
        judge = str(block.get("judge") or "")
        story = str(block.get("story") or "")
        line = "；".join(bit for bit in (title, judge, story) if bit)
        if not line:
            continue
        blocks.append({"title": title, "judge": judge, "story": story})
        key = str(block.get("key") or title or len(facts))
        facts.append(_fact(f"mk.mood.{key}", title or "情绪项", "情绪", line))
    return facts, blocks[:10], "；".join(f["text"] for f in facts[:6])


def _slim_cal(tab: dict) -> tuple[list, list, str]:
    facts, blocks = [], []
    for row in (tab.get("rows") or tab.get("items") or [])[:10]:
        if not isinstance(row, dict):
            continue
        text = str(row.get("text") or row.get("title") or "")
        when = str(row.get("date") or row.get("when") or "")
        line = f"{when} {text}".strip()
        if not line:
            continue
        blocks.append({"when": when, "text": text})
        facts.append(_fact(f"mk.cal.{len(facts)}", "日历", "日历", line))
    if not facts and tab.get("summary"):
        facts.append(_fact("mk.cal.summary", "日历", "日历", str(tab["summary"])))
    return facts, blocks, "；".join(f["text"] for f in facts[:8])


def _slim_macro(tab: dict) -> tuple[list, list, str]:
    facts, blocks = [], []
    if tab.get("summary"):
        facts.append(_fact("mk.macro.summary", "宏观", "宏观", str(tab["summary"])))
    for row in (tab.get("rows") or tab.get("items") or tab.get("blocks") or [])[:10]:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or row.get("name") or row.get("k") or "")
        value = str(row.get("value") or row.get("v") or row.get("text") or row.get("big") or "")
        line = f"{title}：{value}".strip("：")
        if not line:
            continue
        blocks.append({"title": title, "value": value})
        facts.append(_fact(f"mk.macro.{len(facts)}", title or "宏观项", "宏观", line))
    return facts, blocks, "；".join(f["text"] for f in facts[:8])
