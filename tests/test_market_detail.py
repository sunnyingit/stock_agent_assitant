"""大盘维度 facts 投影。"""

from __future__ import annotations

from app.services.market_detail import PARTS, project_facts, slim_tab


def test_project_facts_from_summary():
    summary = {
        "today": "2026-11-08",
        "generated_at": "2026-11-08 15:00",
        "indexes": [{"name": "上证指数", "pct": 0.012}],
        "conclusion": {
            "level": "顺风",
            "tilt": "",
            "headline": "钱在进、好不好做偏强",
            "action": "可以顺着做",
            "gate_text": "成交不冷清",
            "signals": [
                {"key": "money", "name": "钱", "label": "在进", "text": "宽基净流入"},
                {"key": "team", "name": "国家队", "label": "没托市", "text": "没有集中申购"},
                {"key": "mood", "name": "好不好做", "label": "偏强", "text": "多数股票上涨"},
            ],
        },
        "money": {"label": "在进", "summary": "宽基净流入且成交不冷清"},
        "team": {"label": "没托市", "summary": "没有集中申购"},
        "mood": {"label": "偏强", "summary": "多数股票上涨"},
        "sectors": {"headline": "电子、通信在主升"},
    }
    facts = project_facts(summary)
    ids = {f["id"] for f in facts}
    assert "mk.conclusion.level" in ids
    assert "mk.signal.money" in ids
    assert "mk.sector.headline" in ids
    assert any("顺风" in f["text"] for f in facts)


def test_slim_tab_fund_from_sections():
    tab = {
        "title": "钱有没有进来",
        "label": "在进",
        "summary": "宽基净流入",
        "sections": [{
            "title": "三个数",
            "blocks": [
                {"key": "turnover", "title": "成交额", "judge": "活跃", "big": "1.2 万亿", "story": "比平时热"},
            ],
        }],
    }
    facts, blocks, text = slim_tab("fund", tab)["facts"], slim_tab("fund", tab)["blocks"], slim_tab("fund", tab)["text"]
    assert facts[0]["id"] == "mk.fund.summary"
    assert any(b.get("title") == "成交额" for b in blocks)
    assert "宽基净流入" in text


def test_parts_cover_market_tabs():
    assert PARTS == ("fund", "team", "sector", "mood", "cal", "macro")
