"""板块一句话。必须有「不过」，不写买卖和关注建议。"""

from __future__ import annotations

_FORBIDDEN = ("可以关注", "买入", "卖出", "目标价")


def write_text(tag: str, gaps: dict | None, counter: str, independent: bool) -> str:
    head = "独立走势，板块结论不适用" if independent else tag
    gap = gap_sentence(gaps or {}, independent)
    tail = counter or "还没有看到反向信号"
    text = f"{head}；{gap}。不过{tail}。" if gap else f"{head}。不过{tail}。"
    if len(text) > 90:
        text = f"{head}。不过{tail}。"
    return text


def finish_text(tag: str, gaps: dict | None, counter: str, independent: bool, model=None) -> tuple[str, str]:
    fallback = write_text(tag, gaps, counter, independent)
    if not callable(model):
        return fallback, "fallback"
    try:
        raw = model({
            "tag": tag,
            "gaps": gaps or {},
            "gap_text": gap_sentence(gaps or {}, independent),
            "counter": counter,
            "independent": independent,
        })
    except Exception:
        return fallback, "fallback"
    if acceptable(raw):
        return str(raw).strip(), "model"
    return fallback, "fallback"


def acceptable(text: str | None) -> bool:
    if not text:
        return False
    cleaned = str(text).strip()
    if "不过" not in cleaned or len(cleaned) > 90:
        return False
    return not any(word in cleaned for word in _FORBIDDEN)


def gap_sentence(gaps: dict, independent: bool) -> str:
    stock_vs_index = gaps.get("stock_vs_index")
    if independent:
        return _versus(stock_vs_index, "个股", "大盘", "弱于", "强过")
    sector_bit = _versus(gaps.get("sector_vs_index"), "板块", "大盘", "跑输", "跑赢")
    stock_vs_sector = gaps.get("stock_vs_sector")
    if stock_vs_sector is None:
        stock_bit = ""
    elif abs(stock_vs_sector) < 0.005:
        stock_bit = "本股与板块接近"
    elif stock_vs_sector < 0:
        stock_bit = f"本股又比板块弱 {abs(stock_vs_sector) * 100:.1f}%"
    else:
        stock_bit = f"本股又比板块强 {stock_vs_sector * 100:.1f}%"
    if sector_bit and stock_bit:
        return f"{sector_bit}，{stock_bit}"
    return sector_bit or stock_bit


def _versus(gap: float | None, left: str, right: str, lose: str, win: str) -> str:
    if gap is None:
        return ""
    if abs(gap) < 0.005:
        return f"{left}与{right}接近"
    word = lose if gap < 0 else win
    return f"{left}近 20 日{word}{right} {abs(gap) * 100:.1f}%"
