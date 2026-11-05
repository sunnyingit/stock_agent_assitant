"""事件类型注册表（设计 5.2）。新增一种通知 = 往这里加一行：默认严重度、有效期、文案、跳转。

事件只描述发生了什么；标题和正文在这里按 payload 渲染，策略层不关心内容。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

SEVERITY = {"info": 0, "normal": 1, "urgent": 2}
DAY = 86400


@dataclass(frozen=True)
class EventType:
    type: str
    severity: str
    ttl_s: int
    tone: str                      # good | warn | bad | flat
    render: Callable[[dict], tuple[str, str]]
    link: Callable[[dict], dict] = lambda p: {}
    required: tuple = ()
    group: str = "other"           # 收件箱里的分组：stock | market | system


REGISTRY: dict[str, EventType] = {}


def register(spec: EventType) -> EventType:
    REGISTRY[spec.type] = spec
    return spec


def get(type_: str) -> EventType:
    try:
        return REGISTRY[type_]
    except KeyError:
        raise ValueError(f"未知的事件类型：{type_}") from None


def _name(p: dict) -> str:
    return p.get("name") or p.get("ts_code") or ""


def _stock_link(p: dict, page: str = "holdings", tab: str = "now") -> dict:
    return {"page": page, "tab": tab, "code": p.get("ts_code", "")}


def _num(value, digits: int = 2) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "-"


def _alert(p):
    side = "跌到" if p["side"] == "down" else "涨到"
    title = f"{_name(p)} {side} {_num(p['trigger_price'])}"
    how = "收盘价" if p.get("confirm") == "close" else "现价"
    body = f"{_name(p)}（{p['ts_code']}）{how} {_num(p['observed_price'])}，已{side}设定的 {_num(p['trigger_price'])}。"
    return title, body + (f"\n备注：{p['note']}" if p.get("note") else "")


def _move(p):
    metric = "较昨收" if p.get("metric", "day_pct") == "day_pct" else f"{p.get('window_min', 15)} 分钟内"
    sign = "涨" if p["pct"] >= 0 else "跌"
    return f"{_name(p)} {metric}{sign} {abs(p['pct']):.1f}%", f"{_name(p)}（{p['ts_code']}）现价 {_num(p['observed_price'])}，{metric}{sign}了 {abs(p['pct']):.1f}%。"


def _limit(p):
    word = {"limit_up": "涨停", "limit_down": "跌停", "limit_open": "炸板"}.get(p["kind"], p["kind"])
    extra = f"，开板 {p['times']} 次" if p.get("times") else ""
    return f"{_name(p)} {word}", f"{_name(p)}（{p['ts_code']}）{p.get('trade_date', '')} {word}，收盘 {_num(p.get('close'))}{extra}。"


def _extreme(p):
    word = f"{p['window_days']} 日{'新高' if p['kind'] == 'high' else '新低'}"
    return f"{_name(p)} 创{word}", f"{_name(p)}（{p['ts_code']}）收盘 {_num(p.get('close'))}，创{word}。"


def _announcement(p):
    more = f"\n还有 {p['extra']} 条：" + "；".join(p.get("others") or []) if p.get("extra") else ""
    hint = f"\n标题提到：{'、'.join(p['matched'])}" if p.get("matched") else ""
    return f"{_name(p)} · {p.get('topic') or p.get('category') or '公告'}", f"{p['title']}\n{p.get('source', '')} {p.get('published_at', '')}{hint}{more}".strip()


def _judgment(p):
    return f"{_name(p)} 判断变了", f"{_name(p)}：{p.get('headline', '')}".strip("：")


def _reason(p):
    return f"{_name(p)} 买入理由{p.get('to', '')}", p.get("line") or f"{_name(p)} 的买入理由状态变成了「{p.get('to', '')}」。"


def _wish(p):
    return f"{_name(p)} 想买的条件到了", "、".join(p.get("met") or []) or f"{_name(p)} 满足了想买的条件。"


def _flow(p):
    word = "流入" if p.get("direction") == "in" else "流出"
    return f"{_name(p)} 主力连续{p.get('days')}日净{word}", f"{_name(p)}（{p['ts_code']}）主力资金已连续 {p.get('days')} 个交易日净{word}。"


def _calendar(p):
    return f"{_name(p)} · {p.get('text') or p.get('kind', '')}", f"{_name(p)}（{p['ts_code']}）{p.get('date', '')}：{p.get('text', '')}"


def _outcome(p):
    return f"{_name(p)} 战绩出来了", f"{p.get('entry_date', '')} 的判断，{p.get('horizon', 5)} 个交易日后{'对了' if p.get('hit') else '没对'}（{_num(p.get('ret'), 1)}%）。"


def _task_failed(p):
    return f"定时任务出错：{p.get('title') or p['task_id']}", f"{p['dead']}/{p['total']} 个单元失败。{p.get('first_error', '')}".strip()


def _task_overdue(p):
    return f"定时任务没跑：{p.get('title') or p['task_id']}", f"应该在 {p.get('expected_by', '')} 前完成，现在是「{p.get('state', '')}」。"


def _digest(p):
    counts = p.get("counts") or {}
    return f"{p.get('period_label', '摘要')}：{sum(counts.values()) if counts else len(p.get('items') or [])} 条", "\n".join(f"· {item}" for item in (p.get("items") or [])[:20])


def _generic(p):
    return p["title"], p.get("text", "")


for _spec in (
    EventType("price.alert.fired", "urgent", DAY, "warn", _alert, _stock_link, ("alert_id", "ts_code", "side", "trigger_price", "observed_price"), "stock"),
    EventType("price.move", "normal", DAY, "warn", _move, _stock_link, ("ts_code", "pct", "observed_price"), "stock"),
    EventType("limit.hit", "normal", DAY, "warn", _limit, _stock_link, ("ts_code", "kind"), "stock"),
    EventType("price.extreme", "info", 3 * DAY, "flat", _extreme, _stock_link, ("ts_code", "kind", "window_days"), "stock"),
    EventType("announcement.published", "normal", 3 * DAY, "warn", _announcement, _stock_link, ("ts_code", "item_id", "title"), "stock"),
    EventType("news.item", "info", 7 * DAY, "flat", _announcement, _stock_link, ("ts_code", "item_id", "title"), "stock"),
    EventType("judgment.changed", "normal", 7 * DAY, "warn", _judgment, lambda p: _stock_link(p, "diagnose", ""), ("ts_code",), "stock"),
    EventType("reason.status_changed", "normal", 7 * DAY, "warn", _reason, _stock_link, ("ts_code", "to"), "stock"),
    EventType("wish.condition_met", "normal", 7 * DAY, "good", _wish, lambda p: _stock_link(p, "holdings", "buy"), ("ts_code",), "stock"),
    EventType("flow.streak", "normal", 3 * DAY, "flat", _flow, _stock_link, ("ts_code", "direction", "days"), "stock"),
    EventType("calendar.upcoming", "normal", 7 * DAY, "flat", _calendar, _stock_link, ("ts_code", "kind", "date"), "stock"),
    EventType("outcome.settled", "info", 30 * DAY, "flat", _outcome, _stock_link, ("ts_code",), "stock"),
    EventType("market.level_changed", "normal", 2 * DAY, "warn", lambda p: ("市场档位变了", p.get("headline", "")), lambda p: {"page": "market"}, (), "market"),
    EventType("market.team_changed", "normal", 2 * DAY, "warn", lambda p: ("国家队有动作", p.get("summary", "")), lambda p: {"page": "market", "tab": "team"}, (), "market"),
    EventType("market.sector_phase_changed", "info", 2 * DAY, "flat", lambda p: (f"{p.get('sector', '')} 阶段变了", p.get("why", "")), lambda p: {"page": "market", "tab": "sector"}, (), "market"),
    EventType("market.brief", "normal", DAY, "flat", lambda p: (p.get("title", "市场简报"), p.get("text", "")), lambda p: {"page": "market"}, (), "market"),
    EventType("portfolio.checkup_changed", "normal", 3 * DAY, "warn", lambda p: ("持仓体检有变化", p.get("summary", "")), lambda p: {"page": "holdings", "tab": "chk"}, (), "stock"),
    EventType("task.failed", "normal", 3 * DAY, "bad", _task_failed, lambda p: {"page": "sched", "run_id": p.get("run_id", "")}, ("task_id", "dead", "total"), "system"),
    EventType("task.overdue", "urgent", DAY, "bad", _task_overdue, lambda p: {"page": "sched"}, ("task_id",), "system"),
    EventType("digest.summary", "normal", 3 * DAY, "flat", _digest, lambda p: {}, ("period",), "system"),
    EventType("generic.notice", "normal", DAY, "flat", _generic, lambda p: p.get("link") or {}, ("title",), "market"),
):
    register(_spec)
