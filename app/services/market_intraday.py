"""盘中同一时刻：今天、昨天、前几天平均的累计成交额放在一起比。

tushare 的分钟接口一分钟才让调一次，这里用东方财富的分钟走势（一次 5 个交易日）。
全市场成交额 = 上证指数 + 深证综指；ETF 异动看国家队 9 只合计。盘中数据只提醒，不改结论。
"""

from __future__ import annotations

import json
import time
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

from app.services.market_team import TEAM_BOOK
from app.tools import cache

_ZONE = ZoneInfo("Asia/Shanghai")
_URL = (
    "https://push2his.eastmoney.com/api/qt/stock/trends2/get?secid={secid}"
    "&fields1=f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13&fields2=f51,f52,f53,f54,f55,f56,f57,f58&iscr=0&iscca=0&ndays=5"
)
MARKET = ("1.000001", "0.399106")
TTL = 60
STALE = 3600           # 取不到时最多拿 1 小时前的
FAIL_COOLDOWN = 30     # 失败后 30 秒内不再请求
PAUSE = 0.25           # 连着请求之间的间隔
STEP = 5                 # 图上每 5 分钟取一个点
SURGE = 3.0              # 成交放大到前几天同一时刻平均的 3 倍
DROP = -0.01             # 且大盘跌超 1%


def secid_of(code: str) -> str:
    return ("1." if code.endswith(".SH") else "0.") + code[:6]


def fetch(secid: str) -> list[dict]:
    """60 秒内用缓存。东方财富短时间连着请求会断连接，断了就重试一次，还不行就用最近一次成功的数据（最多 1 小时）。"""
    key = f"market_intraday:{secid}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    if cache.get(key + ":fail") is not None:
        stale = cache.get(key + ":stale")
        if stale is not None:
            return stale
        raise RuntimeError("分钟走势刚失败过，稍后再试")
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            request = urllib.request.Request(_URL.format(secid=secid), headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"})
            with urllib.request.urlopen(request, timeout=8) as response:
                body = json.loads(response.read().decode("utf-8"))
            rows = parse(((body or {}).get("data") or {}).get("trends") or [])
            if rows:
                cache.set(key, rows, TTL)
                cache.set(key + ":stale", rows, STALE)
                time.sleep(PAUSE)
                return rows
            last_error = RuntimeError("没有分钟数据")
        except Exception as exc:
            last_error = exc
        time.sleep(0.8)
    cache.set(key + ":fail", True, FAIL_COOLDOWN)
    stale = cache.get(key + ":stale")
    if stale is not None:
        return stale
    raise last_error or RuntimeError("分钟走势没取到")


def parse(lines: list[str]) -> list[dict]:
    """每行：时间,开,收,高,低,量,额,均价。"""
    out = []
    for line in lines:
        parts = str(line).split(",")
        if len(parts) < 7 or len(parts[0]) < 16:
            continue
        try:
            out.append({"day": parts[0][:10], "hm": parts[0][11:16], "close": float(parts[2]), "amount": float(parts[6])})
        except ValueError:
            continue
    return out


def cumulate(rows: list[dict]) -> dict[str, list[tuple[str, float, float]]]:
    """{日期: [(HH:MM, 累计成交额·亿元, 收盘价)]}，同一分钟多条（几个品种）先相加。"""
    per_day: dict[str, dict[str, list[float]]] = {}
    for row in rows:
        slot = per_day.setdefault(row["day"], {}).setdefault(row["hm"], [0.0, 0.0])
        slot[0] += row["amount"] / 1e8
        slot[1] += row["close"]
    out = {}
    for day, minutes in per_day.items():
        acc, series = 0.0, []
        for hm in sorted(minutes):
            acc += minutes[hm][0]
            series.append((hm, acc, minutes[hm][1]))
        out[day] = series
    return out


def _at(series: list[tuple[str, float, float]], hm: str) -> float | None:
    value = None
    for label, cum, _close in series:
        if label > hm:
            break
        value = cum
    return value


def compare(rows: list[dict]) -> dict | None:
    days = cumulate(rows)
    order = sorted(days)
    if len(order) < 2:
        return None
    today, before = order[-1], order[:-1]
    series = days[today]
    last_hm, now_cum = series[-1][0], series[-1][1]
    yesterday = _at(days[before[-1]], last_hm)
    olds = [value for value in (_at(days[day], last_hm) for day in before) if value]
    avg = sum(olds) / len(olds) if olds else None
    points = []
    for hm, cum, _close in series:
        minute = int(hm[:2]) * 60 + int(hm[3:])
        if (minute - 570) % STEP != 0 and hm != last_hm:
            continue
        earlier = [value for value in (_at(days[day], hm) for day in before) if value]
        points.append({
            "t": hm, "today": cum, "yesterday": _at(days[before[-1]], hm),
            "avg": sum(earlier) / len(earlier) if earlier else None,
        })
    return {
        "day": today, "last": last_hm, "now": now_cum, "yesterday": yesterday, "avg": avg,
        "vs_yesterday": None if not yesterday else now_cum / yesterday - 1,
        "vs_avg": None if not avg else now_cum / avg - 1, "times_avg": None if not avg else now_cum / avg,
        "avg_days": len(olds), "points": points,
        "index_move": None if len(order) < 2 else _move(days, order),
    }


def _move(days: dict, order: list[str]) -> float | None:
    """当天最新价相对前一个交易日收盘的涨跌；价格是几个品种相加的，只在单品种时才有意义。"""
    last, prev = days[order[-1]][-1][2], days[order[-2]][-1][2]
    return None if not prev else last / prev - 1


def build(now: datetime | None = None) -> dict:
    now = now or datetime.now(_ZONE)
    out: dict = {"ok": False, "live": _live(now), "updated": now.strftime("%H:%M:%S")}
    try:
        market_rows = [row for secid in MARKET for row in fetch(secid)]
        market = compare(market_rows)
        sh = compare(fetch(MARKET[0]))
        out["market"] = market and {**market, "index_move": sh and sh["index_move"]}
        out["ok"] = market is not None
    except Exception:
        out["market"] = None
    try:
        etf_rows = []
        for code, _name in TEAM_BOOK:
            try:
                etf_rows += fetch(secid_of(code))
            except Exception:
                continue
        etf = compare(etf_rows)
        if etf:
            move = (out.get("market") or {}).get("index_move")
            etf["alert"] = bool(move is not None and move <= DROP and etf["times_avg"] is not None and etf["times_avg"] >= SURGE)
            etf["index_move"] = move
        out["etf"] = etf
    except Exception:
        out["etf"] = None
    return out


def _live(now: datetime) -> bool:
    minute = now.hour * 60 + now.minute
    return now.weekday() < 5 and (570 <= minute <= 690 or 780 <= minute <= 900)
