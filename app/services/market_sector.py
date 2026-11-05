"""板块：钱、价、热度、杠杆放在一起看。

覆盖申万 31 个一级行业：
- 有合适 ETF（近 20 日日均成交 ≥ 1 亿、规模 ≥ 20 亿）的主题，用 ETF：钱 = 份额变化 × 收盘价，价和热度也取 ETF 自己的。
- 没有合适 ETF 的行业，用申万行业指数的价格和成交额，钱用东方财富行业主力资金近几日净流入代替，页面上写明「估算」。
每个板块的「钱」不拿绝对金额比（大 ETF 和小 ETF 没法比），而是看近 5 日净流入比它自己平时 5 日净流入多了几倍的波动。
"""

from __future__ import annotations

from datetime import date

from app.services import market_rules as rules
from app.services import market_tape as tape

WINDOWS = (1, 3, 5, 30, 60)
CONCLUSION_DAYS = 5      # 板块结论用近 5 个交易日，和成交、宽基同一档
HISTORY = 60             # 拿自己前 60 天的 5 日净流入当「平时」
RS_DAYS = (5, 20)

# 名称、大类、代表 ETF、能代表的申万一级行业、申万二三级名字里带这些词的股票归它、是不是这个一级行业的默认板块
UNIVERSE = (
    ("银行", "金融", "512800.SH", ("银行",), (), True),
    ("券商", "金融", "512880.SH", ("非银金融",), ("证券",), True),
    ("保险", "金融", "512070.SH", ("非银金融",), ("保险",), False),
    ("红利", "稳定", "510880.SH", (), (), False),
    ("电力公用", "稳定", "159611.SZ", ("公用事业",), (), True),
    ("房地产", "稳定", "512200.SH", ("房地产",), (), True),
    ("白酒", "消费", "512690.SH", ("食品饮料",), ("白酒",), False),
    ("养殖农业", "消费", "159865.SZ", ("农林牧渔",), (), True),
    ("创新药", "医药", "159992.SZ", ("医药生物",), ("化学制药", "生物制品"), False),
    ("医疗", "医药", "512170.SH", ("医药生物",), ("医疗器械", "医疗服务"), False),
    ("医药", "医药", "512010.SH", ("医药生物",), (), True),
    ("芯片", "科技", "159995.SZ", ("电子",), ("半导体",), False),
    ("消费电子", "科技", "159732.SZ", ("电子",), ("消费电子",), False),
    ("软件", "科技", "515230.SH", ("计算机",), ("软件开发", "IT服务"), True),
    ("人工智能", "科技", "159819.SZ", (), (), False),
    ("通信", "科技", "515880.SH", ("通信",), (), True),
    ("传媒", "科技", "512980.SH", ("传媒",), (), True),
    ("游戏", "科技", "159869.SZ", ("传媒",), ("游戏",), False),
    ("机器人", "制造", "562500.SH", ("机械设备",), ("自动化设备",), False),
    ("电池", "制造", "159755.SZ", ("电力设备",), ("电池",), False),
    ("光伏", "制造", "515790.SH", ("电力设备",), ("光伏设备",), False),
    ("电网设备", "制造", "159326.SZ", ("电力设备",), ("电网设备", "风电设备", "其他电源设备"), True),
    ("军工", "制造", "512660.SH", ("国防军工",), (), True),
    ("有色金属", "周期", "512400.SH", ("有色金属",), (), True),
    ("黄金", "周期", "518880.SH", ("有色金属",), ("贵金属",), False),
    ("煤炭", "周期", "515220.SH", ("煤炭",), (), True),
    ("化工", "周期", "159870.SZ", ("基础化工",), (), True),
)
# 没有合适 ETF 的申万一级行业：名称就是行业名，大类、行业代码
BASE = (
    ("食品饮料", "消费", "801120.SI"), ("家用电器", "消费", "801110.SI"), ("纺织服饰", "消费", "801130.SI"),
    ("轻工制造", "消费", "801140.SI"), ("商贸零售", "消费", "801200.SI"), ("社会服务", "消费", "801210.SI"),
    ("美容护理", "消费", "801980.SI"), ("电子", "科技", "801080.SI"),
    ("机械设备", "制造", "801890.SI"), ("汽车", "制造", "801880.SI"), ("建筑装饰", "稳定", "801720.SI"),
    ("交通运输", "稳定", "801170.SI"), ("环保", "稳定", "801970.SI"), ("建筑材料", "周期", "801710.SI"),
    ("钢铁", "周期", "801040.SI"), ("石油石化", "周期", "801960.SI"), ("综合", "稳定", "801230.SI"),
)
L1_CODES = {
    "农林牧渔": "801010.SI", "基础化工": "801030.SI", "钢铁": "801040.SI", "有色金属": "801050.SI", "电子": "801080.SI",
    "家用电器": "801110.SI", "食品饮料": "801120.SI", "纺织服饰": "801130.SI", "轻工制造": "801140.SI", "医药生物": "801150.SI",
    "公用事业": "801160.SI", "交通运输": "801170.SI", "房地产": "801180.SI", "商贸零售": "801200.SI", "社会服务": "801210.SI",
    "综合": "801230.SI", "建筑材料": "801710.SI", "建筑装饰": "801720.SI", "电力设备": "801730.SI", "国防军工": "801740.SI",
    "计算机": "801750.SI", "传媒": "801760.SI", "通信": "801770.SI", "银行": "801780.SI", "非银金融": "801790.SI",
    "机械设备": "801890.SI", "汽车": "801880.SI", "煤炭": "801950.SI", "石油石化": "801960.SI", "环保": "801970.SI", "美容护理": "801980.SI",
}
# 兼容旧用法：（名称，ETF，代表的第一个申万一级行业代码）
SECTORS = tuple((name, etf, L1_CODES.get((l1 or ("",))[0])) for name, _group, etf, l1, _kw, _default in UNIVERSE)
L1_NAMES = {code: name for name, code in L1_CODES.items()}


