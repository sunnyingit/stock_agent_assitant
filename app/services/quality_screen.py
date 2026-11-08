"""质地筛查：避开垃圾公司，找同赛道里质地靠前的公司。

两件事，都是规则，不调模型，不预测涨跌：
1. screen：一家公司有没有「红线」（ST、连续亏损、近一年立案调查或行政处罚、负债率过高）和「注意」（单年亏损、
   现金流连续为负、负债偏高、盈利很弱且营收下滑、警示函）。
2. candidates：和一只股票同一个申万行业、不踩红线也没有注意项、盈利和现金都像样的公司，按 ROE、成长、现金、负债打分取前几名。
这只是质地筛选，不是买入建议：好公司也可能贵，也可能在弱势板块里。
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app import config
from app.tools import cache

_ZONE = ZoneInfo("Asia/Shanghai")
_FINANCIAL = ("银行", "保险", "证券", "房地产", "多元金融", "非银")
_RISK_RED = ("立案调查", "行政处罚", "退市风险", "风险警示")
_RISK_NOTE = ("警示函", "处罚")
TTL = 12 * 3600
POOL = 30          # 同行里按市值取前这么多家来筛，更多就太慢
MIN_ROE = 0.08
MIN_CASH = 0.6     # 经营现金流 / 净利润


def _num(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


def _pct(value):
    number = _num(value)
    return None if number is None else number / 100


def _ratio(value):
    """现金比：有的口径是 0.9，有的是 90。"""
    number = _num(value)
    if number is None:
        return None
    return number / 100 if abs(number) > 3 else number


def is_st(name) -> bool:
    text = str(name or "").strip().upper().replace(" ", "")
    return text.startswith(("*ST", "S*ST", "SST", "ST")) or "退市" in text


def is_financial(industry) -> bool:
    return any(word in str(industry or "") for word in _FINANCIAL)


def metrics(rows: list[dict]) -> dict:
    """从 fina_indicator 的行里取最新一期和近两个年报。"""
    dated = [row for row in rows if row.get("end_date")]
    if not dated:
        return {}
    newest = max(dated, key=lambda row: str(row["end_date"]))
    annual = sorted((row for row in dated if str(row["end_date"]).endswith("1231")), key=lambda row: str(row["end_date"]))[-2:]
    # ROE 是年初到报告期累计的，一季报 ×4、中报 ×2、三季报 ×4/3，才和年报能比
    factor = {"03": 4.0, "06": 2.0, "09": 4 / 3, "12": 1.0}.get(str(newest["end_date"])[4:6], 1.0)
    roe = _pct(newest.get("roe"))
    return {
        "period": str(newest["end_date"]),
        "roe": None if roe is None else roe * factor,
        "debt": _pct(newest.get("debt_to_assets")),
        "growth": _pct(newest.get("tr_yoy")),
        "profit_growth": _pct(newest.get("netprofit_yoy")),
        "margin": _pct(newest.get("netprofit_margin")),
        "cash": _ratio(newest.get("ocf_to_profit")),
        "annual_loss": [(_num(row.get("profit_dedt")) or 0) <= 0 for row in annual if _num(row.get("profit_dedt")) is not None],
        "annual_ocf_negative": [(_ratio(row.get("ocf_to_or")) or 0) < 0 for row in annual if _ratio(row.get("ocf_to_or")) is not None],
    }


def screen(name: str, industry, rows: list[dict], risks: list[dict] | None = None) -> dict:
    """{level: red|attention|clean|unknown, flags: [{level, text}], metrics}。risks 是近一年的风险公告 [{topic, title, date}]。"""
    m = metrics(rows)
    flags: list[dict] = []
    red = lambda text: flags.append({"level": "red", "text": text})        # noqa: E731
    note = lambda text: flags.append({"level": "attention", "text": text})  # noqa: E731
    if is_st(name):
        red("被特别处理（ST/*ST 或退市整理）：有退市风险")
    losses = m.get("annual_loss") or []
    if len(losses) >= 2 and all(losses):
        red("连续两个年度扣非净利润为负")
    elif losses and losses[-1]:
        note("最近一个年度扣非净利润为负")
    seen_topics = set()
    for item in risks or []:
        topic = str(item.get("topic") or "")
        if topic in seen_topics:
            continue
        seen_topics.add(topic)
        text = f"近一年有「{topic}」公告" + (f"（{item.get('date')}）" if item.get("date") else "")
        if topic in _RISK_RED:
            red(text)
        elif topic in _RISK_NOTE:
            note(text)
    debt = m.get("debt")
    if debt is not None and not is_financial(industry):
        if debt > 0.9:
            red(f"资产负债率 {debt * 100:.0f}%，偏离正常水平")
        elif debt > 0.75:
            note(f"资产负债率 {debt * 100:.0f}%，偏高")
    ocf = m.get("annual_ocf_negative") or []
    if len(ocf) >= 2 and all(ocf):
        note("经营现金流连续两个年度为负")
    roe, growth = m.get("roe"), m.get("growth")
    if roe is not None and growth is not None and roe < 0.03 and growth < 0:
        note("盈利很弱（ROE 不到 3%）且营收在下滑")
    level = "red" if any(f["level"] == "red" for f in flags) else "attention" if flags else ("clean" if m else "unknown")
    return {"level": level, "flags": flags, "metrics": m}


def score(m: dict) -> float:
    """质地分，只用来在同一批候选里排先后。ROE、营收成长、现金含量越高越好，负债率越低越好。"""
    def clip(v, lo, hi):
        return 0.0 if v is None else (min(max(v, lo), hi) - lo) / (hi - lo)
    return round(0.4 * clip(m.get("roe"), 0, 0.30) + 0.25 * clip(m.get("growth"), -0.2, 0.5) + 0.2 * clip(m.get("cash"), 0, 1.5) + 0.15 * (1 - clip(m.get("debt"), 0, 1)), 4)


def qualifies(result: dict) -> bool:
    m = result.get("metrics") or {}
    if result.get("level") != "clean" or (m.get("roe") or 0) < MIN_ROE:
        return False
    cash = m.get("cash")
    return cash is None or cash >= MIN_CASH


def why(m: dict) -> str:
    bits = []
    if m.get("roe") is not None:
        bits.append(f"ROE {m['roe'] * 100:.1f}%")
    if m.get("growth") is not None:
        bits.append(f"营收同比 {m['growth'] * 100:+.0f}%")
    if m.get("cash") is not None:
        bits.append(f"现金/利润 {m['cash']:.1f}")
    if m.get("debt") is not None:
        bits.append(f"负债率 {m['debt'] * 100:.0f}%")
    return " · ".join(bits)


def rank(rows: list[dict], limit: int) -> list[dict]:
    """rows：[{ts_code, name, total_mv, result}]。只留合格的，按质地分排。"""
    good = [r for r in rows if qualifies(r["result"])]
    out = []
    for r in sorted(good, key=lambda r: -score(r["result"]["metrics"]))[:limit]:
        m = r["result"]["metrics"]
        out.append({"ts_code": r["ts_code"], "name": r["name"], "total_mv_yi": r.get("total_mv_yi"), "score": score(m), "why": why(m), "metrics": m, "period": m.get("period")})
    return out


# ---- 取数

def _fina_rows(code: str) -> list[dict]:
    key = f"quality:fina:{code}"
    cached = cache.get(key)
    if isinstance(cached, list):
        return cached
    from app.services.fundamental_detail import load
    from app.tools.tushare_client import get_pro

    start = (datetime.now(_ZONE) - timedelta(days=1100)).strftime("%Y%m%d")
    rows = load._records(load._fina(get_pro(), code, start), load._FINA_COLUMNS)
    rows = [row for row in rows if load._announced(row, datetime.now(_ZONE).strftime("%Y%m%d"))]
    if rows:
        cache.set(key, rows, TTL)
    return rows


def _risk_items(code: str) -> list[dict]:
    key = f"quality:risk:{code}"
    cached = cache.get(key)
    if isinstance(cached, list):
        return cached
    from app.services import announcements as ann
    from app.services.news import fetch_announcements
    from app.synthesis.watchlist import stock_of

    since = (datetime.now(_ZONE) - timedelta(days=365)).strftime("%Y-%m-%d")
    items = []
    try:
        for it in ann.enrich(code, fetch_announcements(stock_of({"ts_code": code, "name": code}), 60)):
            when = str(it.get("time") or "")[:10]
            if it.get("category") == "风险与治理" and when >= since and it.get("topic"):
                items.append({"topic": it["topic"], "title": it.get("title"), "date": when})
    except Exception:  # noqa: BLE001 - 公告取不到就不下「有公告」的结论
        return []
    cache.set(key, items, TTL)
    return items


def check(stock) -> dict:
    """一家公司的筛查结果（带名字、行业、查到哪一期）。"""
    if stock.ts_code.endswith(".HK"):
        return {"ts_code": stock.ts_code, "name": stock.name, "level": "unknown", "flags": [], "metrics": {}, "note": "港股暂不筛查"}
    key = f"quality:check:{stock.ts_code}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    out = screen(stock.name, stock.industry, _fina_rows(stock.ts_code), _risk_items(stock.ts_code))
    out.update({"ts_code": stock.ts_code, "name": stock.name, "industry": stock.industry})
    cache.set(key, out, TTL)
    return out


_JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()


def candidates(stock, limit: int = 3, *, sources=None) -> dict:
    """同一申万行业里质地靠前的公司。第一次要逐家取财务指标，在后台算，先回 computing。"""
    key = f"quality:peers:{stock.ts_code}"
    done = cache.get(key)
    if done is not None:
        return {**done, "status": "ok", "items": done["all"][:limit]}
    if stock.ts_code.endswith(".HK"):
        return {"status": "empty", "items": [], "note": "港股暂不筛选"}
    with _LOCK:
        job = _JOBS.get(stock.ts_code)
        if job and job["error"]:
            _JOBS.pop(stock.ts_code, None)
            return {"status": "error", "items": [], "note": job["error"]}
        if not job:
            _JOBS[stock.ts_code] = {"error": ""}
            threading.Thread(target=_run, args=(stock, key, sources), daemon=True).start()
    return {"status": "computing", "items": []}


def _run(stock, key: str, sources) -> None:
    try:
        if sources is None:
            from app.services.fundamental_detail import load
            from app.synthesis.calendar import last_close_date
            from app.tools.tushare_client import get_pro

            pro = get_pro()
            day = last_close_date(datetime.now(_ZONE)).strftime("%Y%m%d")
            ind = load._industry(pro, stock)
            members = ind.get("members") or []
            names = load._name_map(pro)
            market = load._market_basic(pro, day)
            name_of = lambda code: (names.get(code) or {}).get("name")  # noqa: E731
            sources = {"industry": ind.get("name") or stock.industry, "members": members, "market": market, "name_of": name_of, "fina": _fina_rows}
        members, market, name_of = sources["members"], sources["market"], sources["name_of"]
        pool = []
        for item in members:
            code = item.get("ts_code")
            name = name_of(code) or item.get("name") or code
            if not code or code == stock.ts_code or is_st(name):
                continue
            mv = _num((market.get(code) or {}).get("total_mv"))
            pool.append({"ts_code": code, "name": name, "total_mv_yi": None if mv is None else round(mv / 10000, 1), "mv": mv or 0})
        pool = sorted(pool, key=lambda r: -r["mv"])[:POOL]
        industry = sources["industry"]

        def one(row):
            try:
                return {**row, "result": screen(row["name"], industry, sources["fina"](row["ts_code"]), [])}
            except Exception:  # noqa: BLE001 - 一家取不到不影响其余
                return None

        with ThreadPoolExecutor(max_workers=4) as pool_ex:
            checked = [r for r in pool_ex.map(one, pool) if r]
        full = rank(checked, 10)
        cache.set(key, {"industry": industry, "checked": len(checked), "pool": len(pool), "all": full, "as_of": datetime.now(_ZONE).strftime("%Y-%m-%d")}, TTL)
        with _LOCK:
            _JOBS.pop(stock.ts_code, None)
    except Exception as exc:  # noqa: BLE001
        with _LOCK:
            _JOBS[stock.ts_code] = {"error": f"同行筛选失败：{exc}"}
