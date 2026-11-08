"""板块：钱、价、热度、杠杆放在一起看。

覆盖申万 31 个一级行业，外加一组常看的东财题材：
- 有合适 ETF 的主题：钱看日度「净流入÷前日规模」的 3 日累计 Modified Z-Score；价/热度取 ETF。
- 没有合适 ETF 的行业：价/热度用申万行业指数，钱用「主力净流入÷成交额」的同口径 Mz（门槛更严）；页面写明「估算」。
- 题材（CPO / PCB 等）：价/热度用东财概念板指数，钱用概念主力净流入÷成交额的同口径 Mz；归入「题材」分组。
- Mz 与全页一致：0.6745 × (累计 − n×M) / (MAD×√n)，M/MAD 取窗口前 60 个日度点。
- 同指数多只 ETF 合并算钱；新上市 20 个交易日内不计钱；份额折算日剔除。
状态：先价×热定行情，再钱定性；连续 2 日满足才切换。
"""

from __future__ import annotations

import math
from datetime import date

from app.services import market_rules as rules
from app.services import market_tape as tape

WINDOWS = (1, 3, 5, 30, 60)
CONCLUSION_DAYS = 3      # 钱 / 价 / 热 的结论窗口
HISTORY = 60
RS_DAYS = (3, 5, 20)
LIST_SKIP = 20           # 新上市这么多交易日内不计钱
PHASE_HISTORY = 12        # 回溯天数，供 2 日确认与刚启动/可能在退

