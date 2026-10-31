"""美股、韩股近期日K。"""

from __future__ import annotations

from app import config
from app.tools.http import get_json

def recent_bars(symbol: str, limit: int = 5) -> list[dict]:
    payload = get_json(
        config.KLINE_URL,
        {"param": f"{symbol},day,,,{limit},qfq"},
        headers={"Referer": "https://gu.qq.com/"},
    )
    node = ((payload or {}).get("data") or {}).get(symbol) or {}
    rows = node.get("qfqday") or node.get("day") or []
    bars = []
    for row in rows:
        if len(row) < 5:
            continue
        bars.append({
            "date": row[0],
            "open": float(row[1]),
            "close": float(row[2]),
            "high": float(row[3]),
            "low": float(row[4]),
            "volume": float(row[5]) if len(row) > 5 and row[5] != "" else None,
        })
    return bars


def trend_of(symbol: str, name: str, limit: int = 5) -> dict:
    try:
        bars = recent_bars(symbol, limit)
    except Exception as exc:
        return {"symbol": symbol, "name": name, "error": str(exc), "bars": []}
    change_pct = None
    if len(bars) >= 2 and bars[0]["close"] and bars[-1]["close"]:
        change_pct = round((bars[-1]["close"] - bars[0]["close"]) / bars[0]["close"] * 100, 2)
    return {
        "symbol": symbol,
        "name": name,
        "change_pct": change_pct,
        "bars": bars,
    }
