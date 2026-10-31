"""基本面事实表：代码算好所有数字，模型只引用编号。比例统一成百分数，倍数写"X 倍"，金额写"亿元"。

输入是 build_detail 的完整结果（带 inputs）和行业环境（板块阶段、近期行业新闻）。返回 (facts, derived)：
facts 给模型看，derived 是原始数值，给模板答案用。
"""

from __future__ import annotations

from app.reviews.core import FactSheet
from app.synthesis.types import Fact

GROUP_ORDER = ("核心业务", "估值", "质量", "股东行为", "行业环境", "数据质量")
UNITS = {"pct": "%", "share": "%", "times": "倍", "yi": "亿元", "days": "天", "count": "", "text": ""}

_QUALITY = (
    ("profitability", "ROE"), ("growth", "三年成长"), ("solvency", "负债率"), ("cash", "现金比"), ("payout", "分红率"),
)
_HOLDER_TYPE = {
    "buyback": "回购", "holder_increase": "增持", "holder_reduce": "减持", "unlock": "解禁",
    "pledge": "质押", "incentive": "股权激励", "holder_count": "股东户数",
}
_IMPACT = {"positive": "利好", "negative": "利空", "none": "中性"}
_STAGE = {
    "start": "启动", "main": "主升", "climax": "高潮", "diverge": "分歧", "ebb": "退潮", "weak": "弱势", "none": "没有明确阶段",
}
_CATEGORY = {"industry": "政策与行业", "macro": "宏观与市场"}
_LEVEL = {
    "valuation": {"cheap": "偏便宜", "fair": "合理", "expensive": "偏贵"},
    "quality": {"strong": "较好", "medium": "中等", "weak": "偏弱"},
    "holders": {"positive": "偏多", "offset": "相抵", "negative": "偏空", "quiet": "没有动作"},
}


def build(payload: dict, context: dict | None = None) -> tuple[FactSheet, dict]:
    context = context or {}
    facts = FactSheet()
    d: dict = {}

    def put(fact_id: str, label: str, group: str, kind: str, value, text: str) -> None:
        if value is None and not text:
            return
        facts.labels[fact_id] = label
        facts.meta[fact_id] = (group, kind)
        facts.append(Fact(fact_id, value, text))

    for cell in payload.get("cells") or []:   # 档位由基本面详情的规则算好，体检直接读，不再另写一份阈值
        d[f"{cell['key']}_level"] = cell.get("level")
    _business(payload, put, d)
    _valuation(payload, put, d)
    _quality(payload, put, d)
    _holders(payload, put, d)
    _industry(payload, context, put, d)
    _data_quality(payload, put, d)
    return facts, d


def public(facts: FactSheet) -> list[dict]:
    """全部事实：编号、名称、分组、类型、单位、原始数值和展示文字。按分组排好。"""
    rows = []
    for fact in facts:
        group, kind = facts.meta.get(fact.id, ("其他", "text"))
        rows.append({
            "id": fact.id, "label": facts.labels.get(fact.id, fact.id), "group": group, "kind": kind, "unit": UNITS.get(kind, ""),
            "value": round(fact.value, 6) if isinstance(fact.value, float) else fact.value, "text": fact.text,
        })
    order = {name: index for index, name in enumerate(GROUP_ORDER)}
    rows.sort(key=lambda row: order.get(row["group"], len(order)))
    return rows


def _pct(value, digits: int = 0) -> str:
    return f"{value * 100:.{digits}f}%"


def _signed(value, digits: int = 0) -> str:
    return f"{value * 100:+.{digits}f}%"


def _times(value) -> str:
    return f"{value:.2f} 倍" if abs(value) < 10 else f"{value:.1f} 倍"


def _business(payload: dict, put, d: dict) -> None:
    business = ((payload.get("inputs") or {}).get("business")) or {}
    description = str(business.get("main_business") or "").strip()
    items = business.get("items") or []
    if description:
        put("fd.main_business", "公司主营业务描述", "核心业务", "text", None, description)
    if not items:
        return
    d["business_items"] = items
    put("fd.biz_period", "主营构成的报告期", "核心业务", "text", None, str(business.get("period") or ""))
    top = items[0]
    put("fd.biz_top", "最大的业务线", "核心业务", "text", None, f"{top['item']}，占营收 {_pct(top['share'], 1)}")
    put("fd.biz_top_share", "最大业务线占营收比例", "核心业务", "share", top["share"], _pct(top["share"], 1))
    lines = []
    for item in items[:4]:
        extra = []
        if item.get("yoy") is not None:
            extra.append(f"同比 {_signed(item['yoy'], 1)}")
        if item.get("gross_margin") is not None:
            extra.append(f"毛利率 {_pct(item['gross_margin'], 1)}")
        lines.append(f"{item['item']} {item['sales_yi']:.1f} 亿元，占 {_pct(item['share'], 1)}" + (f"（{'，'.join(extra)}）" if extra else ""))
    put("fd.biz_lines", "各业务线的营收、占比、同比和毛利率", "核心业务", "text", None, "；".join(lines))


