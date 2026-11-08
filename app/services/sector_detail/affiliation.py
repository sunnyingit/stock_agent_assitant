"""判断一只股票属于哪两个板块：业务暴露、交易联动、板块内地位三层证据，只做计算，不取数。

成分名单只说明"挂在这个板块名下"，不说明"属于"。属于要求：是成分，并且
  业务占比 ≥ BUSINESS_MIN，或者（60 日相关 ≥ CORR_MIN 且 市值排名在板块前 TOP_PCT）。
主板块是属于里综合分最高的；次板块是和主板块不是同一回事（指数相关 < TWIN_CORR、成分重合 < OVERLAP_MAX）的属于板块里，
在主板块之外多解释个股收益最多的那个（R² 增量 ≥ SECOND_MIN，否则没有次板块）。
再用两个板块指数对个股收益做回归，拆出各自解释了多少（沙普利分解），得到双驱动 / 单驱动 / 独立。
"""

from __future__ import annotations

import re

VERSION = "sector-aff-1"
WINDOW = 60
SHORT = 20
MIN_DAYS = 40

BUSINESS_MIN = 0.20
BUSINESS_FULL = 0.50
CORR_MIN = 0.60
TOP_PCT = 0.30
TWIN_CORR = 0.97      # 指数相关到这个程度才算"同一回事"：科技类板块之间普遍 0.9 以上，用 0.85 会全并掉
SECOND_MIN = 0.03     # 次板块要在主板块之外多解释个股收益至少这么多（R² 增量）
OFFICIAL_SHARE = 0.50 # 申万按主营收入占比 ≥50% 归类，所以本股所属的申万三级行业按这个占比算业务证据
OVERLAP_MAX = 0.50
SHORTLIST = 8
WEIGHTS = {"business": 0.45, "linkage": 0.40, "position": 0.15}

DRIVER_FLOOR = 0.25   # 两个板块合起来解释的收益不到这个比例：个股走势独立
DRIVER_EACH = 0.08    # 单个板块至少解释这么多才算一个驱动

# 同花顺"概念"里大量是市场标签（融资融券、指数成份、年报概念），和公司做什么无关
NOISE = (
    "融资融券", "转融券", "深股通", "沪股通", "港股通", "国企改革", "央企", "国资", "中证", "沪深300", "上证", "深证", "成份", "成分",
    "人民币", "贬值", "数字经济", "专精特新", "一带一路", "破净", "标普", "MSCI", "富时", "百元", "低价", "高送转", "预盈", "预增",
    "超跌", "昨日", "举牌", "股权转让", "回购", "增持", "股权激励", "业绩", "次新", "社保", "基金重仓", "QFII", "机构重仓",
    "注册制", "摘帽", "重组", "并购", "ST", "科创板", "创业板", "北交所", "新股", "ESG", "同花顺",
)
CONCEPT_MIN = 8
CONCEPT_MAX = 600

TOTAL_ITEMS = ("产品", "其他业务", "其他", "其它", "合计", "其他(补充)", "其他（补充）", "抵消", "内部抵销")
_GENERIC = ("板块", "概念", "产业", "行业", "制造", "产品", "其他", "业务", "服务", "设备", "材料", "技术", "相关", "综合", "及", "和", "类", "Ⅰ", "Ⅱ", "Ⅲ")
# 大类词：两个名字只在这些词上重合，说明不了做的是同一件事（"存储半导体"和"第三代半导体"只共有"半导体"）
_BROAD = (
    "半导体", "芯片", "电子", "智能", "新能源", "科技", "信息", "通信", "汽车", "能源", "数字", "网络", "软件", "互联网",
    "医药", "医疗", "金融", "消费", "工业", "第三代", "新型", "高端", "国产", "自主",
)
_PUNCT = re.compile(r"[\s\d()（）、，,.\-_/]+")


def usable_concept(name: str, count: int) -> bool:
    if not (CONCEPT_MIN <= count <= CONCEPT_MAX):
        return False
    return not any(word in name for word in NOISE)


