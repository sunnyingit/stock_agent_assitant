"""从 Tushare 取财报要用的原始数据：单季和累计利润表、累计现金流量表、资产负债表、扣非利润、预告、快报、预约披露日、审计意见、主营构成。

只取数、不计算。取不到的部分留空列表，不编造；整份快照是纯字典和列表，可以直接存成测试夹具。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import pandas as pd

from app.tools.stock import Stock

YEARS_BACK = 5   # 校正环比要看过去 3 年的同季环比，同比还要再往前一年，多取一些

INCOME_FIELDS = (
    "ts_code,ann_date,f_ann_date,end_date,report_type,update_flag,revenue,total_revenue,oper_cost,sell_exp,admin_exp,rd_exp,fin_exp,"
    "invest_income,fv_value_chg_gain,forex_gain,assets_impair_loss,oth_b_income,non_oper_income,nca_disploss,"
    "total_profit,income_tax,n_income,n_income_attr_p,minority_gain"
)
CASHFLOW_FIELDS = "ts_code,ann_date,f_ann_date,end_date,report_type,update_flag,n_cashflow_act,c_pay_acq_const_fiolta"
BALANCE_FIELDS = (
    "ts_code,ann_date,f_ann_date,end_date,report_type,update_flag,accounts_receiv,notes_receiv,inventories,contract_liab,adv_receipts,"
    "st_borr,lt_borr,bond_payable,non_cur_liab_due_1y,money_cap,goodwill,total_hldr_eqy_exc_min_int"
)


def load_snapshot(stock: Stock, today: date) -> dict:
    from app.tools.tushare_client import get_pro

    pro = get_pro()
    start = f"{today.year - YEARS_BACK}0101"
    end = f"{today.year + 1}1231"
    jobs = {
        "income_q": lambda: pro.income(ts_code=stock.ts_code, report_type="2", start_date=start, fields=INCOME_FIELDS),
        "income_c": lambda: pro.income(ts_code=stock.ts_code, report_type="1", start_date=start, fields=INCOME_FIELDS),
        "cashflow_c": lambda: pro.cashflow(ts_code=stock.ts_code, report_type="1", start_date=start, fields=CASHFLOW_FIELDS),
        "balance": lambda: pro.balancesheet(ts_code=stock.ts_code, report_type="1", start_date=start, fields=BALANCE_FIELDS),
        "indicator": lambda: pro.fina_indicator(ts_code=stock.ts_code, start_date=start, fields="ts_code,ann_date,end_date,update_flag,profit_dedt"),
        "audit": lambda: pro.fina_audit(ts_code=stock.ts_code),
        "forecast": lambda: pro.forecast(ts_code=stock.ts_code, start_date=f"{today.year - 1}0101"),
        "express": lambda: pro.express(ts_code=stock.ts_code, start_date=f"{today.year - 1}0101"),
        "disclosure": lambda: pro.disclosure_date(ts_code=stock.ts_code, end_date=end),
        "mainbz": lambda: pro.fina_mainbz(ts_code=stock.ts_code, type="P", fields="ts_code,end_date,bz_item,bz_sales,bz_profit,bz_cost"),
    }
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {name: pool.submit(_call, job) for name, job in jobs.items()}
        frames = {name: future.result() for name, future in futures.items()}
    basic = _call(lambda: pro.stock_basic(ts_code=stock.ts_code, fields="ts_code,name,industry,list_date"))
    info = (_records(basic) or [{}])[0]
    snapshot = {
        "ts_code": stock.ts_code,
        "stock_name": info.get("name") or stock.name,
        "industry": info.get("industry") or stock.industry or "",
        "list_date": info.get("list_date") or "",
        "as_of": today.isoformat(),
        **{name: _records(frame) for name, frame in frames.items()},
    }
    snapshot.update(_market(pro, stock.ts_code, today))
    snapshot["peers"] = _peers(pro, stock.ts_code, snapshot["sector"], snapshot["income_c"])
    return snapshot


def _market(pro, ts_code: str, today: date) -> dict:
    """披露后的股价反应要用的行情：个股日线，和所在申万行业指数（三级行业，没有就退回二级）的日线。"""
    start = (today - timedelta(days=150)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")
    bars = _records(_call(lambda: pro.daily(ts_code=ts_code, start_date=start, end_date=end, fields="ts_code,trade_date,pct_chg,vol,amount")))
    industry = _industry(pro, ts_code)
    rows = []
    if industry["code"]:
        rows = _records(_call(lambda: pro.sw_daily(ts_code=industry["code"], start_date=start, end_date=end, fields="ts_code,trade_date,pct_change")))
    return {"bars": bars, "sector": {**industry, "rows": rows}}


def _industry(pro, ts_code: str) -> dict:
    """所在行业：三级行业成分股（除自己外至少 5 家）就用三级，否则退回二级。"""
    row = (_records(_call(lambda: pro.index_member_all(ts_code=ts_code, is_new="Y"))) or [{}])[0]
    for level in ("l3", "l2"):
        code, name = row.get(f"{level}_code"), row.get(f"{level}_name")
        if not code:
            continue
        members = [r["ts_code"] for r in _records(_call(lambda: pro.index_member_all(**{f"{level}_code": code}, is_new="Y"))) if r.get("ts_code")]
        if len([m for m in members if m != ts_code]) >= 5 or level == "l2":
            return {"code": code, "name": name or "", "level": level, "members": members}
    return {"code": None, "name": "", "level": None, "members": []}


def _peers(pro, ts_code: str, sector: dict, income_c: list[dict]) -> dict | None:
    """同行本期和去年同期的数据：单季营收、单季归母，和累计扣非。整个市场一次取回，再按行业成分股筛。"""
    members = set(sector.get("members") or []) - {ts_code}
    ends = sorted({str(r.get("end_date")) for r in income_c if r.get("end_date")})
    if not ends or len(members) < 5:
        return None
    end = ends[-1]
    before = f"{int(end[:4]) - 1}{end[4:]}"
    out = {"period": end, "members": len(members), "cur": {}, "prior": {}}
    for tag, period in (("cur", end), ("prior", before)):
        quarterly = _call(lambda: pro.income_vip(period=period, report_type="2", fields="ts_code,revenue,n_income_attr_p"))
        deducted = _call(lambda: pro.fina_indicator_vip(period=period, fields="ts_code,profit_dedt"))
        rows: dict[str, dict] = {}
        for r in _records(quarterly):
            if r["ts_code"] in members:
                rows.setdefault(r["ts_code"], {}).update(rev=r.get("revenue"), np=r.get("n_income_attr_p"))
        for r in _records(deducted):
            if r["ts_code"] in members:
                rows.setdefault(r["ts_code"], {})["ded_cum"] = r.get("profit_dedt")
        out[tag] = rows
    return out


def _call(fn):
    try:
        return fn()
    except Exception:
        return None


def _records(frame) -> list[dict]:
    if frame is None or getattr(frame, "empty", True):
        return []
    rows = []
    for record in frame.to_dict(orient="records"):
        rows.append({key: _plain(value) for key, value in record.items()})
    return rows


def _plain(value):
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if hasattr(value, "item"):
        try:
            return _plain(value.item())
        except Exception:
            pass
    return value
