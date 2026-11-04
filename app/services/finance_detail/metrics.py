"""财报指标的计算：只做算术，不判断好坏。输入 load.load_snapshot 的快照和"今天"，输出分组的指标字典。

单季口径优先用 tushare 的单季利润表，没有时用累计数相减，来源记在 data.single_source。
金额都是元，比例都是小数（0.31 = 31%），百分点差也是小数（0.02 = 2 个百分点）。
"""

from __future__ import annotations

from datetime import date
from statistics import mean

_KIND = {"0331": "q1", "0630": "h1", "0930": "q3", "1231": "annual"}
_NEXT = {"0331": "0630", "0630": "0930", "0930": "1231"}
_PREV = {"0630": "0331", "0930": "0630", "1231": "0930"}
_FINANCE_WORDS = ("银行", "证券", "保险", "多元金融", "期货", "信托")
_REALESTATE_WORDS = ("房地产", "地产", "房产")


# ---------------------------------------------------------------- 小工具
def ymd(day: date) -> str:
    return day.strftime("%Y%m%d")


def parse_ymd(value) -> date | None:
    text = "".join(ch for ch in str(value or "") if ch.isdigit())[:8]
    if len(text) < 8:
        return None
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    except ValueError:
        return None


def num(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


def div(a, b):
    return None if a is None or b in (None, 0) else a / b


def growth(cur, prior):
    """同比增速。基期为零或为负（亏损）时不算百分比，返回 None。"""
    if cur is None or prior is None or prior <= 0:
        return None
    return cur / prior - 1


def prev_end(end: str) -> str | None:
    return f"{end[:4]}{_PREV[end[4:]]}" if end[4:] in _PREV else None


def last_year(end: str) -> str:
    return f"{int(end[:4]) - 1}{end[4:]}"


def prior_quarter(end: str) -> str:
    """上一个季度末（跨年：一季度的上一季度是去年年报期）。"""
    return prev_end(end) or f"{int(end[:4]) - 1}1231"


def next_end(end: str) -> str:
    return f"{end[:4]}{_NEXT[end[4:]]}" if end[4:] in _NEXT else f"{int(end[:4]) + 1}0331"


def quarter_ends(end: str, count: int) -> list[str]:
    out = [end]
    while len(out) < count:
        out.append(prior_quarter(out[-1]))
    return out


def deadline(end: str) -> date | None:
    """法定披露截止日：一季报 4/30，半年报 8/31，三季报 10/31，年报次年 4/30。"""
    year, tail = int(end[:4]), end[4:]
    return {"0331": date(year, 4, 30), "0630": date(year, 8, 31), "0930": date(year, 10, 31), "1231": date(year + 1, 4, 30)}.get(tail)


def expected_end(today: date) -> str:
    """今天按法定截止日，应该已经披露的最新报告期。"""
    for year in (today.year, today.year - 1):
        for tail in ("1231", "0930", "0630", "0331"):
            end = f"{year}{tail}"
            due = deadline(end)
            if due and due <= today:
                return end
    return f"{today.year - 1}0930"


def periods_between(older: str, newer: str) -> int:
    count, cursor = 0, older
    while cursor < newer and count < 40:
        cursor = next_end(cursor)
        count += 1
    return count


def _visible(rows: list[dict], as_of: date) -> list[dict]:
    cut = ymd(as_of)
    return [row for row in rows or [] if str(row.get("ann_date") or "") and str(row["ann_date"]) <= cut]


def _row_rank(row: dict) -> tuple:
    """更正版优先于原始版；同一类里取公告日更晚的。公告日相同则后出现的覆盖先出现的。"""
    revised = 1 if str(row.get("update_flag")) == "1" else 0
    return (revised, str(row.get("ann_date") or ""))


def _by_end(rows: list[dict]) -> dict[str, dict]:
    """同一报告期有多版时，留下应采用的那一版。"""
    out: dict[str, dict] = {}
    for row in rows:
        end = str(row.get("end_date") or "")
        if not end:
            continue
        prior = out.get(end)
        if prior is None or _row_rank(row) >= _row_rank(prior):
            out[end] = row
    return out


def _series(rows_by_end: dict[str, dict], field: str) -> dict[str, float]:
    return {end: value for end, row in rows_by_end.items() if (value := num(row.get(field))) is not None}


def _single(cum: dict[str, float], end: str):
    """累计数相减得到单季。一季度本身就是累计。"""
    cur = cum.get(end)
    if cur is None:
        return None
    before = prev_end(end)
    if before is None:
        return cur
    last = cum.get(before)
    return None if last is None else cur - last


def industry_rule(name) -> str:
    text = str(name or "")
    if any(word in text for word in _FINANCE_WORDS):
        return "finance"
    if any(word in text for word in _REALESTATE_WORDS):
        return "realestate"
    return "general"


# ---------------------------------------------------------------- 总入口
def compute(snapshot: dict, today: date) -> dict | None:
    """没有任何已披露的利润表时返回 None。"""
    inc_c = _by_end(_visible(snapshot.get("income_c") or [], today))
    if not inc_c:
        return None
    end = max(inc_c)
    inc_q_raw = _by_end(_visible(snapshot.get("income_q") or [], today))
    ind = _by_end(_visible(snapshot.get("indicator") or [], today))
    cf = _by_end(_visible(snapshot.get("cashflow_c") or [], today))
    bs = _by_end(_visible(snapshot.get("balance") or [], today))

    cum = {field: _series(inc_c, field) for field in (
        "revenue", "total_revenue", "oper_cost", "sell_exp", "admin_exp", "rd_exp", "fin_exp", "n_income_attr_p", "n_income",
        "invest_income", "fv_value_chg_gain", "forex_gain", "assets_impair_loss", "oth_b_income", "non_oper_income", "nca_disploss",
        "total_profit", "income_tax", "minority_gain",
    )}
    cum["ded"] = _series(ind, "profit_dedt")
    cum["ocf"] = _series(cf, "n_cashflow_act")
    cum["capex"] = _series(cf, "c_pay_acq_const_fiolta")

    def single(field: str, at: str):
        """单季数。利润表字段优先用 tushare 单季口径，没有就累计相减。"""
        quarterly = _series(inc_q_raw, field) if field in cum and field not in ("ded", "ocf", "capex") else {}
        if at in quarterly:
            return quarterly[at]
        return _single(cum[field], at)

    ends = quarter_ends(end, 14)   # 校正环比要 3 年同季环比，每一年还要再往前一个季度
    rev_q = {at: single("revenue", at) for at in ends}
    np_q = {at: single("n_income_attr_p", at) for at in ends}
    ded_q = {at: _single(cum["ded"], at) for at in ends}
    source = "tushare" if end in _series(inc_q_raw, "revenue") else "diff"

    m = {
        "report_end": end,
        "kind": _KIND.get(end[4:], ""),
        "q_index": {"0331": 1, "0630": 2, "0930": 3, "1231": 4}.get(end[4:], 0),
        "industry": snapshot.get("industry") or "",
        "industry_rule": industry_rule(snapshot.get("industry")),
    }
    m["time"] = _time(snapshot, inc_c, end, today)
    m["growth"] = _growth(cum, rev_q, np_q, ded_q, end, ends)
    m["quality"] = _quality(cum, end)
    m["margin"] = _margin(cum, single, end)
    m["bridge"] = _bridge(single, ded_q, end)
    m["cash"] = _cash(cum, single, ded_q, end, ends)
    m["balance"] = _balance(bs, cum, end)
    m["quarters"] = _quarters(rev_q, np_q, ded_q, ends[:8])
    m["data"] = _data(snapshot, inc_q_raw, ind, end, today, source, ends, rev_q, m)
    if m["data"]["listed_years"] is not None and m["data"]["listed_years"] < 3:   # 上市不足 3 年：不做季节性校正，只用同比判断
        m["growth"]["adj_available"], m["growth"]["qoq_rev_adj"], m["growth"]["adj_state"] = False, None, None
        m["data"]["adj_ok"] = False
    m["data"]["chronic_loss"] = len(m["quarters"]) >= 4 and all((row["deducted"] or 0) < 0 for row in m["quarters"][:4])
    m["growth"]["loss_rate"] = None if not m["growth"]["loss_now"] or not rev_q[end] else -np_q[end] / rev_q[end]
    m["valuation"] = _valuation_effect(cum, end)
    m["cycle"] = _cycle(single, end)
    m["lines"] = _lines(snapshot, cum, end)
    m["expect"] = _expect(snapshot, m, rev_q, np_q, ends, cum)
    m["reaction"] = _reaction(snapshot, parse_ymd(m["time"]["announced"]), today)
    m["peers"] = _peers(snapshot, m)
    return m


# ---------------------------------------------------------------- 各组
def _time(snapshot: dict, inc_c: dict, end: str, today: date) -> dict:
    row = inc_c[end]
    announced = parse_ymd(row.get("f_ann_date") or row.get("ann_date"))
    nxt = next_end(end)
    schedule = next((r for r in snapshot.get("disclosure") or [] if str(r.get("end_date")) == nxt), None)
    pre = parse_ymd((schedule or {}).get("pre_date"))
    due = deadline(nxt)
    target, source = (pre, "预约") if pre else (due, "法定截止")
    forecasts = [r for r in snapshot.get("forecast") or [] if str(r.get("end_date")) > end and str(r.get("ann_date") or "") <= ymd(today)]
    forecasts.sort(key=lambda r: str(r.get("ann_date")), reverse=True)
    forecast_days = None
    if forecasts:
        forecast_day = parse_ymd(forecasts[0].get("ann_date"))
        forecast_days = None if forecast_day is None else (today - forecast_day).days
    express = [r for r in snapshot.get("express") or [] if str(r.get("end_date")) > end and str(r.get("ann_date") or "") <= ymd(today)]
    annual = sorted((r for r in snapshot.get("audit") or [] if str(r.get("end_date", "")).endswith("1231") and str(r.get("ann_date") or "") <= ymd(today)), key=lambda r: str(r.get("end_date")), reverse=True)
    audit = annual[0] if annual else None
    return {
        "announced": announced.isoformat() if announced else None,
        "days_since": None if announced is None else (today - announced).days,
        "next_end": nxt,
        "next_date": target.isoformat() if target else None,
        "next_source": source,
        "days_to_next": None if target is None else (target - today).days,
        "forecast": forecasts[0] if forecasts else None,
        "forecast_days": forecast_days,
        "express": express[0] if express else None,
        "audit": None if audit is None else {
            "end_date": audit.get("end_date"), "result": audit.get("audit_result") or "",
            "standard": "标准无保留" in str(audit.get("audit_result") or ""),
        },
    }


def _growth(cum, rev_q, np_q, ded_q, end, ends) -> dict:
    last = last_year(end)
    prev = prior_quarter(end)
    g = {
        "q_rev": rev_q[end], "q_rev_yoy": growth(rev_q[end], rev_q.get(last)),
        "q_np": np_q[end], "q_np_yoy": growth(np_q[end], np_q.get(last)),
        "q_ded": ded_q[end], "q_ded_yoy": growth(ded_q[end], ded_q.get(last)),
        "cum_rev_yoy": growth(cum["revenue"].get(end), cum["revenue"].get(last)),
        "cum_np_yoy": growth(cum["n_income_attr_p"].get(end), cum["n_income_attr_p"].get(last)),
        "cum_ded_yoy": growth(cum["ded"].get(end), cum["ded"].get(last)),
        "qoq_rev": growth(rev_q[end], rev_q.get(prev)),
        "qoq_np": growth(np_q[end], np_q.get(prev)),
    }
    g["loss_now"] = np_q[end] is not None and np_q[end] < 0
    g["loss_prior"] = np_q.get(last) is not None and np_q[last] < 0
    g["loss_change"] = None if np_q[end] is None or np_q.get(last) is None else np_q[end] - np_q[last]
    # 上一季的同比，用来判断加速还是减速
    prev_yoy_rev = growth(rev_q.get(prev), rev_q.get(prior_quarter(last)))
    prev_yoy_np = growth(np_q.get(prev), np_q.get(prior_quarter(last)))
    g["prev_q_rev_yoy"], g["prev_q_np_yoy"] = prev_yoy_rev, prev_yoy_np
    g["rev_accel_pp"] = None if g["q_rev_yoy"] is None or prev_yoy_rev is None else g["q_rev_yoy"] - prev_yoy_rev
    g["np_accel_pp"] = None if g["q_np_yoy"] is None or prev_yoy_np is None else g["q_np_yoy"] - prev_yoy_np
    g["scissors_pp"] = None if g["q_rev_yoy"] is None or g["q_np_yoy"] is None else g["q_rev_yoy"] - g["q_np_yoy"]
    # 季节性校正环比：本季环比减去过去 3 年同季环比的均值，历史不足 3 年只报原始环比
    past = []
    for years in (1, 2, 3):
        at = end
        for _ in range(4 * years):
            at = prior_quarter(at)
        qoq = growth(rev_q.get(at), rev_q.get(prior_quarter(at))) if at in rev_q else None
        if qoq is not None:
            past.append(qoq)
    g["adj_available"] = len(past) >= 3 and g["qoq_rev"] is not None
    g["qoq_rev_adj"] = g["qoq_rev"] - mean(past) if g["adj_available"] else None
    g["adj_state"] = None
    if g["qoq_rev_adj"] is not None:
        threshold = 0.10
        g["adj_state"] = "accelerating" if g["qoq_rev_adj"] > threshold else "decelerating" if g["qoq_rev_adj"] < -threshold else "normal"
    # 连续两季同比和校正环比都下滑（硬阈值用）
    g["slowdown_two_quarters"] = False
    if g["rev_accel_pp"] is not None and g["rev_accel_pp"] < 0 and g["qoq_rev_adj"] is not None and g["qoq_rev_adj"] < 0:
        before = prior_quarter(end)
        b_prev_yoy = growth(rev_q.get(prior_quarter(before)), rev_q.get(prior_quarter(last_year(before))))
        b_yoy = growth(rev_q.get(before), rev_q.get(last_year(before)))
        g["slowdown_two_quarters"] = b_yoy is not None and b_prev_yoy is not None and b_yoy < b_prev_yoy
    return g


def _quality(cum, end) -> dict:
    last = last_year(end)
    np_cum, ded_cum = cum["n_income_attr_p"].get(end), cum["ded"].get(end)
    nonrec = None if np_cum is None or ded_cum is None else np_cum - ded_cum
    comp = {
        "invest": cum["invest_income"].get(end), "fair_value": cum["fv_value_chg_gain"].get(end), "forex": cum["forex_gain"].get(end),
        "subsidy": cum["oth_b_income"].get(end), "disposal": cum["nca_disploss"].get(end),
    }
    fin_now, fin_before = cum["fin_exp"].get(end), cum["fin_exp"].get(last)
    imp_now, imp_before = cum["assets_impair_loss"].get(end), cum["assets_impair_loss"].get(last)
    return {
        "np_cum": np_cum, "ded_cum": ded_cum,
        "ded_to_np": div(ded_cum, np_cum) if np_cum and np_cum > 0 else None,
        "nonrec": nonrec,
        "nonrec_share": div(nonrec, np_cum) if np_cum and np_cum > 0 and nonrec is not None else None,
        "ded_negative_np_positive": bool(np_cum and np_cum > 0 and ded_cum is not None and ded_cum < 0),
        "comp": comp,
        "market_driven": None if nonrec in (None, 0) else sum(v for k, v in comp.items() if k in ("invest", "fair_value") and v is not None) / nonrec,
        "fin_exp": fin_now, "fin_exp_before": fin_before,
        "fin_exp_shift": None if fin_now is None or fin_before is None else fin_now - fin_before,
        "fin_exp_shift_to_np": None if fin_now is None or fin_before is None or not np_cum else (fin_now - fin_before) / abs(np_cum),
        "impair": imp_now, "impair_before": imp_before,
        "impair_change": None if imp_now is None or imp_before is None else imp_now - imp_before,
    }


def _margin(cum, single, end) -> dict:
    last, prev = last_year(end), prior_quarter(end)

    def ratios(at: str) -> dict:
        rev = single("revenue", at)
        out = {"rev": rev}
        out["gm"] = div(None if rev is None or single("oper_cost", at) is None else rev - single("oper_cost", at), rev)
        out["nm"] = div(single("n_income_attr_p", at), rev)
        for key, field in (("sell", "sell_exp"), ("admin", "admin_exp"), ("rd", "rd_exp"), ("fin", "fin_exp")):
            out[key] = div(single(field, at), rev)
        return out

    now, before_y, before_q = ratios(end), ratios(last), ratios(prev)

    def delta(a, b):
        return None if a is None or b is None else a - b

    out = {key: now.get(key) for key in ("gm", "nm", "sell", "admin", "rd", "fin")}
    for key in ("gm", "nm", "sell", "admin", "rd", "fin"):
        out[f"{key}_yoy_pp"] = delta(now.get(key), before_y.get(key))
        out[f"{key}_qoq_pp"] = delta(now.get(key), before_q.get(key))
    expense = [out["sell_yoy_pp"], out["admin_yoy_pp"], out["rd_yoy_pp"], out["fin_yoy_pp"]]
    out["expense_yoy_pp"] = None if any(v is None for v in expense) else sum(expense)
    # 净利率变动的主因：毛利率变动 vs 费用率变动（费用率下降是加分）
    if out["gm_yoy_pp"] is not None and out["expense_yoy_pp"] is not None:
        gross_effect, expense_effect = out["gm_yoy_pp"], -out["expense_yoy_pp"]
        out["driver"] = "gross" if abs(gross_effect) >= abs(expense_effect) else "expense"
    else:
        out["driver"] = None
    return out


def _bridge(single, ded_q, end) -> dict | None:
    """利润桥：单季归母净利润同比变化 = 收入增长贡献 + 毛利率变化 + 四项费用率变化 + 非经常损益变化 + 所得税与少数股东损益变化 + 其余。
    收入增长贡献按去年的经营利润率（毛利率减费用率）折算；毛利率和费用率的变化按本期收入折算；非经常损益用归母减扣非的变化；其余是前几项加总后剩下的（税金及附加、减值等），保证几项相加等于总变化。"""
    last = last_year(end)

    def block(at: str):
        fields = {name: single(name, at) for name in ("revenue", "oper_cost", "sell_exp", "admin_exp", "rd_exp", "fin_exp", "total_profit", "income_tax", "minority_gain", "n_income_attr_p")}
        if any(v is None for v in fields.values()) or not fields["revenue"] or fields["revenue"] <= 0:
            return None
        fields["ded"] = ded_q.get(at)
        expense = fields["sell_exp"] + fields["admin_exp"] + fields["rd_exp"] + fields["fin_exp"]
        return {**fields, "gm": (fields["revenue"] - fields["oper_cost"]) / fields["revenue"], "exp_ratio": expense / fields["revenue"],
                "nonrec": None if fields["ded"] is None else fields["n_income_attr_p"] - fields["ded"]}

    now, before = block(end), block(last)
    if now is None or before is None:
        return None
    delta = now["n_income_attr_p"] - before["n_income_attr_p"]
    revenue = (now["revenue"] - before["revenue"]) * (before["gm"] - before["exp_ratio"])
    gross = now["revenue"] * (now["gm"] - before["gm"])
    expense = -now["revenue"] * (now["exp_ratio"] - before["exp_ratio"])
    nonrec = None if now["nonrec"] is None or before["nonrec"] is None else now["nonrec"] - before["nonrec"]
    tax = -((now["income_tax"] - before["income_tax"]) + (now["minority_gain"] - before["minority_gain"]))
    other = delta - revenue - gross - expense - tax - (nonrec or 0.0)
    parts = {"revenue": revenue, "gross_margin": gross, "expense": expense, "nonrec": nonrec, "tax_minority": tax, "other": other}
    named = {k: v for k, v in parts.items() if v is not None and k != "other"}
    return {
        "delta": delta, "np_now": now["n_income_attr_p"], "np_before": before["n_income_attr_p"], "parts": parts,
        "top_drag": min(named, key=named.get) if named and min(named.values()) < 0 else None,
        "top_boost": max(named, key=named.get) if named and max(named.values()) > 0 else None,
    }


def _cash(cum, single, ded_q, end, ends) -> dict:
    ocf_q = {at: _single(cum["ocf"], at) for at in ends[:6]}
    np_q = {at: single("n_income_attr_p", at) for at in ends[:6]}
    ocf_cum, np_cum = cum["ocf"].get(end), cum["n_income_attr_p"].get(end)
    run = 0
    for at in ends[:6]:
        if ocf_q.get(at) is not None and ocf_q[at] < 0:
            run += 1
        else:
            break
    last4 = [ocf_q.get(at) for at in ends[:4]]
    last4_np = [np_q.get(at) for at in ends[:4]]
    capex, capex_before = cum["capex"].get(end), cum["capex"].get(last_year(end))
    prev = ends[1]
    burn_quarters = [-v for v in (ocf_q.get(at) for at in ends[:4]) if v is not None]
    burn = mean(burn_quarters) if len(burn_quarters) == 4 and mean(burn_quarters) > 0 else None   # 近 4 季平均每季净流出
    return {
        "burn": burn,
        "ocf_cum": ocf_cum, "ocf_q": ocf_q.get(end),
        "ocf_to_np_cum": div(ocf_cum, np_cum) if np_cum and np_cum > 0 else None,
        "ocf_to_np_q": div(ocf_q.get(end), np_q.get(end)) if np_q.get(end) and np_q[end] > 0 else None,
        "ocf_to_np_prev_q": div(ocf_q.get(prev), np_q.get(prev)) if np_q.get(prev) and np_q[prev] > 0 else None,
        "ocf_to_np_4q": div(sum(last4), sum(last4_np)) if all(v is not None for v in last4 + last4_np) and sum(last4_np) > 0 else None,
        "ocf_negative_run": run,
        "ocf_cum_negative": ocf_cum is not None and ocf_cum < 0,
        "capex": capex, "capex_yoy": growth(capex, capex_before),
    }


def _balance(bs: dict, cum, end) -> dict:
    now, before = bs.get(end), bs.get(last_year(end))

    def pick(row, *fields):
        values = [num((row or {}).get(f)) for f in fields]
        return None if all(v is None for v in values) else sum(v or 0 for v in values)

    ar_now, ar_before = pick(now, "accounts_receiv", "notes_receiv", "receiv_financing"), pick(before, "accounts_receiv", "notes_receiv", "receiv_financing")
    inv_now, inv_before = pick(now, "inventories"), pick(before, "inventories")
    cl_now, cl_before = pick(now, "contract_liab", "adv_receipts"), pick(before, "contract_liab", "adv_receipts")
    rev_yoy = growth(cum["revenue"].get(end), cum["revenue"].get(last_year(end)))
    cost_yoy = growth(cum["oper_cost"].get(end), cum["oper_cost"].get(last_year(end)))
    ar_yoy, inv_yoy = growth(ar_now, ar_before), growth(inv_now, inv_before)
    equity = num((now or {}).get("total_hldr_eqy_exc_min_int"))
    debt = pick(now, "st_borr", "lt_borr", "bond_payable", "non_cur_liab_due_1y", "lease_liab")
    goodwill = num((now or {}).get("goodwill"))
    return {
        "cash_balance_for_runway": num((now or {}).get("money_cap")),
        "ar": ar_now, "ar_yoy": ar_yoy, "rev_yoy": rev_yoy,
        "ar_gap_pp": None if ar_yoy is None or rev_yoy is None else ar_yoy - rev_yoy,
        "inv": inv_now, "inv_yoy": inv_yoy, "cost_yoy": cost_yoy,
        "inv_gap_pp": None if inv_yoy is None or cost_yoy is None else inv_yoy - cost_yoy,
        "contract_liab": cl_now, "contract_liab_yoy": growth(cl_now, cl_before),
        "debt": debt, "cash": num((now or {}).get("money_cap")),
        "goodwill_to_equity": div(goodwill, equity) if equity and equity > 0 else None,
    }


def _quarters(rev_q, np_q, ded_q, ends) -> list[dict]:
    rows = []
    for at in ends:
        last = last_year(at)
        rows.append({
            "end": at, "revenue": rev_q.get(at), "revenue_yoy": growth(rev_q.get(at), rev_q.get(last)),
            "net_profit": np_q.get(at), "net_profit_yoy": growth(np_q.get(at), np_q.get(last)),
            "deducted": ded_q.get(at), "deducted_yoy": growth(ded_q.get(at), ded_q.get(last)),
        })
    return rows


def _expect(snapshot: dict, m: dict, rev_q, np_q, ends, cum) -> dict:
    """预期对照：公司自己对这个报告期发过的业绩预告（实际相对预告中值偏离、落在区间的哪一段），和朴素预期。
    朴素预期 = 去年同季 ×（1 + 之前四个季度的平均同比增速），是自身趋势外推，不是市场预期。券商一致预期的接口每天只能调 10 次，不接入。"""
    end = m["report_end"]
    announced = str(m["time"]["announced"] or "").replace("-", "")
    rows = [r for r in snapshot.get("forecast") or [] if str(r.get("end_date")) == end and str(r.get("ann_date") or "") <= announced]
    rows.sort(key=lambda r: str(r.get("ann_date")), reverse=True)
    out: dict = {"preview": None, "naive": None}
    np_cum = cum["n_income_attr_p"].get(end)
    if rows and np_cum is not None:
        row = rows[0]
        low, high = num(row.get("net_profit_min")), num(row.get("net_profit_max"))
        if low is None and high is None:   # 只给了同比区间：按去年同期累计归母折算
            base = cum["n_income_attr_p"].get(last_year(end))
            p_low, p_high = num(row.get("p_change_min")), num(row.get("p_change_max"))
            if base and base > 0 and p_low is not None and p_high is not None:
                low, high = base * (1 + p_low / 100) / 1e4, base * (1 + p_high / 100) / 1e4
        if low is not None or high is not None:
            low, high = (low if low is not None else high) * 1e4, (high if high is not None else low) * 1e4
            mid = (low + high) / 2
            position = "below" if np_cum < low else "above" if np_cum > high else "within"
            out["preview"] = {
                "ann_date": row.get("ann_date"), "type": row.get("type") or "", "low": low, "high": high, "mid": mid, "actual": np_cum,
                "dev": None if mid <= 0 else np_cum / mid - 1, "dev_amount": np_cum - mid, "position": position,
                "reason": str(row.get("change_reason") or "").strip(), "summary": str(row.get("summary") or "").strip(),
            }
    yoys = []
    for at in ends[1:5]:
        value = growth(rev_q.get(at), rev_q.get(last_year(at)))
        yoys.append(value)
    out["naive"] = _naive(rev_q, np_q, end, yoys)
    return out


def _naive(rev_q, np_q, end: str, yoys: list) -> dict | None:
    known = [v for v in yoys if v is not None]
    if len(known) < 4 or rev_q.get(last_year(end)) is None:
        return None
    avg = mean(known)
    naive_rev = rev_q[last_year(end)] * (1 + avg)
    result = {"avg_yoy": avg, "rev": naive_rev, "rev_dev": div(rev_q.get(end), naive_rev) - 1 if rev_q.get(end) is not None and naive_rev > 0 else None, "np": None, "np_dev": None}
    base_np = np_q.get(last_year(end))
    np_yoys = [growth(np_q.get(at), np_q.get(last_year(at))) for at in quarter_ends(prior_quarter(end), 4)]
    np_known = [v for v in np_yoys if v is not None]
    if base_np and base_np > 0 and len(np_known) == 4 and np_q.get(end) is not None:
        naive_np = base_np * (1 + mean(np_known))
        if naive_np > 0:
            result["np"], result["np_dev"] = naive_np, np_q[end] / naive_np - 1
    return result


def _window(bars: list[dict], dates: list[str], sector: dict, lo: int, hi: int) -> dict | None:
    """(lo, hi] 区间的个股、行业累计涨幅和超额（个股减行业）。行业数据缺了就只给个股。"""
    stock = sec = 1.0
    covered = True
    for index in range(lo + 1, hi + 1):
        move = num(bars[index].get("pct_chg"))
        if move is None:
            return None
        stock *= 1 + move / 100
        day = sector.get(dates[index])
        if day is None:
            covered = False
        else:
            sec *= 1 + day / 100
    return {"stock": stock - 1, "sector": sec - 1 if covered else None, "excess": stock - sec if covered else None}


def _event_sessions(dates: list[str], ann: str) -> tuple[int, list[int]] | None:
    """披露日前一个交易日作起算，披露日当天（休市则顺延到下一个交易日）是第 1 个反应日。
    第 k 档就是这 k 个交易日的累计涨跌，不多算披露日之前那一天。"""
    before = [i for i, day in enumerate(dates) if day < ann]
    if not before:
        return None
    base = before[-1]
    sessions = [i for i, day in enumerate(dates) if day >= ann]
    return base, sessions


def _reaction(snapshot: dict, announced: date | None, today: date) -> dict | None:
    """披露后的股价反应。从披露日前一个交易日的收盘算到第 1、3、5 个反应日的收盘，对照所在申万行业指数。
    公告只有日期、没有时刻：披露日当天有成交，就把当天算作第 1 个交易日。"""
    if announced is None:
        return None
    cut = ymd(today)
    bars = sorted((r for r in snapshot.get("bars") or [] if str(r.get("trade_date")) <= cut), key=lambda r: str(r["trade_date"]))
    dates = [str(r["trade_date"]) for r in bars]
    span = _event_sessions(dates, ymd(announced))
    if span is None:
        return None
    base, sessions = span
    sector = {str(r["trade_date"]): num(r.get("pct_change")) for r in (snapshot.get("sector") or {}).get("rows") or [] if num(r.get("pct_change")) is not None}
    out = {
        "base_date": dates[base], "sector_name": (snapshot.get("sector") or {}).get("name") or "",
        "days_after": len(sessions), "after": {}, "pre5": None, "volume_ratio": None,
    }
    for k in (1, 3, 5):
        if len(sessions) >= k:
            out["after"][k] = _window(bars, dates, sector, base, sessions[k - 1])
    if base >= 5:
        out["pre5"] = _window(bars, dates, sector, base - 5, base)
    if sessions:
        day = sessions[0]
        today_amount = num(bars[day].get("amount"))
        history = [a for a in (num(bars[i].get("amount")) for i in range(max(0, base - 19), base + 1)) if a]
        if today_amount and len(history) >= 10:
            out["volume_ratio"] = today_amount / mean(history)
    return out


def forward_returns(bars: list[dict], sector_rows: list[dict], announced: date, horizons=(5, 20)) -> dict[int, dict]:
    """披露后第 N 个交易日的个股涨幅和相对行业的超额，起算点和档位同 _reaction。数据还不够的档位不给。"""
    ordered = sorted(bars, key=lambda r: str(r["trade_date"]))
    dates = [str(r["trade_date"]) for r in ordered]
    span = _event_sessions(dates, ymd(announced))
    if span is None:
        return {}
    base, sessions = span
    sector = {str(r["trade_date"]): num(r.get("pct_change")) for r in sector_rows or [] if num(r.get("pct_change")) is not None}
    return {n: w for n in horizons if len(sessions) >= n and (w := _window(ordered, dates, sector, base, sessions[n - 1]))}


def _peers(snapshot: dict, m: dict) -> dict | None:
    """本期单季营收、单季归母、累计扣非的同比，在同行里的分位（越高越好）和同行中位数。同行有数据的不足 5 家就不给。"""
    peers = snapshot.get("peers")
    if not peers or peers.get("period") != m["report_end"]:
        return None
    g = m["growth"]
    own = {"rev": g["q_rev_yoy"], "np": g["q_np_yoy"], "ded": g["cum_ded_yoy"]}
    keys = {"rev": "rev", "np": "np", "ded": "ded_cum"}
    out = {"members": peers.get("members"), "metrics": {}}
    for name, key in keys.items():
        yoys = []
        for ts_code, cur in (peers.get("cur") or {}).items():
            value = growth(num(cur.get(key)), num(((peers.get("prior") or {}).get(ts_code) or {}).get(key)))
            if value is not None:
                yoys.append(value)
        if own[name] is None or len(yoys) < 5:
            continue
        out["metrics"][name] = {
            "count": len(yoys), "own": own[name], "median": sorted(yoys)[len(yoys) // 2] if len(yoys) % 2 else mean(sorted(yoys)[len(yoys) // 2 - 1: len(yoys) // 2 + 1]),
            "pctile": sum(1 for v in yoys if v <= own[name]) / len(yoys),
        }
    return out if out["metrics"] else None


def _ttm_ded(cum: dict, at: str):
    """截至 at 的滚动 12 个月扣非净利润 = 本年累计 + 上年全年 - 上年同期累计。缺任何一项就算不出来。"""
    year_end = f"{int(at[:4]) - 1}1231"
    now, full, same = cum["ded"].get(at), cum["ded"].get(year_end), cum["ded"].get(last_year(at))
    if at.endswith("1231"):
        return now
    return None if None in (now, full, same) else now + full - same


def _valuation_effect(cum: dict, end: str) -> dict | None:
    """本期财报对估值的影响：扣非 TTM 利润从上一季到本季变了多少。股本和股价不变时，扣非每股收益、
    以及按"自身历史倍数 × 每股收益"算出的估值锚点，都按同样的比例变。这里只报变化的比例，不出价格。扣非为负，市盈率不可比，不给。"""
    now, before = _ttm_ded(cum, end), _ttm_ded(cum, prior_quarter(end))
    if now is None or before is None:
        return None
    if now <= 0 or before <= 0:
        return {"ttm_now": now, "ttm_before": before, "change": None, "applicable": False}
    return {"ttm_now": now, "ttm_before": before, "change": now / before - 1, "applicable": True}


def _cycle(single, end) -> dict:
    """毛利率处在近 5 年（20 个季度）单季毛利率的哪个分位。90% 分位以上，提示利润可能处于周期高位。"""
    series = []
    for at in quarter_ends(end, 20):
        rev, cost = single("revenue", at), single("oper_cost", at)
        if rev and rev > 0 and cost is not None:
            series.append((at, (rev - cost) / rev))
    now = dict(series).get(end)
    if now is None or len(series) < 12:
        return {"gm_pctile": None, "high": False, "quarters": len(series)}
    pctile = sum(1 for _at, value in series if value <= now) / len(series)
    return {"gm_pctile": pctile, "high": pctile >= 0.90, "quarters": len(series)}


_AGGREGATE = {"产品", "主营业务", "主营业务收入", "合计", "总计", "营业收入", "其他业务", "其他"}


def _clean_line(name: str) -> str:
    return str(name or "").replace("业务", "").replace("产品", "").strip()


def _same_line(a: str, b: str) -> bool:
    a, b = _clean_line(a), _clean_line(b)
    return bool(a and b and (a == b or a in b or b in a))


def _lines(snapshot: dict, cum: dict, end: str) -> dict | None:
    """主营构成（半年报、年报才有）：疑似并表（新出现一条产品线，收入占营收增量 20% 以上），和各产品线毛利率的同比变化。"""
    rows = snapshot.get("mainbz") or []
    periods = sorted({str(r.get("end_date")) for r in rows if str(r.get("end_date")) <= end and str(r.get("end_date"))[4:] in ("0630", "1231")})
    if not periods:
        return None
    period = periods[-1]
    before = last_year(period)

    def lines(at: str) -> dict[str, dict]:
        out = {}
        for r in rows:
            name, sales = str(r.get("bz_item") or "").strip(), num(r.get("bz_sales"))
            if str(r.get("end_date")) == at and name and name not in _AGGREGATE and sales and sales > 0:
                profit = num(r.get("bz_profit"))
                cost = num(r.get("bz_cost"))
                gross = profit if profit is not None else (None if cost is None else sales - cost)
                out[name] = {"sales": sales, "gm": None if gross is None else gross / sales}
        return out

    now, old = lines(period), lines(before)
    if not now or not old:
        return None
    increment = (cum["revenue"].get(period) or 0) - (cum["revenue"].get(before) or 0)
    new_lines = [(name, row) for name, row in now.items() if not any(_same_line(name, other) for other in old)]
    consolidation = None
    for name, row in sorted(new_lines, key=lambda pair: -pair[1]["sales"]):
        if increment > 0 and row["sales"] / increment >= 0.20:
            consolidation = {"item": name, "sales": row["sales"], "share_of_increment": row["sales"] / increment}
            break
    changes = []
    total = sum(row["sales"] for row in now.values())
    for name, row in sorted(now.items(), key=lambda pair: -pair[1]["sales"])[:4]:
        match = next((o for other, o in old.items() if _same_line(name, other)), None)
        if match and row["gm"] is not None and match["gm"] is not None:
            changes.append({"item": name, "gm": row["gm"], "gm_before": match["gm"], "change_pp": row["gm"] - match["gm"], "share": row["sales"] / total})
    return {"period": period, "consolidation": consolidation, "gm_changes": changes}


def _restated(rows: list[dict], end: str) -> bool:
    """同一报告期有原始和更正两版，而且营收或归母数字对不上，才算更正重述（两版一样的是正常的重复记录）。"""
    versions = [row for row in rows if str(row.get("end_date")) == end]
    for field in ("revenue", "n_income_attr_p"):
        values = {num(row.get(field)) for row in versions if num(row.get(field)) is not None}
        if len(values) > 1 and max(values) - min(values) > 0.005 * max(abs(v) for v in values):
            return True
    return False


def _data(snapshot, inc_q_raw, ind, end, today, source, ends, rev_q, m) -> dict:
    listed = parse_ymd(snapshot.get("list_date"))
    history = sum(1 for at in ends if rev_q.get(at) is not None)
    restated = _restated(snapshot.get("income_c") or [], end)
    return {
        "periods_behind": periods_between(end, expected_end(today)),
        "single_source": source,
        "history_quarters": history,
        "listed_years": None if listed is None else round((today - listed).days / 365.25, 1),
        "adj_ok": m["growth"]["adj_available"],
        "restated": restated,
        "applicable": m["industry_rule"] != "finance",
    }