def _norm(name: str) -> str:
    return str(name or "").replace("Ⅱ", "").replace("Ⅲ", "").strip()


def etf_sector_for(l1: str, l2: str, l3: str = "") -> str | None:
    """这只股票归哪个有 ETF 的板块：申万二三级名字带关键词的优先（词越长越准），没有就看一级行业的默认板块。"""
    l2, l3, l1 = _norm(l2), _norm(l3), _norm(l1)
    best, size = None, 0
    for name, _group, _etf, _l1s, keywords, _default in UNIVERSE:
        for word in keywords:
            if (word in l2 or word in l3) and len(word) > size:
                best, size = name, len(word)
    if best:
        return best
    return next((name for name, _group, _etf, l1s, _kw, default in UNIVERSE if default and l1 in l1s), None)


def sector_for(l1: str, l2: str, l3: str = "") -> str | None:
    """先找有 ETF 的板块；没有，就是这个一级行业自己的板块（没有代表 ETF 的那 17 个）。"""
    found = etf_sector_for(l1, l2, l3)
    if found:
        return found
    base = {name for name, _group, _code in BASE}
    return _norm(l1) if _norm(l1) in base else None


def coverage() -> dict:
    """申万 31 个一级行业各由哪个板块覆盖：有 ETF 的（含默认归属和关键词归属）、只能估算的。"""
    out = {}
    for l1 in L1_CODES:
        etf = [name for name, _g, _e, l1s, kw, default in UNIVERSE if l1 in l1s]
        out[l1] = {"etf": etf, "default": etf_sector_for(l1, ""), "estimated": l1 in {name for name, _g, _c in BASE}}
    return out


# ---------------------------------------------------------------- 取数后的计算（纯函数，方便测试）
def flows(shares: list[dict], closes: list[dict]) -> dict[int, float | None]:
    """各窗口的净申购金额（亿元）：每天的份额变化（万份）× 当天收盘价 ÷ 1 万，再累加。"""
    daily = [value for _day, value in daily_flows(shares, closes)]
    return {n: (sum(daily[-n:]) if len(daily) >= n else None) for n in WINDOWS}


SPLIT_SHARE, SPLIT_VALUE = 0.30, 0.15   # 份额一天变 30% 以上，而份额 × 价格几乎没变（差不到 15%）：是折算（拆分、合并），不是申赎