def _valuation(payload: dict, put, d: dict) -> None:
    valuation = payload.get("valuation") or {}
    if not valuation:
        return
    value, pctile, mid = valuation.get("value"), valuation.get("pctile_5y"), valuation.get("peer_median")
    history = valuation.get("history") or {}
    d.update(value=value, pctile=pctile, peer_median=mid)
    put("fd.metric", "估值指标", "估值", "text", None, str(valuation.get("metric_text") or ""))
    if value is not None:
        put("fd.value", "估值指标当前值", "估值", "times", value, _times(value))
    if pctile is not None:
        put("fd.pctile_5y", "当前值在自己历史里的分位", "估值", "share", pctile, f"{round(pctile * 100)}% 分位")
    dates = history.get("dates") or []
    if dates:
        years = max(1, round(len(dates) / 52))
        put("fd.history_span", "历史样本的长度", "估值", "text", None, f"近 {years} 年，共 {len(dates)} 个周样本")
    for key, label in (("p20", "历史 20% 分位线"), ("p50", "历史中位数"), ("p80", "历史 80% 分位线")):
        if history.get(key) is not None:
            put(f"fd.{key}", label, "估值", "times", history[key], _times(history[key]))
    if mid is not None:
        put("fd.peer_median", "同行中位数（不含本公司）", "估值", "times", mid, _times(mid))
        if value is not None and mid:
            gap = value / mid - 1
            d["vs_peer"] = gap
            put("fd.vs_peer", "当前值相对同行中位数", "估值", "pct", gap, f"{'高' if gap > 0 else '低'}于同行中位数 {_pct(abs(gap))}")
    reason = valuation.get("metric_reason")
    put("fd.metric_reason", "换指标的原因", "估值", "text", None, str(reason) if reason else "没有换指标")
    distorted = bool((payload.get("inputs") or {}).get("distorted"))
    d["distorted"] = distorted
    put("fd.distorted", "利润是否薄到让市盈率失真", "估值", "text", None, "是（ROE 低于 5% 或扣非利润同比降超过 50%）" if distorted else "否")


def _quality(payload: dict, put, d: dict) -> None:
    quality = payload.get("quality") or {}
    items = {item["key"]: item for item in quality.get("items") or []}
    if not items:
        return
    d["quality_items"] = items
    for key, label in _QUALITY:
        item = items.get(key)
        if not item:
            continue
        if item.get("value_text"):
            put(f"fd.q_{key}", f"{label}（本公司）", "质量", "text", item.get("value"), str(item["value_text"]))
        if item.get("peer_pctile") is not None:
            put(f"fd.qp_{key}", f"{label}在同行里的分位（越高越好）", "质量", "share", item["peer_pctile"], f"{round(item['peer_pctile'] * 100)}%")
    weakest = quality.get("weakest")
    d["weakest"] = weakest
    put("fd.q_weakest", "最弱的一项", "质量", "text", None, weakest["text"] if weakest else "没有明显弱项（同行分位都高于 20%）")
    years = int((payload.get("inputs") or {}).get("ocf_negative_years") or 0)
    d["ocf_negative_years"] = years
    put("fd.ocf_neg_years", "经营现金流连续为负的年数", "质量", "days", years, f"{years} 年")
    if d.get("quality_level"):
        put("fd.q_level", "规则版对质量的档位", "质量", "text", None, _LEVEL["quality"].get(d["quality_level"], ""))


def _holders(payload: dict, put, d: dict) -> None:
    if d.get("holders_level"):
        put("fd.holders_level", "规则版对股东行为的档位", "股东行为", "text", None, _LEVEL["holders"].get(d["holders_level"], ""))
    events = [event for event in payload.get("holder_events") or [] if event.get("type") in _HOLDER_TYPE]
    d["holder_events"] = events
    for index, event in enumerate(events[:6], start=1):
        days = event.get("days_left")
        when = "进行中或日期未定" if days is None else (f"{int(days)} 个交易日内" if days >= 0 else "已过期")
        text = f"{_HOLDER_TYPE[event['type']]}：{event.get('detail_text') or event.get('title') or ''}，{when}，{_IMPACT.get(event.get('impact'), '中性')}"
        put(f"fd.hold_{index}", f"股东事件 {index}", "股东行为", "text", days, text)
    if not events:
        put("fd.hold_none", "股东事件", "股东行为", "text", None, "近期没有股东动作")


def _industry(payload: dict, context: dict, put, d: dict) -> None:
    name = ((payload.get("meta") or {}).get("industry") or {}).get("name")
    if name:
        put("fd.industry", "所属行业", "行业环境", "text", None, str(name))
    stage = (context.get("sector") or {}).get("stage")
    if stage:
        d["sector_stage"] = stage
        put("fd.sector_stage", "所属板块当前阶段", "行业环境", "text", None, _STAGE.get(stage, stage))
    news = context.get("news")
    if news is None:
        return
    d["industry_news"] = news
    put("fd.industry_news_count", "近期行业层面的新闻条数", "行业环境", "count", len(news), f"{len(news)} 条")
    for index, row in enumerate(news[:5], start=1):
        day = str(row.get("published_at") or "")[5:10]
        put(f"fd.industry_news_{index}", f"行业新闻 {index}", "行业环境", "text", None, f"{day} {row.get('title') or ''}（{_CATEGORY.get(row.get('category'), '行业')}，来源等级 {row.get('source_grade') or '?'}）".strip())


def _data_quality(payload: dict, put, d: dict) -> None:
    meta = payload.get("meta") or {}
    inputs = payload.get("inputs") or {}
    put("fd.price_date", "行情日期", "数据质量", "text", None, str(meta.get("price_date") or ""))
    put("fd.report_period", "最新财报期", "数据质量", "text", None, str(meta.get("report_period") or "未知"))
    behind = inputs.get("periods_behind")
    d["periods_behind"] = behind
    if behind is not None:
        put("fd.periods_behind", "财务数据落后的报告期数", "数据质量", "count", behind, f"{behind} 期")
    count = (meta.get("industry") or {}).get("peer_count")
    d["peer_count"] = count
    if count is not None:
        put("fd.peer_count", "可比同行家数", "数据质量", "count", count, f"{count} 家")
