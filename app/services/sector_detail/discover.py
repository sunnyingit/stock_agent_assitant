"""找出一只股票可能属于的板块并收集三层证据（取数部分；判断规则在 affiliation.py）。

候选 = 申万三级和二级行业 + 同花顺概念（去掉市场标签）。评分只用指数日线（每个候选一次调用，并行），
成分名单只为排名靠前的几个候选去取，完整的成分走势留给最终选中的两个板块（load.py）。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from app import config
from app.services.sector_detail import affiliation as aff
from app.services.sector_detail.tsutil import call, first, iso, member_rows, num, records, ret, si
from app.tools import cache

WORKERS = 8
_TTL = lambda: config.CACHE_TTL["sector_detail"]  # noqa: E731


def _cached(key: str, make):
    hit = cache.get(key)
    if hit is not None:
        return hit
    value = make()
    if value:
        cache.set(key, value, _TTL())
    return value


def industry_row(pro, ts_code: str) -> dict:
    return first(call(lambda: pro.index_member_all(ts_code=ts_code, is_new="Y")))


def ths_catalog(pro) -> dict:
    def make():
        return {
            str(row.get("ts_code")): {"name": str(row.get("name") or row.get("ts_code")), "type": str(row.get("type") or ""), "count": num(row.get("count")) or 0}
            for row in records(call(lambda: pro.ths_index(exchange="A"))) if row.get("ts_code")
        }

    return _cached("sector_ths_index", make)


def sw_members(pro, code: str, level: str) -> list[dict]:
    bare = code[:-3] if str(code).endswith(".SI") else str(code)
    for attempt in dict.fromkeys((code, bare, f"{bare}.SI")):
        members = member_rows(call(lambda value=attempt: pro.index_member_all(**{f"{level}_code": value, "is_new": "Y"})))
        if members:
            return members
    return member_rows(call(lambda: pro.index_member(index_code=f"{bare}.SI", is_new="Y")))


def members_of(pro, cand: dict) -> list[dict]:
    if cand["kind"] == "sw":
        return sw_members(pro, cand["code"], cand["level"])
    return member_rows(call(lambda: pro.ths_member(ts_code=cand["code"])))


def index_returns(pro, cand: dict, start: str, end: str) -> dict:
    """板块发布指数的日收益（日期→小数）。申万用 sw_daily，同花顺概念用 ths_daily。"""
    def make():
        if cand["kind"] == "sw":
            frame = call(lambda: pro.sw_daily(ts_code=cand["code"], start_date=start, end_date=end))
        else:
            frame = call(lambda: pro.ths_daily(ts_code=cand["code"], start_date=start, end_date=end))
        out = {}
        for row in records(frame):
            day, value = iso(row.get("trade_date")), ret(row.get("pct_change"))
            if day and value is not None:
                out[day] = value
        return out

    return _cached(f"sector_ix:{cand['code']}:{end}", make) or {}


def main_business(pro, ts_code: str) -> list[tuple[str, float]]:
    """最近一期的主营产品构成（产品线, 收入）。"""
    def make():
        rows = records(call(lambda: pro.fina_mainbz(ts_code=ts_code, type="P")))
        stamps = sorted({str(row.get("end_date")) for row in rows if row.get("end_date")})
        if not stamps:
            return []
        return [(str(row.get("bz_item") or ""), num(row.get("bz_sales")) or 0.0) for row in rows if str(row.get("end_date")) == stamps[-1]]

    return _cached(f"sector_mainbz:{ts_code}", make) or []


def market_caps(pro, day: str) -> dict[str, float]:
    def make():
        frame = call(lambda: pro.daily_basic(trade_date=day.replace("-", ""), fields="ts_code,total_mv"))
        return {str(row["ts_code"]): num(row.get("total_mv")) for row in records(frame) if num(row.get("total_mv")) is not None}

    return _cached(f"sector_mv:{day}", make) or {}


def _stubs(pro, ts_code: str, row: dict) -> list[dict]:
    found = []
    for level, code_key, name_key in (("l3", "l3_code", "l3_name"), ("l2", "l2_code", "l2_name")):
        code = si(row.get(code_key))
        if code:
            found.append({
                "code": code, "name": str(row.get(name_key) or code), "type": "industry", "kind": "sw", "level": level,
                "l1_code": si(row.get("l1_code")), "l1_name": str(row.get("l1_name") or ""), "member": True,
            })
    catalog = ths_catalog(pro) or {}
    codes = dict.fromkeys(str(item.get("ts_code") or "") for item in records(call(lambda: pro.ths_member(con_code=ts_code))))
    for code in codes:
        meta = catalog.get(code)
        if meta and meta["type"] == "N" and aff.usable_concept(meta["name"], int(meta["count"] or 0)):
            found.append({"code": code, "name": meta["name"], "type": "concept", "kind": "ths", "member": True})
    return found


def _prelim(cand: dict) -> float:
    biz = min((cand["business"].get("share") or 0.0) / aff.BUSINESS_FULL, 1.0)
    return 0.5 * biz + 0.5 * max(cand["linkage"].get("corr60") or 0.0, 0.0)


def gather(pro, stock, stock_rets: dict, dates: list[str], start: str, end: str) -> dict:
    """返回 {candidates, primary, secondary, merged, driver, members, industry}。candidates 的每一项带三层证据和综合分。"""
    row = industry_row(pro, stock.ts_code)
    stubs = _stubs(pro, stock.ts_code, row)
    lines = main_business(pro, stock.ts_code)
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        series = list(pool.map(lambda cand: index_returns(pro, cand, start, end), stubs))
    cands = []
    for cand, returns in zip(stubs, series):
        linkage = aff.link(stock_rets, returns, dates)
        if linkage["days"] < aff.MIN_DAYS:
            continue
        business = aff.business_share(lines, cand["name"])
        if cand["kind"] == "sw" and cand["level"] == "l3":
            business = aff.official_business(business)
        cands.append({**cand, "returns": returns, "linkage": linkage, "business": business})
    shortlist = [c for c in cands if c["kind"] == "sw"]   # 本股所属的申万行业一定要有成分，否则没法算它里面的名次
    shortlist += [c for c in sorted(cands, key=_prelim, reverse=True) if c["kind"] != "sw"][:aff.SHORTLIST]
    caps = market_caps(pro, dates[-1]) if dates else {}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        listed = list(pool.map(lambda cand: members_of(pro, cand), shortlist))
    roster = {}
    for cand, members in zip(shortlist, listed):
        roster[cand["code"]] = members
        cand["members"] = {item["ts_code"] for item in members}
    for cand in cands:
        members = roster.get(cand["code"])
        if members is None:
            cand["position"] = aff.position(None, None)
        else:
            cand["position"] = aff.position(*aff.rank_by_value(stock.ts_code, [item["ts_code"] for item in members], caps))
        cand["score"] = aff.score(cand["business"], cand["linkage"], cand["position"])
        cand["belongs"], cand["basis"] = aff.judge(cand)
    picked = aff.choose(cands, dates, stock_rets)
    primary, secondary = picked["primary"], picked["secondary"]
    driver = None
    if primary is not None:
        driver = aff.attribute(stock_rets, primary["returns"], None if secondary is None else secondary["returns"], dates)
    return {
        "candidates": cands, "primary": primary, "secondary": secondary, "merged": picked["merged"], "driver": driver,
        "roster": roster, "industry": row, "caps": caps, "lines": aff.business_lines(lines),
    }


def public_candidate(cand: dict, role: str | None) -> dict:
    return {
        "code": cand["code"], "name": cand["name"], "type": cand["type"],
        "business": cand["business"], "linkage": cand["linkage"], "position": cand["position"],
        "score": cand["score"], "belongs": cand["belongs"], "basis": cand["basis"], "role": role,
    }