# ---------------------------------------------------------------- 业务暴露
def _core(name: str) -> str:
    text = _PUNCT.sub("", str(name or ""))
    for word in _GENERIC:
        text = text.replace(word, "")
    return text


def _specific(text: str) -> str:
    for word in _BROAD:
        text = text.replace(word, "")
    return text


def _common(left: str, right: str) -> int:
    best = 0
    for i in range(len(left)):
        for j in range(len(right)):
            k = 0
            while i + k < len(left) and j + k < len(right) and left[i + k] == right[j + k]:
                k += 1
            best = max(best, k)
    return best


def name_match(item: str, sector: str) -> bool:
    """主营产品名和板块名说的是同一件事：去掉大类词后，有至少 2 个字的公共片段，且占较短一方的一半以上。
    两边都只剩大类词时（产品叫"半导体"、板块也叫"半导体"），改比原名，要求公共片段至少 3 个字。"""
    a, b = _core(item), _core(sector)
    if len(a) < 2 or len(b) < 2:
        return False
    sa, sb = _specific(a), _specific(b)
    if sa and sb:
        common = _common(sa, sb)
        return common >= 2 and common >= 0.5 * min(len(sa), len(sb))
    if sa or sb:
        return False
    common = _common(a, b)
    return common >= 3 and common >= 0.5 * min(len(a), len(b))


def business_lines(lines: list[tuple[str, float]]) -> list[dict]:
    """主营产品构成（不含汇总行），按收入占比从大到小。"""
    rows = [(name, float(sales)) for name, sales in lines if name and sales and sales > 0 and name not in TOTAL_ITEMS]
    total = sum(sales for _name, sales in rows)
    if total <= 0:
        return []
    return [{"name": name, "share": round(sales / total, 4)} for name, sales in sorted(rows, key=lambda row: -row[1])]


def business_share(lines: list[tuple[str, float]], sector_name: str) -> dict:
    """lines 是（产品线名, 收入）。返回匹配上的收入占比和匹配到的产品线。汇总行（"产品""其他业务"）不算。"""
    rows = [(name, float(sales)) for name, sales in lines if name and sales and sales > 0 and name not in TOTAL_ITEMS]
    total = sum(sales for _name, sales in rows)
    if total <= 0:
        return {"share": None, "items": []}
    hit = [(name, sales) for name, sales in rows if name_match(name, sector_name)]
    return {"share": round(sum(sales for _n, sales in hit) / total, 4), "items": [name for name, _s in hit]}


def official_business(business: dict) -> dict:
    """申万行业按主营收入归类，所以本股所属的三级行业，业务占比至少按 OFFICIAL_SHARE 算，并标明依据。"""
    share = max(business.get("share") or 0.0, OFFICIAL_SHARE)
    return {"share": round(share, 4), "items": business.get("items") or [], "basis": "申万按主营收入归类"}


# ---------------------------------------------------------------- 交易联动
def _aligned(left: dict, right: dict, dates: list[str], size: int) -> tuple[list[float], list[float]]:
    xs, ys = [], []
    for day in dates:
        a, b = left.get(day), right.get(day)
        if a is not None and b is not None:
            xs.append(float(a))
            ys.append(float(b))
    return xs[-size:], ys[-size:]


