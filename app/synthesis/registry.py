"""事实注册表：每个事实的编号、所属维度、含义、单位和展示格式。

事实表构建、数字校验、匿名化都读这一份，避免提示词、校验、模板各写一套而对不上。
值的约定：pct 是带符号的小数（0.052 = +5.2%）；share 是不带符号的占比；ratio 是倍数；int/days 是整数；enum 是英文码；bool；text 直接展示。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_NUMBER = re.compile(r"\d+(?:\.\d+)?")


@dataclass(frozen=True)
class FactDef:
    id: str
    dim: str          # 事实所属维度；market 是代码直接给的口径
    label: str
    kind: str         # pct | share | ratio | int | days | enum | bool | text
    enum: tuple = ()  # ((code, 中文), ...)


def _enum(*pairs) -> tuple:
    return tuple(pairs)


_PATTERN = _enum(
    ("up_vol_up", "价涨量增"), ("up_vol_down", "价涨量缩"),
    ("down_vol_up", "价跌量增"), ("down_vol_down", "价跌量缩"), ("none", "无明显形态"),
)
_BIG_TREND = _enum(
    ("accelerating_in", "大资金流入在加快"), ("slowing_in", "大资金仍在流入但放缓"),
    ("steady_in", "大资金持续流入"), ("turning_out", "大资金由流入转为流出"),
    ("accelerating_out", "大资金流出在加快"), ("mixed", "大资金方向不一致"),
)
_PRICED = _enum(("not_yet", "尚未反应"), ("partly", "部分反应"), ("mostly", "基本反应"), ("unknown", "无法判断"))
_STAGE = _enum(
    ("start", "启动"), ("main", "主升"), ("climax", "高潮"),
    ("diverge", "分歧"), ("ebb", "退潮"), ("weak", "弱势"), ("none", "没有明确阶段"),
)
_ROLE = _enum(
    ("leader", "领涨"), ("resilient", "抗跌"), ("core", "中军"),
    ("follower", "跟风"), ("independent", "独立走势"),
)
_DRIVER = _enum(("dual", "两个板块共同驱动"), ("single", "单一板块驱动"), ("independent", "走势独立于板块"))
_PHASE = _enum(("active", "财报披露期"), ("upcoming", "即将披露"), ("quiet", "无新财报"))
_FIN_GROWTH = _enum(("up", "增长向上"), ("down", "增长向下"), ("mixed", "营收和利润分化"), ("loss", "亏损"), ("na", "暂无法比较"))
_FIN_QUALITY = _enum(("good", "含金量较好"), ("ok", "含金量一般"), ("poor", "含金量偏弱"), ("na", "暂无法判断"))
_VALUATION = _enum(("cheap", "偏便宜"), ("fair", "合理"), ("expensive", "偏贵"))
_QUALITY = _enum(("strong", "较好"), ("medium", "中等"), ("weak", "偏弱"))
_HOLDERS = _enum(("positive", "偏多"), ("offset", "相抵"), ("negative", "偏空"), ("quiet", "没有动作"))
_VIEW_NOTE = "该维度自己的结论"

_DEFS = (
    # 量价资金
    FactDef("flow.view", "volume_flow", f"量价资金：{_VIEW_NOTE}", "text"),
    FactDef("flow.rel_vol", "volume_flow", "近 20 日相对量能（相对此前 60 日均量）", "ratio"),
    FactDef("flow.pattern", "volume_flow", "量价形态", "enum", _PATTERN),
    FactDef("flow.up_down_vol", "volume_flow", "上涨日与下跌日的成交量比", "ratio"),
    FactDef("flow.big_trend", "volume_flow", "大资金近期趋势", "enum", _BIG_TREND),
    FactDef("flow.net_rate", "volume_flow", "近 20 日大资金净流入占成交额", "pct"),
    FactDef("flow.stall", "volume_flow", "命中的涨不动迹象", "text"),
    # 新闻
    FactDef("news.view", "news", f"新闻：{_VIEW_NOTE}", "text"),
    FactDef("news.main_event", "news", "主要事件（类别、来源等级、利好利空、量级）", "text"),
    FactDef("news.priced_in", "news", "主要事件是否已被股价反应", "enum", _PRICED),
    FactDef("news.counter_event", "news", "反方事件（同样四项）", "text"),
    FactDef("news.company_notice", "news", "近期是否有公司级公告", "bool"),
    # 板块
    FactDef("sector.view", "sector", f"板块：{_VIEW_NOTE}", "text"),
    FactDef("sector.stage", "sector", "所属板块当前阶段", "enum", _STAGE),
    FactDef("sector.days", "sector", "这一轮已持续的交易日数", "days"),
    FactDef("sector.role", "sector", "本股在板块里的角色", "enum", _ROLE),
    FactDef("sector.vs_index", "sector", "板块 20 日涨幅减大盘", "pct"),
    FactDef("sector.stock_vs_sector", "sector", "本股 20 日涨幅减板块", "pct"),
    FactDef("sector.warn", "sector", "板块是否出现高潮或持续过久的提示", "bool"),
    FactDef("sector.driver", "sector", "两个板块对本股走势的驱动方式", "enum", _DRIVER),
    FactDef("sector.secondary_stage", "sector", "次板块当前阶段（没有次板块则不出）", "enum", _STAGE),
    FactDef("sector.divergent", "sector", "主、次板块阶段是否对立", "bool"),
    # 财报
    FactDef("fin.view", "finance", f"财报：{_VIEW_NOTE}", "text"),
    FactDef("fin.phase", "finance", "财报阶段", "enum", _PHASE),
    FactDef("fin.growth", "finance", "最近一份财报的单季增长", "enum", _FIN_GROWTH),
    FactDef("fin.quality", "finance", "最近一份财报的利润含金量", "enum", _FIN_QUALITY),
    FactDef("fin.days_since", "finance", "距最近一份财报披露的天数", "days"),
    FactDef("fin.days_to_next", "finance", "距下次披露的天数", "days"),
    FactDef("fin.warn_hits", "finance", "命中必须判需警惕的硬阈值数", "int"),
    FactDef("fin.red_line", "finance", "是否命中财报红线", "bool"),
    # 基本面
    FactDef("fund.view", "fundamental", f"基本面：{_VIEW_NOTE}", "text"),
    FactDef("fund.valuation", "fundamental", "估值档位", "enum", _VALUATION),
    FactDef("fund.pctile", "fundamental", "估值在自身 5 年历史里的分位", "share"),
    FactDef("fund.quality", "fundamental", "质地档位", "enum", _QUALITY),
    FactDef("fund.holders", "fundamental", "股东行为档位", "enum", _HOLDERS),
    FactDef("fund.holder_event", "fundamental", "未来 20 个交易日内的大股东事件", "text"),
    # 市场（代码直接按统一口径算）
    FactDef("market.stock_ret_20d", "market", "本股近 20 日涨幅", "pct"),
    FactDef("market.index_ret_20d", "market", "大盘（沪深300）近 20 日涨跌", "pct"),
    FactDef("market.from_year_high", "market", "收盘价距一年高点（负数表示在高点下方）", "pct"),
)

REGISTRY: dict[str, FactDef] = {item.id: item for item in _DEFS}


def definition(fact_id: str) -> FactDef | None:
    return REGISTRY.get(fact_id)


def enum_text(fact_id: str, code: Any) -> str:
    defn = REGISTRY.get(fact_id)
    if defn is None:
        return str(code)
    for key, text in defn.enum:
        if key == code:
            return text
    return str(code)


def render(fact_id: str, value: Any) -> str:
    """给模型看的值。保留符号和一位小数，数字校验按同一套规则回查。"""
    defn = REGISTRY.get(fact_id)
    if defn is None or value is None:
        return "" if value is None else str(value)
    if defn.kind == "pct":
        return f"{value * 100:+.1f}%"
    if defn.kind == "share":
        return f"{value * 100:.1f}%"
    if defn.kind == "ratio":
        return f"{value:.2f} 倍"
    if defn.kind in ("int", "days"):
        return f"{int(value)}" + (" 个交易日" if defn.kind == "days" else "")
    if defn.kind == "bool":
        return "是" if value else "否"
    if defn.kind == "enum":
        return enum_text(fact_id, value)
    return str(value)


def numbers(fact_id: str, value: Any, text: str = "") -> set[float]:
    """这条事实允许在文字里出现的数字：原值、百分数与小数互换、常见的四舍五入。"""
    defn = REGISTRY.get(fact_id)
    allowed: set[float] = set()
    if defn is not None and defn.kind in ("pct", "share") and isinstance(value, (int, float)):
        pct = abs(value) * 100
        allowed.update({abs(value), pct, round(pct, 0), round(pct, 1), round(pct, 2)})
    elif defn is not None and defn.kind == "ratio" and isinstance(value, (int, float)):
        base = abs(value)
        allowed.update({base, round(base, 0), round(base, 1), round(base, 2)})
    elif defn is not None and defn.kind in ("int", "days") and isinstance(value, (int, float)):
        allowed.add(abs(float(value)))
    for token in _NUMBER.findall(text or ""):
        allowed.add(float(token))
    return allowed