def daily_flows(shares: list[dict], closes: list[dict]) -> list[tuple[str, float]]:
    """每天的净申购金额（亿元）：份额变化（万份）× 当天收盘价 ÷ 1 万。份额折算那天不算（否则会冒出一笔虚假的巨额申购）。"""
    rows = sorted(
        [row for row in shares if tape.num(row.get("fd_share")) is not None],
        key=lambda row: tape.ymd(row.get("trade_date")),
    )
    price = {tape.ymd(row.get("trade_date")): tape.num(row.get("close")) for row in closes}
    out = []
    for prev, row in zip(rows, rows[1:]):
        day = tape.ymd(row.get("trade_date"))
        close, before = price.get(day), price.get(tape.ymd(prev.get("trade_date")))
        if close is None:
            continue
        old, new = tape.num(prev.get("fd_share")), tape.num(row.get("fd_share"))
        if old and before and abs(new / old - 1) >= SPLIT_SHARE and abs((new / old) * (close / before) - 1) <= SPLIT_VALUE:
            out.append((day, 0.0))
            continue
        out.append((day, (new - old) * close / 10000))
    return out


def heat(shares: list[dict], closes: list[dict]) -> float | None:
    """今天换手率 ÷ 近一年平均换手率。换手率 = 成交量（手）× 100 ÷ 份额（万份 × 1 万）。"""
    held = sorted(
        [(tape.ymd(row.get("trade_date")), tape.num(row.get("fd_share"))) for row in shares if tape.num(row.get("fd_share"))],
    )
    if not held:
        return None
    rates = []
    pointer = 0
    current = held[0][1]
    for row in sorted(closes, key=lambda item: tape.ymd(item.get("trade_date"))):
        day = tape.ymd(row.get("trade_date"))
        while pointer < len(held) and held[pointer][0] <= day:
            current = held[pointer][1]
            pointer += 1
        vol = tape.num(row.get("vol"))
        if vol is None or not current:
            continue
        rates.append(vol * 100 / (current * 10000))
    if len(rates) < 60:
        return None
    base = sum(rates[-250:]) / len(rates[-250:])
    return rates[-1] / base if base else None


def etf_series(shares: list[dict], bars: list[dict]) -> dict:
    """ETF 的每日净申购（亿元）、收盘价、成交额，以及 5 日前的规模（亿元，算占规模多少）。"""
    daily = daily_flows(shares, bars)
    ordered = sorted([row for row in bars if tape.num(row.get("close")) is not None], key=lambda row: tape.ymd(row.get("trade_date")))
    held = sorted((tape.ymd(row.get("trade_date")), tape.num(row.get("fd_share"))) for row in shares if tape.num(row.get("fd_share")) is not None)
    size = None
    if len(held) > CONCLUSION_DAYS and len(ordered) > CONCLUSION_DAYS:
        day = tape.ymd(ordered[-1 - CONCLUSION_DAYS].get("trade_date"))
        past = [value for stamp, value in held if stamp <= day]
        if past:
            size = past[-1] * tape.num(ordered[-1 - CONCLUSION_DAYS].get("close")) / 1e4
    return {
        "daily": daily, "closes": [(tape.ymd(row.get("trade_date")), tape.num(row.get("close"))) for row in ordered],
        "amounts": [(tape.ymd(row.get("trade_date")), tape.num(row.get("amount")) or 0.0) for row in ordered], "size": size,
    }


def rolling_sums(values: list[float], n: int) -> list[float]:
    return [sum(values[i - n + 1:i + 1]) for i in range(n - 1, len(values))]


def money_z(daily: list[float], n: int = CONCLUSION_DAYS, history: int = HISTORY) -> float | None:
    """最近 n 天净流入，比自己前 history 个滚动 n 日净流入多（少）几倍标准差。样本不够返回 None。"""
    if len(daily) < n + 30:
        return None
    sums = rolling_sums(daily, n)
    base = sums[-1 - history:-1] if len(sums) > history else sums[:-1]
    if len(base) < 25:
        return None
    avg = sum(base) / len(base)
    spread = (sum((value - avg) ** 2 for value in base) / len(base)) ** 0.5
    return None if spread == 0 else (sums[-1] - avg) / spread


def window_return(closes: list[tuple[str, float]], n: int) -> float | None:
    if len(closes) <= n or not closes[-1 - n][1]:
        return None
    return closes[-1][1] / closes[-1 - n][1] - 1


def amount_ratio(amounts: list[tuple[str, float]], recent: int = 5, base: int = 60) -> float | None:
    """近 recent 日日均成交额 ÷ 近 base 日日均成交额：板块放量了没有。"""
    values = [value for _day, value in amounts]
    if len(values) < base // 2 + recent:
        return None
    tail = values[-base:]
    mean_base = sum(tail) / len(tail)
    return None if not mean_base else (sum(values[-recent:]) / recent) / mean_base