def _corr(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 10:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sx = sum((x - mx) ** 2 for x in xs) ** 0.5
    sy = sum((y - my) ** 2 for y in ys) ** 0.5
    if sx == 0 or sy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


def link(stock: dict, sector: dict, dates: list[str]) -> dict:
    """个股与板块指数的联动：20/60 日相关、60 日 beta、板块涨时和跌时个股的跟随程度。stock、sector 都是 日期→日收益。"""
    xs, ys = _aligned(sector, stock, dates, WINDOW)
    out = {"days": len(xs), "corr60": None, "corr20": None, "beta60": None, "up_capture": None, "down_capture": None}
    if len(xs) < MIN_DAYS:
        return out
    corr60 = _corr(xs, ys)
    out["corr60"] = None if corr60 is None else round(corr60, 3)
    short = _corr(xs[-SHORT:], ys[-SHORT:])
    out["corr20"] = None if short is None else round(short, 3)
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    var = sum((x - mx) ** 2 for x in xs)
    if var > 0:
        out["beta60"] = round(sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / var, 3)
    for key, pick in (("up_capture", lambda x: x > 0), ("down_capture", lambda x: x < 0)):
        pairs = [(x, y) for x, y in zip(xs, ys) if pick(x)]
        if len(pairs) >= 8 and sum(x for x, _y in pairs) != 0:
            out[key] = round(sum(y for _x, y in pairs) / sum(x for x, _y in pairs), 3)
    return out


# ---------------------------------------------------------------- 板块内地位
def position(rank: int | None, total: int | None) -> dict:
    if not rank or not total:
        return {"rank": None, "total": None, "top_pct": None}
    return {"rank": rank, "total": total, "top_pct": round(rank / total, 4)}


def rank_by_value(code: str, members: list[str], caps: dict[str, float]) -> tuple[int | None, int | None]:
    """本股在成分里按总市值的名次。取不到本股市值，或成分里有市值的不到 3 只，不给名次。"""
    mine = caps.get(code)
    if mine is None:
        return None, None
    peers = [caps[item] for item in members if item in caps]
    if len(peers) < 3:
        return None, None
    return sum(1 for value in peers if value > mine) + 1, len(peers)


# ---------------------------------------------------------------- 综合
def score(business: dict, linkage: dict, pos: dict) -> float:
    biz = min((business.get("share") or 0.0) / BUSINESS_FULL, 1.0)
    corr = max(linkage.get("corr60") or 0.0, 0.0)
    top = pos.get("top_pct")
    place = 0.0 if top is None else max(1.0 - (top - 1.0 / max(pos.get("total") or 1, 1)), 0.0)
    return round(WEIGHTS["business"] * biz + WEIGHTS["linkage"] * corr + WEIGHTS["position"] * place, 4)


def judge(cand: dict) -> tuple[bool, str]:
    """是否"属于"，以及依据（给人读，也给模板答案用）。cand 里要有 member、business、linkage、position。"""
    if not cand.get("member"):
        return False, "不是该板块成分"
    share = (cand.get("business") or {}).get("share")
    if share is not None and share >= BUSINESS_MIN:
        return True, "business"
    corr = (cand.get("linkage") or {}).get("corr60")
    top = (cand.get("position") or {}).get("top_pct")
    if corr is not None and corr >= CORR_MIN and top is not None and top <= TOP_PCT:
        return True, "linkage"
    if corr is not None and corr >= CORR_MIN:
        return False, "联动强但不是核心成员"
    return False, "仅挂名"


def index_corr(left: dict, right: dict, dates: list[str]) -> float | None:
    xs, ys = _aligned(left, right, dates, WINDOW)
    return None if len(xs) < MIN_DAYS else _corr(xs, ys)


def overlap(left: set, right: set) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def choose(cands: list[dict], dates: list[str], stock: dict) -> dict:
    """cands 每项带 score / belongs / returns（板块指数日收益）/ members（代码集合）；stock 是本股日收益。
    返回主、次板块，和被并掉的相近板块（和主板块几乎是同一条指数、或成分高度重合）。"""
    owned = sorted((c for c in cands if c.get("belongs")), key=lambda c: c["score"], reverse=True)
    if not owned:
        return {"primary": None, "secondary": None, "merged": []}
    primary = owned[0]
    merged, apart = [], []
    for cand in owned[1:]:
        twin = index_corr(primary["returns"], cand["returns"], dates)
        shared = overlap(primary.get("members") or set(), cand.get("members") or set())
        if (twin is not None and twin >= TWIN_CORR) or shared >= OVERLAP_MAX:
            merged.append({"code": cand["code"], "name": cand["name"], "index_corr": None if twin is None else round(twin, 3), "overlap": round(shared, 3)})
        else:
            apart.append(cand)
    secondary, best = None, SECOND_MIN
    base = _fit(stock, primary["returns"], None, dates)
    for cand in apart:   # 按综合分从高到低，增量相同时先到先得
        both = _fit(stock, primary["returns"], cand["returns"], dates)
        if base is None or both is None:
            continue
        gain = both["r2"] - base["r2"]
        cand["gain"] = round(gain, 4)
        if gain > best:
            secondary, best = cand, gain
    return {"primary": primary, "secondary": secondary, "merged": merged}


# ---------------------------------------------------------------- 驱动归因
def _r2_two(y: list[float], a: list[float], b: list[float]) -> float | None:
    """y 对 a、b 带截距的最小二乘 R²。"""
    n = len(y)
    my, ma, mb = sum(y) / n, sum(a) / n, sum(b) / n
    ya = [v - my for v in y]
    aa = [v - ma for v in a]
    bb = [v - mb for v in b]
    saa = sum(v * v for v in aa)
    sbb = sum(v * v for v in bb)
    sab = sum(p * q for p, q in zip(aa, bb))
    sya = sum(p * q for p, q in zip(ya, aa))
    syb = sum(p * q for p, q in zip(ya, bb))
    syy = sum(v * v for v in ya)
    det = saa * sbb - sab * sab
    if syy == 0 or det <= 1e-12 * max(saa * sbb, 1e-30):
        return None
    ca = (sya * sbb - syb * sab) / det
    cb = (syb * saa - sya * sab) / det
    return max(0.0, min(1.0, (ca * sya + cb * syb) / syy))


def _fit(stock: dict, primary: dict, secondary: dict | None, dates: list[str]) -> dict | None:
    """近 WINDOW 日，个股收益对一个或两个板块指数的拟合：r2 合计，a / b 各自单独的 r2。样本不足或没有波动返回 None。"""
    rows = []
    for day in dates:
        y, a = stock.get(day), primary.get(day)
        b = None if secondary is None else secondary.get(day)
        if y is None or a is None or (secondary is not None and b is None):
            continue
        rows.append((float(y), float(a), None if b is None else float(b)))
    rows = rows[-WINDOW:]
    if len(rows) < MIN_DAYS:
        return None
    y = [r[0] for r in rows]
    ca = _corr([r[1] for r in rows], y)
    if ca is None:
        return None
    out = {"days": len(rows), "a": ca * ca, "b": None, "r2": ca * ca}
    if secondary is None:
        return out
    cb = _corr([r[2] for r in rows], y)
    both = _r2_two(y, [r[1] for r in rows], [r[2] for r in rows])
    if cb is None or both is None:
        return None
    out.update({"b": cb * cb, "r2": max(both, ca * ca, cb * cb)})
    return out


def attribute(stock: dict, primary: dict, secondary: dict | None, dates: list[str]) -> dict:
    """两个板块指数各自解释了个股近 60 日收益的多少。type：dual 双驱动、single 单驱动、independent 独立。

    沙普利分解：两个板块相关时，共同解释的部分平分，所以两项加起来等于合计的 R²。
    """
    fit = _fit(stock, primary, secondary, dates)
    out = {"type": "unknown", "r2": None, "primary_share": None, "secondary_share": None, "weights": None, "days": 0 if fit is None else fit["days"]}
    if fit is None:
        return out
    if secondary is None:
        out.update({"r2": round(fit["r2"], 4), "primary_share": round(fit["r2"], 4), "weights": {"primary": 1.0, "secondary": 0.0}})
        out["type"] = "single" if fit["r2"] >= DRIVER_FLOOR else "independent"
        return out
    r2a, r2b, r2ab = fit["a"], fit["b"], fit["r2"]
    share_a = (r2a + (r2ab - r2b)) / 2
    share_b = (r2b + (r2ab - r2a)) / 2
    out.update({"r2": round(r2ab, 4), "primary_share": round(share_a, 4), "secondary_share": round(share_b, 4)})
    out["weights"] = {"primary": round(share_a / r2ab, 3), "secondary": round(share_b / r2ab, 3)} if r2ab > 0 else {"primary": 0.5, "secondary": 0.5}
    if r2ab < DRIVER_FLOOR:
        out["type"] = "independent"
    elif share_a >= DRIVER_EACH and share_b >= DRIVER_EACH:
        out["type"] = "dual"
    else:
        out["type"] = "single"
    return out
