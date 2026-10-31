"""把财报接口的结果改成 Agent 能逐期阅读的记录。报表金额单位是元。"""

from __future__ import annotations

from app.services.financial import report_desc


def to_rows(payload: dict | None) -> dict:
    payload = payload or {}
    statements = payload.get("financial_statements") or {}
    income = _index(statements.get("income"))
    balance = _index(statements.get("balance"))
    cashflow = _index(statements.get("cashflow"))
    indicator = _index(statements.get("indicator"))
    disclosure = _index(payload.get("disclosure_info"))
    order = []
    for bucket in (income, balance, cashflow, indicator):
        for period in bucket:
            if period not in order:
                order.append(period)
    unit = "亿港元" if (payload or {}).get("currency") == "HKD" else "亿元"
    rows = []
    for period in order:
        summary = _sentence(
            period,
            income.get(period) or {},
            balance.get(period) or {},
            cashflow.get(period) or {},
            indicator.get(period) or {},
            disclosure.get(period) or {},
            income.get(_last_year(period)) or {},
            unit,
        )
        if not summary:
            continue
        rows.append({
            "period": report_desc(period),
            "kind": "财报",
            "summary": summary,
        })
    return {"rows": rows, "pct": _pct((payload.get("valuation") or {}).get("pct_chg"))}


def latest_change(payload: dict | None) -> str:
    if not payload:
        return ""
    pct = payload.get("pct")
    if pct:
        return str(pct)
    return ""


def _index(rows) -> dict[str, dict]:
    indexed = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        period = _period(row.get("report_period"))
        if period and period not in indexed:
            indexed[period] = row
    return indexed


def _period(value) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits[:8]


def _last_year(period: str) -> str:
    if len(period) < 8:
        return ""
    return f"{int(period[:4]) - 1}{period[4:8]}"


def _date(period: str) -> str:
    if len(period) < 8:
        return ""
    return f"{period[:4]}-{period[4:6]}-{period[6:8]}"


def _pct(value) -> str:
    if value is None or value == "":
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if number != number:
        return ""
    return f"{number:+.2f}%"


def _percent(value) -> str:
    if value is None or value == "":
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if number != number:
        return ""
    return f"{number:.2f}%"


def _money(value, unit: str = "亿元") -> str:
    if value is None or value == "":
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if number != number:
        return ""
    sign = "-" if number < 0 else ""
    amount = abs(number)
    if amount >= 100000000:
        return f"{sign}{amount / 100000000:.2f} {unit}"
    if amount >= 10000:
        return f"{sign}{amount / 10000:.2f} 万元"
    return f"{sign}{amount:.2f} 元"


def _eps(value) -> str:
    if value is None or value == "":
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if number != number:
        return ""
    return f"{number:.2f} 元"


def _yoy(current, previous) -> str:
    try:
        now = float(current)
        before = float(previous)
    except (TypeError, ValueError):
        return ""
    if before <= 0 or now != now or before != before:
        return ""
    rate = (now - before) / before * 100
    if rate > 0:
        return f"同比增长 {rate:.2f}%"
    if rate < 0:
        return f"同比下降 {abs(rate):.2f}%"
    return "同比持平"


def _metric(label: str, shown: str, current=None, previous=None) -> str:
    if not shown:
        return ""
    change = _yoy(current, previous) if previous is not None else ""
    if change:
        return f"{label} {shown}，{change}"
    return f"{label} {shown}"


def _sentence(period, income, balance, cashflow, indicator, disclosure, last_year, unit: str = "亿元") -> str:
    parts = []
    revenue = _metric(
        "营业收入",
        _money(income.get("total_revenue"), unit),
        income.get("total_revenue"),
        last_year.get("total_revenue"),
    )
    profit = _metric(
        "归母净利润",
        _money(income.get("net_profit_parent"), unit),
        income.get("net_profit_parent"),
        last_year.get("net_profit_parent"),
    )
    eps = _metric("基本每股收益", _eps(income.get("basic_eps")))
    for item in (revenue, profit, eps):
        if item:
            parts.append(item)
    ratios = []
    for label, key in (
        ("净资产收益率", "roe"),
        ("毛利率", "gross_profit_margin"),
        ("净利率", "net_profit_margin"),
        ("资产负债率", "debt_to_assets"),
    ):
        shown = _percent(indicator.get(key))
        if shown:
            ratios.append(f"{label} {shown}")
    cash = _money(cashflow.get("operate_cash_flow"), unit)
    if cash:
        ratios.append(f"经营活动现金流净额 {cash}")
    assets = _money(balance.get("total_assets"), unit)
    liabilities = _money(balance.get("total_liabilities"), unit)
    if assets:
        ratios.append(f"总资产 {assets}")
    if liabilities:
        ratios.append(f"总负债 {liabilities}")
    if not parts and not ratios:
        return ""
    announced = _date(_period(disclosure.get("ann_date")))
    header = f"{report_desc(period)}，报告期 {_date(period)}"
    if announced:
        header += f"，公告日 {announced}"
    body = "；".join(parts)
    ratios_text = "，".join(ratios)
    if body and ratios_text:
        text = f"{header}：{body}。{ratios_text}"
    elif body:
        text = f"{header}：{body}"
    else:
        text = f"{header}：{ratios_text}"
    return text + "。"