def position(closes: list[tuple[str, float]], days: int = 60) -> float | None:
    """今天收盘在近 days 个交易日里排在哪：0 是最低，1 是最高。"""
    window = [price for _day, price in closes[-days:] if price]
    if len(window) < 20:
        return None
    return sum(1 for price in window if price <= window[-1]) / len(window)


def lever_by_l1(now_rows: list[dict], then_rows: list[dict], member: dict[str, str]) -> dict[str, float | None]:
    """各申万一级行业融资余额 30 个交易日变化，减去各行业变化的中位数：整个市场都在降（升）的时候，「降（升）」没有信息，
    要看比别的行业多降还是少降。"""
    def total(rows: list[dict]) -> dict[str, float]:
        out: dict[str, float] = {}
        for row in rows:
            value = tape.num(row.get("rzye"))
            name = member.get(str(row.get("ts_code") or ""))
            if value and name:
                out[name] = out.get(name, 0.0) + value
        return out

    now, then = total(now_rows), total(then_rows)
    growth = {name: tape.growth(now[name], then[name]) for name in L1_CODES if name in now and then.get(name)}
    if not growth:
        return {name: None for name in L1_CODES}
    ordered = sorted(growth.values())
    middle = ordered[len(ordered) // 2] if len(ordered) % 2 else (ordered[len(ordered) // 2 - 1] + ordered[len(ordered) // 2]) / 2
    return {name: ((1 + growth[name]) / (1 + middle) - 1 if name in growth else None) for name in L1_CODES}


def make_row(name: str, group: str, code: str, source: str, series: dict, bench: list[tuple[str, float]], lever_value: float | None,
             l1: str = "") -> dict:
    """把一个板块的几条序列拼成一行：钱、价、热度、位置、杠杆，再给出状态。"""
    daily = [value for _day, value in series["daily"]]
    flow = {n: (sum(daily[-n:]) if len(daily) >= n else None) for n in WINDOWS}
    closes = series["closes"]
    ret = {n: window_return(closes, n) for n in RS_DAYS}
    base = {n: window_return(bench, n) for n in RS_DAYS}
    excess = {n: (None if ret[n] is None or base[n] is None else ret[n] - base[n]) for n in RS_DAYS}
    size = series.get("size")
    rate = None if size in (None, 0) or flow[CONCLUSION_DAYS] is None else flow[CONCLUSION_DAYS] / size
    mz = money_z(daily)
    mz3 = money_z(daily, 3)
    ratio = amount_ratio(series["amounts"])
    heat3 = amount_ratio(series["amounts"], recent=3)
    pos = position(closes)
    layers = rules.sector_phase(mz, excess[CONCLUSION_DAYS], ratio, pos, rules.lever_word(lever_value), rate)
    return {
        "name": name, "group": group, "code": code, "source": source, "industry": l1,
        "flow": flow, "flow_rate": rate, "size": size, "money_z": mz, "money_z3": mz3, "heat3": heat3,
        "ret5": ret[5], "ret20": ret[20], "ex5": excess[5], "ex20": excess[20],
        "heat": ratio, "position": pos, "lever": lever_value, "lever_word": rules.lever_word(lever_value),
        "verdict": layers["verdict"], "phase": layers["phase"], "tone": layers["tone"], "bucket": layers["bucket"], "why": layers["why"],
    }


# ---------------------------------------------------------------- 取数
def _bench(today: date) -> list[tuple[str, float]]:
    start = tape._ymd_offset(today, 400)
    rows = tape.cached(f"holdidx:{start}", lambda: tape.pro().index_daily(
        ts_code="000300.SH", start_date=start, end_date=tape._ymd(today), fields="trade_date,close",
    ))
    return sorted((tape.ymd(row.get("trade_date")), tape.num(row.get("close"))) for row in rows if tape.num(row.get("close")))


def _lever(today: date) -> dict[str, float | None]:
    days = tape.open_dates(today, 32)
    if len(days) < 31:
        return {}
    now_rows = _margin_rows(days[-1]) or _margin_rows(days[-2])
    then_rows = _margin_rows(days[-31])
    if not now_rows or not then_rows:
        return {}
    return lever_by_l1(now_rows, then_rows, tape._member_map())


def lever(today: date) -> dict[str, float | None]:
    """兼容旧接口：{申万一级行业代码: 融资余额 30 个交易日变化}。"""
    by_name = _lever(today)
    return {code: by_name.get(name) for name, code in L1_CODES.items()}


def _margin_rows(day: str) -> list[dict]:
    return tape.cached(f"margin_detail:{day}", lambda: tape.pro().margin_detail(trade_date=day, fields="ts_code,rzye,rzmre,rzche"))


def _industry_daily(today: date, names: list[str]) -> dict[str, list[tuple[str, float]]]:
    """东方财富行业主力资金，每天一份（过去的天落盘），取出这几个行业近 HISTORY+10 个交易日的净流入（亿元）。"""
    days = tape.open_dates(today, HISTORY + 10)
    loaded = tape._parallel(days, lambda day: tape.cached(f"indflow:{day}", lambda d=day: tape.pro().moneyflow_ind_dc(trade_date=d, content_type="行业")))
    out: dict[str, list[tuple[str, float]]] = {name: [] for name in names}
    for day, rows in zip(days, loaded):
        by_name = {str(row.get("name")): tape.num(row.get("net_amount")) for row in rows or []}
        for name in names:
            value = by_name.get(name)
            if value is not None:
                out[name].append((day, value / 1e8))
    return out


def _sw_series(today: date, code: str) -> dict:
    start = tape._ymd_offset(today, 400)
    rows = tape.cached(f"swidx:{code}:{start}", lambda: tape.pro().sw_daily(
        ts_code=code, start_date=start, end_date=tape._ymd(today), fields="trade_date,close,amount",
    ))
    ordered = sorted([row for row in rows if tape.num(row.get("close"))], key=lambda row: tape.ymd(row.get("trade_date")))
    return {
        "closes": [(tape.ymd(row.get("trade_date")), tape.num(row.get("close"))) for row in ordered],
        "amounts": [(tape.ymd(row.get("trade_date")), tape.num(row.get("amount")) or 0.0) for row in ordered],
    }


def build(today: date) -> dict:
    etfs = [etf for _name, _group, etf, _l1, _kw, _default in UNIVERSE]
    share_rows = dict(zip(etfs, tape._parallel(etfs, lambda code: tape.fund_share_rows(code, today))))
    bar_rows = dict(zip(etfs, tape._parallel(etfs, lambda code: tape.fund_bar_rows(code, today))))
    bench = _bench(today)
    levers = _lever(today)
    rows = []
    latest_share_day = ""
    for name, group, etf, l1s, _kw, _default in UNIVERSE:
        series = etf_series(share_rows[etf], bar_rows[etf])
        for row in share_rows[etf]:
            latest_share_day = max(latest_share_day, tape.ymd(row.get("trade_date")))
        rows.append(make_row(name, group, etf, "etf", series, bench, levers.get(l1s[0]) if len(l1s) == 1 else None, "、".join(l1s)))
    names = [name for name, _group, _code in BASE]
    industry = _industry_daily(today, names)
    for name, group, code in BASE:
        sw = _sw_series(today, code)
        rows.append(make_row(name, group, code, "industry", {**sw, "daily": industry.get(name, []), "size": None}, bench, levers.get(name), name))
    return {
        "rows": rows, "date": tape.iso(latest_share_day), "windows": list(WINDOWS), "conclusion_days": CONCLUSION_DAYS,
        "coverage": {"l1": len(L1_CODES), "etf": len(UNIVERSE), "estimated": len(BASE)},
    }


def catalysts(now=None, names: list[str] | None = None) -> dict[str, dict]:
    """每个板块最近 3 天的一条新闻线索。东方财富搜索，标题里要带板块名；没有就空着，不凑。只查有 ETF 的主题，并发查。"""
    from concurrent.futures import ThreadPoolExecutor
    from datetime import datetime, timedelta

    from app.services.news import fetch_search_news

    now = now or datetime.now()
    targets = names or [name for name, *_rest in UNIVERSE]

    def one(name: str):
        try:
            items = fetch_search_news(f"{name}板块", 8)
        except Exception:
            return name, None
        for item in sorted(items, key=lambda row: str(row.get("time") or ""), reverse=True):
            stamp = str(item.get("time") or "")[:16]
            title = str(item.get("title") or "")
            try:
                when = datetime.strptime(stamp[:10], "%Y-%m-%d")
            except ValueError:
                continue
            if name in title and now - when <= timedelta(days=3):
                return name, {"title": title, "time": stamp, "url": item.get("url") or ""}
        return name, None

    with ThreadPoolExecutor(max_workers=4) as pool:
        return {name: found for name, found in pool.map(one, targets) if found}