# 名称、大类、代表 ETF（同指数可多只，合并算钱）、能代表的申万一级、二三级关键词、是否一级默认板块
UNIVERSE = (
    ("银行", "金融", ("512800.SH",), ("银行",), (), True),
    ("券商", "金融", ("512880.SH",), ("非银金融",), ("证券",), True),
    ("保险", "金融", ("512070.SH",), ("非银金融",), ("保险",), False),
    ("红利", "稳定", ("510880.SH",), (), (), False),
    ("电力公用", "稳定", ("159611.SZ",), ("公用事业",), (), True),
    ("房地产", "稳定", ("512200.SH",), ("房地产",), (), True),
    ("白酒", "消费", ("512690.SH",), ("食品饮料",), ("白酒",), False),
    ("养殖农业", "消费", ("159865.SZ",), ("农林牧渔",), (), True),
    ("创新药", "医药", ("159992.SZ",), ("医药生物",), ("化学制药", "生物制品"), False),
    ("医疗", "医药", ("512170.SH",), ("医药生物",), ("医疗器械", "医疗服务"), False),
    ("医药", "医药", ("512010.SH",), ("医药生物",), (), True),
    ("芯片", "科技", ("159995.SZ",), ("电子",), ("半导体",), False),
    ("消费电子", "科技", ("159732.SZ",), ("电子",), ("消费电子",), False),
    ("软件", "科技", ("515230.SH",), ("计算机",), ("软件开发", "IT服务"), True),
    ("人工智能", "科技", ("159819.SZ",), (), (), False),
    ("通信", "科技", ("515880.SH",), ("通信",), (), True),
    ("传媒", "科技", ("512980.SH",), ("传媒",), (), True),
    ("游戏", "科技", ("159869.SZ",), ("传媒",), ("游戏",), False),
    ("机器人", "制造", ("562500.SH",), ("机械设备",), ("自动化设备",), False),
    ("电池", "制造", ("159755.SZ",), ("电力设备",), ("电池",), False),
    ("光伏", "制造", ("515790.SH",), ("电力设备",), ("光伏设备",), False),
    ("电网设备", "制造", ("159326.SZ",), ("电力设备",), ("电网设备", "风电设备", "其他电源设备"), True),
    ("军工", "制造", ("512660.SH",), ("国防军工",), (), True),
    ("有色金属", "周期", ("512400.SH",), ("有色金属",), (), True),
    ("黄金", "周期", ("518880.SH",), ("有色金属",), ("贵金属",), False),
    ("煤炭", "周期", ("515220.SH",), ("煤炭",), (), True),
    ("化工", "周期", ("159870.SZ",), ("基础化工",), (), True),
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
# 常看题材：展示名、大类、东财概念板代码、资金流接口里的板块名（content_type=概念）
CONCEPTS = (
    ("CPO", "题材", "BK1128.DC", "CPO概念"),
    ("PCB", "题材", "BK0877.DC", "PCB"),
    ("光模块", "题材", "BK1136.DC", "光通信模块"),
    ("液冷", "题材", "BK1138.DC", "液冷服务器"),
    ("存储芯片", "题材", "BK1137.DC", "存储芯片"),
    ("铜缆高速连接", "题材", "BK1168.DC", "铜缆高速连接"),
    ("先进封装", "题材", "BK1101.DC", "先进封装"),
    ("算力", "题材", "BK1134.DC", "算力概念"),
)
L1_CODES = {
    "农林牧渔": "801010.SI", "基础化工": "801030.SI", "钢铁": "801040.SI", "有色金属": "801050.SI", "电子": "801080.SI",
    "家用电器": "801110.SI", "食品饮料": "801120.SI", "纺织服饰": "801130.SI", "轻工制造": "801140.SI", "医药生物": "801150.SI",
    "公用事业": "801160.SI", "交通运输": "801170.SI", "房地产": "801180.SI", "商贸零售": "801200.SI", "社会服务": "801210.SI",
    "综合": "801230.SI", "建筑材料": "801710.SI", "建筑装饰": "801720.SI", "电力设备": "801730.SI", "国防军工": "801740.SI",
    "计算机": "801750.SI", "传媒": "801760.SI", "通信": "801770.SI", "银行": "801780.SI", "非银金融": "801790.SI",
    "机械设备": "801890.SI", "汽车": "801880.SI", "煤炭": "801950.SI", "石油石化": "801960.SI", "环保": "801970.SI", "美容护理": "801980.SI",
}
# 兼容旧用法：（名称，主 ETF，代表的第一个申万一级行业代码）
SECTORS = tuple((name, etfs[0], L1_CODES.get((l1 or ("",))[0])) for name, _group, etfs, l1, _kw, _default in UNIVERSE)
L1_NAMES = {code: name for name, code in L1_CODES.items()}


def _norm(name: str) -> str:
    return str(name or "").replace("Ⅱ", "").replace("Ⅲ", "").strip()


def _etfs_of(row) -> tuple[str, ...]:
    etfs = row[2]
    return etfs if isinstance(etfs, tuple) else (etfs,)


def etf_sector_for(l1: str, l2: str, l3: str = "") -> str | None:
    """这只股票归哪个有 ETF 的板块：申万二三级名字带关键词的优先（词越长越准），没有就看一级行业的默认板块。"""
    l2, l3, l1 = _norm(l2), _norm(l3), _norm(l1)
    best, size = None, 0
    for name, _group, _etfs, _l1s, keywords, _default in UNIVERSE:
        for word in keywords:
            if (word in l2 or word in l3) and len(word) > size:
                best, size = name, len(word)
    if best:
        return best
    return next((name for name, _group, _etfs, l1s, _kw, default in UNIVERSE if default and l1 in l1s), None)


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
        etf = [name for name, _g, _etfs, l1s, kw, default in UNIVERSE if l1 in l1s]
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
    """ETF 的每日净申购（亿元）、收盘价、成交额、逐日规模。"""
    daily = daily_flows(shares, bars)
    ordered = sorted([row for row in bars if tape.num(row.get("close")) is not None], key=lambda row: tape.ymd(row.get("trade_date")))
    held = sorted((tape.ymd(row.get("trade_date")), tape.num(row.get("fd_share"))) for row in shares if tape.num(row.get("fd_share")) is not None)
    held_map = {day: share for day, share in held}
    closes = [(tape.ymd(row.get("trade_date")), tape.num(row.get("close"))) for row in ordered]
    amounts = [(tape.ymd(row.get("trade_date")), tape.num(row.get("amount")) or 0.0) for row in ordered]
    # 规模与 daily 对齐：每个净申购日用当日收盘份额×价
    size = None
    sizes: list[float | None] = []
    for day, _flow in daily:
        share = held_map.get(day)
        price = next((p for d, p in closes if d == day), None)
        if share is not None and price:
            value = share * price / 1e4
            sizes.append(value)
            size = value
        else:
            sizes.append(None)
    if len(held) > CONCLUSION_DAYS and len(ordered) > CONCLUSION_DAYS:
        day = tape.ymd(ordered[-1 - CONCLUSION_DAYS].get("trade_date"))
        past = [value for stamp, value in held if stamp <= day]
        if past:
            size = past[-1] * tape.num(ordered[-1 - CONCLUSION_DAYS].get("close")) / 1e4
    return {
        "daily": daily, "closes": closes, "amounts": amounts, "size": size, "sizes": sizes, "shares": held,
        "listed": len(ordered),
    }


def rolling_sums(values: list[float], n: int) -> list[float]:
    return [sum(values[i - n + 1:i + 1]) for i in range(n - 1, len(values))]


def money_z(daily: list[float], n: int = CONCLUSION_DAYS, history: int = HISTORY) -> float | None:
    """兼容旧测试：滚动 n 日净流入相对自身历史的普通 Z 分。新口径请用 money_score。"""
    if len(daily) < n + 30:
        return None
    sums = rolling_sums(daily, n)
    base = sums[-1 - history:-1] if len(sums) > history else sums[:-1]
    if len(base) < 25:
        return None
    avg = sum(base) / len(base)
    spread = (sum((value - avg) ** 2 for value in base) / len(base)) ** 0.5
    return None if spread == 0 else (sums[-1] - avg) / spread


def _rate_mz(rates: list[float], n: int = CONCLUSION_DAYS, history: int = HISTORY) -> float | None:
    """日度比率序列的 n 日累计 Modified Z-Score：对照窗口前 history 日的 M / MAD，中心×n，MAD×√n。"""
    if len(rates) < n + min(history, tape.BASELINE_DAYS):
        return None
    cum = sum(rates[-n:])
    prior = rates[-n - history:-n]
    med, mad, enough = tape._baseline(prior)
    if not enough or med is None or not mad:
        return None
    return tape.modified_z(cum, n * med, mad * math.sqrt(n))


def money_score_etf(daily: list[float], sizes: list[float | None], n: int = CONCLUSION_DAYS,
                    history: int = HISTORY, listed: int | None = None) -> float | None:
    """ETF：日度净流入÷前日规模，再对累计做 Modified Z-Score。"""
    if listed is not None and listed < LIST_SKIP:
        return None
    if len(daily) < n + 20 or len(sizes) < len(daily):
        return None
    rates = []
    for i in range(1, len(daily)):
        prev = sizes[i - 1]
        if prev is None or prev <= 0:
            continue
        rates.append(daily[i] / prev)
    return _rate_mz(rates, n, history)


def money_score_base(daily: list[float], amounts: list[float], n: int = CONCLUSION_DAYS,
                     history: int = HISTORY) -> float | None:
    """BASE：日度主力净流入÷成交额，再对累计做 Modified Z-Score。"""
    if len(daily) < n + 20 or len(amounts) != len(daily):
        return None
    rates = []
    for flow, amount in zip(daily, amounts):
        if amount and amount > 0:
            rates.append(flow / amount)
    return _rate_mz(rates, n, history)


def window_return(closes: list[tuple[str, float]], n: int) -> float | None:
    if len(closes) <= n or not closes[-1 - n][1]:
        return None
    return closes[-1][1] / closes[-1 - n][1] - 1


def price_score(closes: list[tuple[str, float]], bench: list[tuple[str, float]], n: int = CONCLUSION_DAYS,
                history: int = HISTORY) -> tuple[float | None, float | None]:
    """返回 (超额 Mz, 原始 n 日超额)。相对沪深300 的日度超额累计后按 Modified Z-Score。"""
    by_bench = {day: price for day, price in bench}
    aligned = [(day, price, by_bench[day]) for day, price in closes if day in by_bench and price and by_bench[day]]
    if len(aligned) < n + 25:
        return None, window_return(closes, n) if len(closes) > n else None
    daily_ex = []
    for i in range(1, len(aligned)):
        _day, price, bprice = aligned[i]
        prev_p, prev_b = aligned[i - 1][1], aligned[i - 1][2]
        if prev_p and prev_b:
            daily_ex.append(price / prev_p - 1 - (bprice / prev_b - 1))
    if len(daily_ex) < n:
        return None, None
    ex_n = sum(daily_ex[-n:])
    return _rate_mz(daily_ex, n, history), ex_n


def amount_ratio(amounts: list[tuple[str, float]], recent: int = 5, base: int = 60) -> float | None:
    """近 recent 日日均 ÷ 此前 base 日日均（不含这 recent 天）。"""
    values = [value for _day, value in amounts]
    if len(values) < base + recent:
        return None
    prior = values[-(base + recent):-recent]
    mean_base = sum(prior) / len(prior) if prior else 0.0
    return None if not mean_base else (sum(values[-recent:]) / recent) / mean_base


def heat_relative(sector_amounts: list[tuple[str, float]], bench_amounts: list[tuple[str, float]],
                  recent: int = 5, base: int = 60) -> float | None:
    """(板块 recent/前 base) ÷ (沪深300 同口径)。"""
    sec = amount_ratio(sector_amounts, recent, base)
    mkt = amount_ratio(bench_amounts, recent, base)
    if sec is None or mkt is None or not mkt:
        return None
    return sec / mkt


def position(closes: list[tuple[str, float]], days: int = 60) -> float | None:
    """今天收盘在近 days 个交易日里排在哪：0 是最低，1 是最高。"""
    window = [price for _day, price in closes[-days:] if price]
    if len(window) < 20:
        return None
    return sum(1 for price in window if price <= window[-1]) / len(window)


def merge_etf_series(parts: list[dict]) -> dict:
    """同指数多只 ETF：钱和规模按日相加，价/成交取规模最大的那只。"""
    if not parts:
        return {"daily": [], "closes": [], "amounts": [], "size": None, "sizes": []}
    if len(parts) == 1:
        return parts[0]
    days = sorted({day for part in parts for day, _ in part.get("daily") or []})
    flow_by = {day: 0.0 for day in days}
    size_by: dict[str, float] = {}
    for part in parts:
        for day, value in part.get("daily") or []:
            flow_by[day] = flow_by.get(day, 0.0) + value
        held = {day: share for day, share in (part.get("shares") or [])}
        close = {day: price for day, price in (part.get("closes") or [])}
        for day in days:
            if day in held and day in close:
                size_by[day] = size_by.get(day, 0.0) + held[day] * close[day] / 1e4
    primary = max(parts, key=lambda part: (part.get("size") or 0.0))
    sizes = [size_by.get(day) for day, _ in sorted(flow_by.items())]
    return {
        "daily": sorted(flow_by.items()),
        "closes": primary.get("closes") or [],
        "amounts": primary.get("amounts") or [],
        "size": primary.get("size"),
        "sizes": sizes,
        "shares": primary.get("shares") or [],
    }


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


def _slice_series(series: dict, end: int) -> dict:
    """截到 daily 的第 end 个点（含），closes/amounts 按日期对齐到这一天。"""
    daily = series["daily"][: end + 1]
    if not daily:
        return {**series, "daily": [], "sizes": [], "closes": [], "amounts": []}
    last = daily[-1][0]
    closes = [(d, p) for d, p in series["closes"] if d <= last]
    amounts = [(d, a) for d, a in series["amounts"] if d <= last]
    sizes = (series.get("sizes") or [])[: end + 1]
    return {**series, "daily": daily, "closes": closes, "amounts": amounts, "sizes": sizes,
            "listed": len(closes), "size": next((s for s in reversed(sizes) if s), series.get("size"))}


def _scores_at(series: dict, bench_closes: list[tuple[str, float]], bench_amounts: list[tuple[str, float]],
               source: str) -> tuple[float | None, float | None, float | None, float | None, float | None]:
    """返回 mz, price_z, 结论窗口超额, heat, rate。"""
    daily = [value for _day, value in series["daily"]]
    closes = series["closes"]
    amounts_sec = series["amounts"]
    size = series.get("size")
    flow_n = sum(daily[-CONCLUSION_DAYS:]) if len(daily) >= CONCLUSION_DAYS else None
    rate = None if size in (None, 0) or flow_n is None else flow_n / size
    if source == "etf":
        mz = money_score_etf(daily, series.get("sizes") or [], listed=series.get("listed"))
    else:
        # industry / concept：主力净流入 ÷ 成交额
        amt_vals = []
        amount_map = {d: a for d, a in amounts_sec}
        for day, _flow in series["daily"]:
            amt_vals.append(amount_map.get(day) or 0.0)
        mz = money_score_base(daily, amt_vals)
    price_z, ex_n = price_score(closes, bench_closes)
    heat = heat_relative(amounts_sec, bench_amounts, recent=CONCLUSION_DAYS)
    return mz, price_z, ex_n, heat, rate


def make_row(name: str, group: str, code: str, source: str, series: dict, bench_closes: list[tuple[str, float]],
             bench_amounts: list[tuple[str, float]], lever_value: float | None, l1: str = "") -> dict:
    """把一个板块的几条序列拼成一行：新口径钱/价/热，2 日确认状态，刚启动/可能在退。"""
    daily_all = series["daily"]
    if len(daily_all) < CONCLUSION_DAYS + 5:
        return {
            "name": name, "group": group, "code": code, "source": source, "industry": l1,
            "flow": {n: None for n in WINDOWS}, "flow_rate": None, "size": series.get("size"), "money_z": None,
            "ret5": None, "ret20": None, "ex5": None, "ex20": None, "heat": None, "position": None,
            "lever": lever_value, "lever_word": rules.lever_word(lever_value),
            "verdict": None, "phase": "没数据", "tone": "flat", "bucket": "flat", "why": "历史数据不够，判不了。",
            "weather": "", "price_z": None, "mz_hist": [], "price_z_hist": [],
            "started": False, "fading": False, "bucket_hist": [],
        }
    enter = rules.MONEY_ENTER if source == "etf" else rules.MONEY_ENTER_BASE
    hold = rules.MONEY_HOLD if source == "etf" else enter / 2
    raw = []
    money_prev = price_prev = "平"
    start_i = max(CONCLUSION_DAYS + 25, len(daily_all) - PHASE_HISTORY)
    for end in range(start_i, len(daily_all)):
        sliced = _slice_series(series, end)
        mz, price_z, ex5, heat, rate = _scores_at(sliced, bench_closes, bench_amounts, source)
        money = rules.money_side(mz, money_prev, enter, hold)
        price = rules.price_side(price_z, price_prev)
        heat_f = rules.heat_flag(heat)
        money_prev, price_prev = money, price
        pos = position(sliced["closes"])
        layers = rules.sector_phase(money, price, heat_f, pos, rules.lever_word(lever_value),
                                   mz=mz, price_z=price_z, heat_ratio=heat, rate=rate,
                                   cold=heat is not None and heat <= rules.SECTOR_COLD)
        raw.append({**layers, "mz": mz, "price_z": price_z, "heat": heat, "ex5": ex5, "rate": rate, "pos": pos})
    confirmed = rules.sticky_phases(raw)
    last = confirmed[-1] if confirmed else raw[-1]
    daily = [value for _day, value in daily_all]
    flow = {n: (sum(daily[-n:]) if len(daily) >= n else None) for n in WINDOWS}
    started, fading = rules.pulse_flags(
        [row["bucket"] for row in confirmed], mz=last.get("mz"), heat=last.get("heat"),
        phase=last.get("phase"), flow=flow.get(CONCLUSION_DAYS),
    )
    closes = series["closes"]
    ret = {n: window_return(closes, n) for n in RS_DAYS}
    size = series.get("size")
    # ret5 / ex5 字段名沿用，值按结论窗口（当前 3 日）
    return {
        "name": name, "group": group, "code": code, "source": source, "industry": l1,
        "flow": flow, "flow_rate": last.get("rate"), "size": size, "money_z": last.get("mz"),
        "ret5": ret.get(CONCLUSION_DAYS, ret.get(5)), "ret20": ret.get(20),
        "ex5": last.get("ex5"), "ex20": (None if ret.get(20) is None or window_return(bench_closes, 20) is None
                                         else ret[20] - window_return(bench_closes, 20)),
        "heat": last.get("heat"), "position": last.get("pos"), "lever": lever_value, "lever_word": rules.lever_word(lever_value),
        "verdict": last.get("verdict"), "phase": last.get("phase"), "tone": last.get("tone"), "bucket": last.get("bucket"),
        "why": last.get("why", ""), "weather": last.get("weather", ""), "meaning": last.get("meaning", ""),
        "price_z": last.get("price_z"),
        "mz_hist": [row.get("mz") for row in confirmed[-6:]],
        "price_z_hist": [row.get("price_z") for row in confirmed[-6:]],
        "started": started, "fading": fading, "bucket_hist": [row["bucket"] for row in confirmed[-rules.PULSE_LOOKBACK - 1:]],
        "conclusion_days": CONCLUSION_DAYS,
    }


# ---------------------------------------------------------------- 取数
def _bench(today: date) -> tuple[list[tuple[str, float]], list[tuple[str, float]]]:
    start = tape._ymd_offset(today, 400)
    rows = tape.cached(f"holdidx:{start}", lambda: tape.pro().index_daily(
        ts_code="000300.SH", start_date=start, end_date=tape._ymd(today), fields="trade_date,close,amount",
    ))
    ordered = sorted([row for row in rows if tape.num(row.get("close"))], key=lambda row: tape.ymd(row.get("trade_date")))
    closes = [(tape.ymd(row.get("trade_date")), tape.num(row.get("close"))) for row in ordered]
    amounts = [(tape.ymd(row.get("trade_date")), (tape.num(row.get("amount")) or 0.0)) for row in ordered]
    return closes, amounts


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


def _flow_daily(today: date, names: list[str], content_type: str, cache_prefix: str) -> dict[str, list[tuple[str, float]]]:
    """东方财富行业/概念主力资金，取出指定名称近 HISTORY+10 个交易日的净流入（亿元）。"""
    days = tape.open_dates(today, HISTORY + 10)
    loaded = tape._parallel(
        days,
        lambda day: tape.cached(
            f"{cache_prefix}:{day}",
            lambda d=day: tape.pro().moneyflow_ind_dc(trade_date=d, content_type=content_type),
        ),
    )
    out: dict[str, list[tuple[str, float]]] = {name: [] for name in names}
    for day, rows in zip(days, loaded):
        by_name = {str(row.get("name")): tape.num(row.get("net_amount")) for row in rows or []}
        for name in names:
            value = by_name.get(name)
            if value is not None:
                out[name].append((day, value / 1e8))
    return out


def _industry_daily(today: date, names: list[str]) -> dict[str, list[tuple[str, float]]]:
    """东方财富行业主力资金。"""
    return _flow_daily(today, names, "行业", "indflow")


def _concept_daily(today: date, names: list[str]) -> dict[str, list[tuple[str, float]]]:
    """东方财富概念主力资金。"""
    return _flow_daily(today, names, "概念", "conceptflow")


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


def _dc_series(today: date, code: str) -> dict:
    """东财概念板日线：收盘价 + 成交额（热度对照沪深 300）。"""
    start = tape._ymd_offset(today, 400)
    rows = tape.cached(f"dcidx:{code}:{start}", lambda: tape.pro().dc_daily(
        ts_code=code, start_date=start, end_date=tape._ymd(today),
    ))
    ordered = sorted([row for row in rows if tape.num(row.get("close"))], key=lambda row: tape.ymd(row.get("trade_date")))
    return {
        "closes": [(tape.ymd(row.get("trade_date")), tape.num(row.get("close"))) for row in ordered],
        "amounts": [(tape.ymd(row.get("trade_date")), tape.num(row.get("amount")) or 0.0) for row in ordered],
    }


def build(today: date) -> dict:
    etfs = []
    for row in UNIVERSE:
        for code in _etfs_of(row):
            if code not in etfs:
                etfs.append(code)
    share_rows = dict(zip(etfs, tape._parallel(etfs, lambda code: tape.fund_share_rows(code, today))))
    bar_rows = dict(zip(etfs, tape._parallel(etfs, lambda code: tape.fund_bar_rows(code, today))))
    bench_closes, bench_amounts = _bench(today)
    levers = _lever(today)
    rows = []
    latest_share_day = ""
    for name, group, etf_codes, l1s, _kw, _default in UNIVERSE:
        parts = []
        for code in etf_codes:
            for row in share_rows.get(code) or []:
                latest_share_day = max(latest_share_day, tape.ymd(row.get("trade_date")))
            parts.append(etf_series(share_rows.get(code) or [], bar_rows.get(code) or []))
        series = merge_etf_series(parts)
        rows.append(make_row(name, group, etf_codes[0], "etf", series, bench_closes, bench_amounts,
                             levers.get(l1s[0]) if len(l1s) == 1 else None, "、".join(l1s)))
    names = [name for name, _group, _code in BASE]
    industry = _industry_daily(today, names)
    for name, group, code in BASE:
        sw = _sw_series(today, code)
        daily = industry.get(name, [])
        # BASE 的 sizes 用不到；amounts 与 daily 按日对齐在 make_row 里做
        rows.append(make_row(name, group, code, "industry",
                             {**sw, "daily": daily, "size": None, "sizes": [], "listed": len(sw.get("closes") or [])},
                             bench_closes, bench_amounts, levers.get(name), name))
    money_names = [money for _name, _group, _code, money in CONCEPTS]
    concept_flow = _concept_daily(today, money_names)
    for name, group, code, money_name in CONCEPTS:
        dc = _dc_series(today, code)
        daily = concept_flow.get(money_name, [])
        rows.append(make_row(name, group, code, "concept",
                             {**dc, "daily": daily, "size": None, "sizes": [], "listed": len(dc.get("closes") or [])},
                             bench_closes, bench_amounts, None, money_name))
    return {
        "rows": rows, "date": tape.iso(latest_share_day), "windows": list(WINDOWS), "conclusion_days": CONCLUSION_DAYS,
        "coverage": {"l1": len(L1_CODES), "etf": len(UNIVERSE), "estimated": len(BASE), "concept": len(CONCEPTS)},
    }


def _meta(name: str) -> dict | None:
    """板块静态元数据：来源、代表 ETF、东财/申万代码。"""
    for title, group, etfs, l1s, _kw, _default in UNIVERSE:
        if title == name:
            return {"name": name, "group": group, "source": "etf", "etfs": list(etfs), "code": etfs[0], "l1s": list(l1s)}
    for title, group, code in BASE:
        if title == name:
            return {"name": name, "group": group, "source": "industry", "etfs": [], "code": code, "l1s": [title]}
    for title, group, code, money_name in CONCEPTS:
        if title == name:
            return {"name": name, "group": group, "source": "concept", "etfs": [], "code": code, "money_name": money_name, "l1s": []}
    return None


def _fund_names(today: date) -> dict[str, str]:
    """场内 ETF（market=E）名称；场外 O 不含 159995 这类代码。"""
    rows = tape.cached(
        f"fund_basic_E:{today.year}{today.month:02d}",
        lambda: tape.pro().fund_basic(market="E", fields="ts_code,name"),
    )
    return {str(row.get("ts_code") or ""): str(row.get("name") or "") for row in rows or [] if row.get("ts_code")}


def _dc_index_day(today: date) -> tuple[str, list[dict]]:
    days = tape.open_dates(today, 5)
    for day in reversed(days):
        rows = []
        for kind in ("概念板块", "行业板块"):
            part = tape.cached(
                f"dcidxlist:{kind}:{day}",
                lambda d=day, k=kind: tape.pro().dc_index(trade_date=d, idx_type=k),
            )
            rows.extend(part or [])
        if rows:
            return day, rows
    return tape._ymd(today), []


def _match_dc_board(meta: dict, boards: list[dict]) -> dict | None:
    """给板块找对应的东财板：题材用 BK 代码，其余按名字 / 申万一级碰。"""
    by_code = {str(row.get("ts_code") or ""): row for row in boards}
    by_name = {str(row.get("name") or ""): row for row in boards}
    if meta["source"] == "concept":
        return by_code.get(meta["code"])
    money_name = meta.get("money_name")
    if money_name and money_name in by_name:
        return by_name[money_name]
    if meta["name"] in by_name:
        return by_name[meta["name"]]
    for l1 in meta.get("l1s") or []:
        if l1 in by_name:
            return by_name[l1]
    # 芯片 → 半导体 这类：名字被包含
    for title in [meta["name"], *(meta.get("l1s") or [])]:
        for name, row in by_name.items():
            if title and title in name:
                return row
    return None


def _norm_stock_code(code: str) -> str:
    raw = str(code or "").strip().upper()
    if not raw:
        return ""
    if "." in raw:
        return raw
    if raw.startswith("6"):
        return raw + ".SH"
    if raw.startswith(("0", "3")):
        return raw + ".SZ"
    if raw.startswith(("8", "4")):
        return raw + ".BJ"
    return raw


def _board_members(meta: dict, today: date) -> tuple[str, dict | None, list[dict]]:
    """东财对应板上的成分：[(name, code)]，附带板信息和交易日。"""
    day, boards = _dc_index_day(today)
    board = _match_dc_board(meta, boards)
    if not board:
        return day, None, []
    board_code = str(board.get("ts_code") or "")
    rows = tape.cached(
        f"dcmember:{board_code}:{day}",
        lambda: tape.pro().dc_member(ts_code=board_code, trade_date=day),
    ) if board_code else []
    members = []
    seen: set[str] = set()
    for row in rows or []:
        code = _norm_stock_code(str(row.get("con_code") or row.get("ts_code") or ""))
        name = str(row.get("name") or row.get("con_name") or "").strip()
        if not code or not name or code in seen:
            continue
        seen.add(code)
        members.append({"name": name, "ts_code": code})
    return day, board, members


def _circ_mv_map(day: str) -> dict[str, float]:
    """当日流通市值，单位：亿元。"""
    rows = tape.cached(
        f"daily_basic_mv:{day}",
        lambda: tape.pro().daily_basic(trade_date=day, fields="ts_code,circ_mv,total_mv"),
    )
    out: dict[str, float] = {}
    for row in rows or []:
        code = str(row.get("ts_code") or "")
        mv = tape.num(row.get("circ_mv"))
        if not code or mv is None:
            continue
        out[code] = mv / 1e4
    return out


def _is_st(name: str) -> bool:
    text = str(name or "").upper().replace(" ", "")
    return text.startswith(("ST", "*ST", "ＳＴ", "＊ＳＴ"))


def _gainers(board: dict | None, members: list[dict], day: str, limit: int = 3) -> list[dict]:
    """当日领涨：东财领涨股 + 成分涨幅靠前。"""
    daily = {str(row.get("ts_code") or ""): tape.num(row.get("pct_chg")) for row in (tape._daily_rows(day) or [])}
    out: list[dict] = []
    seen: set[str] = set()

    def add(name: str, code: str, pct, role: str):
        code = _norm_stock_code(code)
        key = code or name
        if not name or key in seen:
            return
        seen.add(key)
        out.append({"name": name, "ts_code": code, "pct": pct, "role": role})

    if board:
        add(str(board.get("leading") or ""), str(board.get("leading_code") or ""), tape.num(board.get("leading_pct")), "领涨")
    ranked = sorted(
        ((daily.get(m["ts_code"]), m["name"], m["ts_code"]) for m in members),
        key=lambda item: (item[0] is None, -(item[0] or 0)),
    )
    for pct, name, code in ranked:
        if len(out) >= limit:
            break
        add(name, code, pct, "成分")
    return out[:limit]


def _leaders(members: list[dict], day: str, limit: int = 3) -> list[dict]:
    """真正的龙头：成分里流通市值最大的几只（剔除 ST）。"""
    mvs = _circ_mv_map(day)
    daily = {str(row.get("ts_code") or ""): tape.num(row.get("pct_chg")) for row in (tape._daily_rows(day) or [])}
    ranked = []
    for m in members:
        if _is_st(m["name"]):
            continue
        mv = mvs.get(m["ts_code"])
        if mv is None:
            continue
        ranked.append((mv, m["name"], m["ts_code"], daily.get(m["ts_code"])))
    ranked.sort(key=lambda item: -item[0])
    out = []
    for index, (mv, name, code, pct) in enumerate(ranked[:limit]):
        out.append({
            "name": name, "ts_code": code, "pct": pct,
            "circ_mv": round(mv, 1), "role": "龙头" if index == 0 else f"第{index + 1}",
        })
    return out


def popup(name: str, today: date | None = None, row: dict | None = None) -> dict:
    """点击板块名时的弹框：钱/热度摘要、代表 ETF、领涨 / 龙头个股。"""
    today = today or date.today()
    meta = _meta(name)
    if not meta:
        raise KeyError(name)
    row = row or {}
    names = _fund_names(today)
    etfs = [{"code": code, "name": names.get(code) or code} for code in meta.get("etfs") or []]
    day, board, members = _board_members(meta, today)
    gainers = _gainers(board, members, day)
    leaders = _leaders(members, day)
    return {
        "name": name,
        "group": meta["group"],
        "source": meta["source"],
        "code": meta["code"],
        "phase": row.get("phase") or "",
        "bucket": row.get("bucket") or "",
        "tone": row.get("tone") or "flat",
        "why": row.get("why") or "",
        "meaning": row.get("meaning") or "",
        "money_z": row.get("money_z"),
        "price_z": row.get("price_z"),
        "heat": row.get("heat"),
        "flow": row.get("flow") if not isinstance(row.get("flow"), dict) else row.get("flow", {}).get(CONCLUSION_DAYS),
        "flow_rate": row.get("flow_rate"),
        "etfs": etfs,
        "gainers": gainers,
        "leaders": leaders,
        "note": (
            "钱看 ETF 净申购÷规模" if meta["source"] == "etf"
            else "钱看行业主力净流入÷成交额，没有代表 ETF" if meta["source"] == "industry"
            else "钱看题材主力净流入÷成交额；代表标的用东财概念成分"
        ),
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
