"""面板用的详情接口。"""

import logging

from flask import Blueprint, jsonify, request

from app.tools.stock import resolve_stock

bp = Blueprint("v1", __name__, url_prefix="/api/v1")
log = logging.getLogger(__name__)


def _notify(stock, dimension: str) -> None:
    """通知综合判断：这个维度刚算过。后台线程没起时什么都不做。"""
    try:
        from app.synthesis.worker import notify

        notify(stock.ts_code, dimension)
    except Exception:
        pass


def _analyst_message(dimension: str, name: str, payload: dict) -> dict:
    """群聊里一位分析师的发言。

    五维详情：规则 verdict 不算分析师结论，失败时详情页才回退显示；群聊气泡保持无结论。
    """
    from app.services.analyst_reply import analysis_message

    headline = payload.get("headline") if isinstance(payload, dict) else None
    if not headline or not headline.get("text"):
        return analysis_message(dimension, name, None)
    return analysis_message(dimension, name, headline)


def _event_stream(dimension: str, name: str, load):
    """start → message（与详情相同的 headline）→ done。"""
    from flask import Response

    def generate():
        import json

        def send(event, data):
            return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

        yield send("start", {"dimension": dimension, "name": name})
        try:
            payload = load()
        except Exception as exc:
            log.exception("%s stream failed", dimension)
            yield send("error", {"dimension": dimension, "error": str(exc)})
            return
        yield send("message", _analyst_message(dimension, name, payload))
        yield send("done", {"dimension": dimension})

    return Response(generate(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _rule_flow_review(stock, payload: dict, aligned: dict) -> dict:
    """资金流逐项解读：只用规则模板，不调模型。失败不影响详情数字。"""
    try:
        from app.flow_review import review as flow_review
        from app.reviews.attach import _public
        from app.synthesis.worker import get_store

        result = flow_review.build(
            stock, store=get_store(), gateway=None,
            payload=payload, aligned=aligned,
        )
        return {"review": _public(result), "review_state": "ready", "review_error": ""}
    except Exception as exc:
        return {"review": None, "review_state": "unavailable", "review_error": f"规则解读失败: {exc}"}


def _volume_flow_page(stock, window: int, *, wait: bool = False) -> dict:
    """详情：规则数字 + 规则逐项；分析师结论仅在 stream（wait=True）写入后，detail 只读同步。"""
    from datetime import date

    from app.services.volume_flow import analyst_concl, build_detail
    from app.services.volume_flow.align import load_aligned
    from app.services.volume_flow.detail import public
    from app.services.volume_flow.headline import headline
    from app.synthesis.worker import get_store

    aligned = load_aligned(stock)
    payload = build_detail(stock, window, aligned=aligned, today=date.today())
    _notify(stock, "volume_flow")
    extra = _rule_flow_review(stock, payload, aligned)
    body = {**public(payload), **extra}
    store = get_store()
    if wait:
        # 资金流分析师跑完：有第 10 题就落库；没有则记失败，详情才回退显示规则 verdict
        shown = headline(extra.get("review"), allow_template=True)
        analyst_concl.save(store, stock.ts_code, shown)
        body["headline"] = shown
        body["analyst_failed"] = shown is None
    else:
        body["headline"] = analyst_concl.load(store, stock.ts_code)
        body["analyst_failed"] = analyst_concl.failed(store, stock.ts_code)
    return body


def _rule_fund_review(stock, payload: dict) -> dict:
    """基本面逐项解读：只用规则模板，不调模型。失败不影响详情数字。"""
    try:
        from app.fundamental_review import review as fund_review
        from app.reviews.attach import _public
        from app.reviews.core import Unavailable
        from app.synthesis.worker import get_store

        result = fund_review.build(stock, store=get_store(), gateway=None, payload=payload)
        return {"review": _public(result), "review_state": "ready", "review_error": ""}
    except Exception as exc:
        from app.reviews.core import Unavailable

        if isinstance(exc, Unavailable):
            return {"review": None, "review_state": "unavailable", "review_error": str(exc)}
        return {"review": None, "review_state": "unavailable", "review_error": f"规则解读失败: {exc}"}


def _fundamental_page(stock, *, wait: bool = False) -> dict:
    """详情：规则数字 + 规则逐项；分析师结论仅在 stream（wait=True）写入后，detail 只读同步。"""
    from app.fundamental_review.review import headline_from_review
    from app.services.fundamental_detail import analyst_concl, build_detail
    from app.services.fundamental_detail.detail import public
    from app.synthesis.worker import get_store

    payload = build_detail(stock)
    _notify(stock, "fundamental")
    meta = payload.get("meta") or {}
    body = public(payload)
    store = get_store()
    if meta.get("state") == "ok":
        extra = _rule_fund_review(stock, payload)
    elif meta.get("state") == "computing":
        extra = {"review": None, "review_state": "computing", "review_error": ""}
    else:
        extra = {"review": None, "review_state": "unavailable", "review_error": meta.get("status_text") or ""}
    body = {**body, **extra}
    if wait and meta.get("state") == "ok":
        shown = headline_from_review(extra.get("review"), allow_template=True)
        analyst_concl.save(store, stock.ts_code, shown)
        body["headline"] = shown
        body["analyst_failed"] = shown is None
    else:
        body["headline"] = analyst_concl.load(store, stock.ts_code)
        body["analyst_failed"] = analyst_concl.failed(store, stock.ts_code)
    return body


def _rule_fin_review(stock, payload: dict) -> dict:
    """财报逐项解读：只用规则模板，不调模型。失败不影响详情数字。"""
    try:
        from app.finance_review import review as fin_review
        from app.reviews.attach import _public
        from app.synthesis.worker import get_store

        result = fin_review.build(stock, store=get_store(), gateway=None, payload=payload)
        return {"review": _public(result), "review_state": "ready", "review_error": ""}
    except Exception as exc:
        from app.reviews.core import Unavailable

        if isinstance(exc, Unavailable):
            return {"review": None, "review_state": "unavailable", "review_error": str(exc)}
        return {"review": None, "review_state": "unavailable", "review_error": f"规则解读失败: {exc}"}


def _finance_page(stock, *, wait: bool = False) -> dict:
    """详情：规则数字 + 规则逐项；分析师结论仅在 stream（wait=True）写入后，detail 只读同步。"""
    from app.finance_review.review import headline
    from app.services.finance_detail import analyst_concl, build_detail
    from app.services.finance_detail.detail import public
    from app.synthesis.worker import get_store

    payload = build_detail(stock)
    _notify(stock, "finance")
    meta = payload.get("meta") or {}
    body = public(payload)
    store = get_store()
    if meta.get("state") == "ok":
        extra = _rule_fin_review(stock, payload)
    elif meta.get("state") == "computing":
        extra = {"review": None, "review_state": "computing", "review_error": ""}
    else:
        extra = {"review": None, "review_state": "unavailable", "review_error": meta.get("status_text") or ""}
    body = {**body, **extra}
    days = (meta.get("report") or {}).get("days_since")
    if wait and meta.get("state") == "ok":
        shown = headline(extra.get("review"), days, allow_template=True)
        analyst_concl.save(store, stock.ts_code, shown)
        body["headline"] = shown
        body["analyst_failed"] = shown is None
    else:
        body["headline"] = analyst_concl.load(store, stock.ts_code)
        body["analyst_failed"] = analyst_concl.failed(store, stock.ts_code)
    return body


def _rule_sector_review(stock, payload: dict) -> dict:
    """板块逐项解读：只用规则模板，不调模型。失败不影响详情数字。"""
    try:
        from app.sector_review import review as sector_review
        from app.reviews.attach import _public
        from app.synthesis.worker import get_store

        result = sector_review.build(stock, store=get_store(), gateway=None, payload=payload)
        return {"review": _public(result), "review_state": "ready", "review_error": ""}
    except Exception as exc:
        from app.reviews.core import Unavailable

        if isinstance(exc, Unavailable):
            return {"review": None, "review_state": "unavailable", "review_error": str(exc)}
        return {"review": None, "review_state": "unavailable", "review_error": f"规则解读失败: {exc}"}


def _sector_page(stock, *, wait: bool = False) -> dict:
    """详情：规则数字 + 规则逐项；分析师结论仅在 stream（wait=True）写入后，detail 只读同步。"""
    from app.sector_review.review import headline
    from app.services.sector_detail import analyst_concl, build_detail
    from app.services.sector_detail.detail import public
    from app.synthesis.worker import get_store

    payload = build_detail(stock)
    _notify(stock, "sector")
    state = (payload.get("meta") or {}).get("state")
    body = public(payload)
    store = get_store()
    if state in ("ok", "intraday", "stale"):
        extra = _rule_sector_review(stock, payload)
    elif state == "computing":
        extra = {"review": None, "review_state": "computing", "review_error": ""}
    else:
        extra = {"review": None, "review_state": "unavailable", "review_error": (payload.get("meta") or {}).get("status_text") or ""}
    body = {**body, **extra}
    if wait and state in ("ok", "intraday", "stale"):
        shown = headline(extra.get("review"), allow_template=True)
        analyst_concl.save(store, stock.ts_code, shown)
        body["headline"] = shown
        body["analyst_failed"] = shown is None
    else:
        body["headline"] = analyst_concl.load(store, stock.ts_code)
        body["analyst_failed"] = analyst_concl.failed(store, stock.ts_code)
    return body


def _window():
    from app.services.volume_flow.rules import DEFAULT_WINDOW

    raw = request.args.get("window", "")
    if raw in (None, ""):
        return DEFAULT_WINDOW, None
    try:
        days = int(raw)
    except (TypeError, ValueError):
        return None, (jsonify({"error": "window 必须是正整数"}), 400)
    if days < 1:
        return None, (jsonify({"error": "window 必须是正整数"}), 400)
    return min(days, 60), None


@bp.route("/stocks/<ts_code>/dimensions/volume_flow/detail", methods=["GET"])
def volume_flow_detail(ts_code: str):
    window, err = _window()
    if err:
        return err
    stock = resolve_stock(ts_code)
    if stock is None:
        return jsonify({"error": f"未找到股票: {ts_code}"}), 404
    try:
        return jsonify(_volume_flow_page(stock, window))
    except Exception as exc:
        log.exception("%s detail failed: %s", "volume_flow", stock.ts_code)
        return jsonify({
            "status": "error",
            "ts_code": stock.ts_code,
            "stock_name": stock.name,
            "error": str(exc),
        }), 500


@bp.route("/stocks/<ts_code>/dimensions/volume_flow/reply", methods=["GET"])
def volume_flow_reply(ts_code: str):
    from app.services.flow_reply import PARTS, build

    part = (request.args.get("part") or "").strip()
    if part not in PARTS:
        return jsonify({"error": "part 必须是 dc、factor、chip、bar、premarket、limit、toplist、margin"}), 400
    stock = resolve_stock(ts_code)
    if stock is None:
        return jsonify({"error": f"未找到股票: {ts_code}"}), 404
    try:
        return jsonify(build(stock, part, request.args.get("days")))
    except Exception as exc:
        log.exception("volume_flow reply failed: %s", stock.ts_code)
        return jsonify({"error": str(exc)}), 500


@bp.route("/stocks/<ts_code>/dimensions/volume_flow/stream", methods=["GET"])
def volume_flow_stream(ts_code: str):
    window, err = _window()
    if err:
        return err
    stock = resolve_stock(ts_code)
    if stock is None:
        return jsonify({"error": f"未找到股票: {ts_code}"}), 404
    return _event_stream("volume_flow", "资金流分析师", lambda: _volume_flow_page(stock, window, wait=True))


def _days():
    raw = request.args.get("days", "")
    if raw in (None, ""):
        return 3, None
    try:
        days = int(raw)
    except (TypeError, ValueError):
        return None, (jsonify({"error": "days 必须是正整数"}), 400)
    if days < 1:
        return None, (jsonify({"error": "days 必须是正整数"}), 400)
    return min(days, 20), None


@bp.route("/stocks/<ts_code>/dimensions/news/detail", methods=["GET"])
def news_detail(ts_code: str):
    days, err = _days()
    if err:
        return err
    stock = resolve_stock(ts_code)
    if stock is None:
        return jsonify({"error": f"未找到股票: {ts_code}"}), 404
    from app.services import news_analyst_concl as analyst_concl
    from app.services.news_detail import build_detail, public
    from app.synthesis.worker import get_store

    try:
        # 详情不调模型：规则清洗 + 规则结论；分析师方向只读 stream 落库结果
        payload = public(build_detail(stock, days, model=False))
        _notify(stock, "news")
        store = get_store()
        payload["headline"] = analyst_concl.load(store, stock.ts_code)
        payload["analyst_failed"] = analyst_concl.failed(store, stock.ts_code)
        return jsonify(payload)
    except Exception as exc:
        log.exception("%s detail failed: %s", "news", stock.ts_code)
        return jsonify({
            "ts_code": stock.ts_code,
            "stock_name": stock.name,
            "error": str(exc),
        }), 500


@bp.route("/stocks/<ts_code>/dimensions/news/stream", methods=["GET"])
def news_stream(ts_code: str):
    """新闻分析师发言。事件顺序：start → message（headline）→ done。"""
    days, err = _days()
    if err:
        return err
    stock = resolve_stock(ts_code)
    if stock is None:
        return jsonify({"error": f"未找到股票: {ts_code}"}), 404
    from flask import Response

    from app.services.news_detail import stream_events

    def generate():
        import json

        for item in stream_events(stock, days):
            body = json.dumps(item["data"], ensure_ascii=False)
            yield f"event: {item['event']}\ndata: {body}\n\n"

    return Response(generate(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@bp.route("/stocks/<ts_code>/dimensions/fundamental/reply", methods=["GET"])
def fundamental_reply(ts_code: str):
    from app.services.fundamental_reply import PARTS, build

    part = (request.args.get("part") or "").strip()
    if part not in PARTS:
        return jsonify({"error": "part 必须是 " + "、".join(PARTS)}), 400
    stock = resolve_stock(ts_code)
    if stock is None:
        return jsonify({"error": f"未找到股票: {ts_code}"}), 404
    try:
        return jsonify(build(stock, part))
    except Exception as exc:
        log.exception("fundamental reply failed: %s", stock.ts_code)
        return jsonify({"error": str(exc)}), 500


@bp.route("/stocks/<ts_code>/dimensions/fundamental/detail", methods=["GET"])
def fundamental_detail(ts_code: str):
    stock = resolve_stock(ts_code)
    if stock is None:
        return jsonify({"error": f"未找到股票: {ts_code}"}), 404
    from app.services.fundamental_detail import failed_payload

    try:
        return jsonify(_fundamental_page(stock))
    except Exception as exc:
        log.exception("%s detail failed: %s", "fundamental", stock.ts_code)
        return jsonify(failed_payload(stock, str(exc))), 500


@bp.route("/stocks/<ts_code>/dimensions/fundamental/stream", methods=["GET"])
def fundamental_stream(ts_code: str):
    stock = resolve_stock(ts_code)
    if stock is None:
        return jsonify({"error": f"未找到股票: {ts_code}"}), 404
    return _event_stream("fundamental", "基本面分析师", lambda: _fundamental_page(stock, wait=True))


@bp.route("/stocks/<ts_code>/dimensions/finance/detail", methods=["GET"])
def finance_detail(ts_code: str):
    stock = resolve_stock(ts_code)
    if stock is None:
        return jsonify({"error": f"未找到股票: {ts_code}"}), 404
    from app.services.finance_detail import failed_payload

    try:
        return jsonify(_finance_page(stock))
    except Exception as exc:
        log.exception("%s detail failed: %s", "finance", stock.ts_code)
        return jsonify(failed_payload(stock, str(exc))), 500


@bp.route("/stocks/<ts_code>/dimensions/finance/stream", methods=["GET"])
def finance_stream(ts_code: str):
    stock = resolve_stock(ts_code)
    if stock is None:
        return jsonify({"error": f"未找到股票: {ts_code}"}), 404
    return _event_stream("finance", "财报分析师", lambda: _finance_page(stock, wait=True))


@bp.route("/market/summary", methods=["GET"])
def market_summary():
    from app.services.market_page import build_summary

    try:
        return jsonify(build_summary())
    except Exception as exc:
        log.exception("market summary failed")
        text = str(exc).strip()
        if not text or text == "None":
            text = type(exc).__name__
        return jsonify({"error": text}), 500


@bp.route("/market/tab/<name>", methods=["GET"])
def market_tab(name: str):
    from app.services.market_page import TABS, build_tab

    if name not in TABS:
        return jsonify({"error": "name 必须是 " + "、".join(TABS)}), 400
    try:
        return jsonify(build_tab(name))
    except Exception as exc:
        log.exception("market tab failed: %s", name)
        return jsonify({"error": str(exc)}), 500


@bp.route("/market/changes", methods=["GET"])
def market_changes():
    from app.services.market_page import changes_view

    mode = (request.args.get("mode") or "today").strip()
    if mode not in ("last", "today", "week"):
        return jsonify({"error": "mode 必须是 last、today、week"}), 400
    try:
        since = float(request.args["since"]) / 1000 if request.args.get("since") else None
        return jsonify(changes_view(mode, since))
    except Exception as exc:
        log.exception("market changes failed")
        return jsonify({"error": str(exc)}), 500


@bp.route("/market/history", methods=["GET"])
def market_history():
    from app.services.market_page import history_view

    return jsonify({"rows": history_view()})


@bp.route("/market/intraday", methods=["GET"])
def market_intraday():
    from app.services.market_intraday import build

    return jsonify(build())


@bp.route("/market/notifications", methods=["GET"])
def market_notifications():
    from app.notify import pageview, pipeline
    from app.services.market_page import notify_payload

    try:
        return jsonify(notify_payload(inbox=pageview.for_market(pipeline.current().store)))
    except Exception as exc:
        log.exception("market notifications failed")
        return jsonify({"error": str(exc)}), 500


@bp.route("/market/notifications/read", methods=["POST"])
def market_notifications_read():
    from app.services.market_store import get_store

    from app.notify import pageview, pipeline

    body = request.get_json(silent=True) or {}
    ids = body.get("ids")
    notifier = pipeline.current()
    own = None if ids is None else [int(item) for item in ids if not str(item).startswith(pageview.PREFIX)]
    get_store().mark_read(own)
    pageview.mark_read(notifier.store, ids, pageview.for_market(notifier.store), notifier.clock.now_ms())
    return jsonify({"status": "ok", "unread": get_store().unread() + sum(1 for i in pageview.for_market(notifier.store) if i["read_at"] is None)})


@bp.route("/market/settings", methods=["GET", "PUT"])
def market_settings():
    from app.services import market_notify
    from app.services.market_store import get_store

    if request.method == "PUT":
        try:
            get_store().set_settings(market_notify.clean_settings(request.get_json(silent=True) or {}))
        except (ValueError, TypeError) as exc:
            return jsonify({"error": str(exc)}), 400
    return jsonify({"settings": market_notify.settings(), "channel": market_notify._channel()})


@bp.route("/market/etf/<code>", methods=["GET"])
def market_etf(code: str):
    from app.services.market_etf import detail

    try:
        days = int(request.args.get("days") or 60)
    except ValueError:
        return jsonify({"error": "days 必须是数字"}), 400
    try:
        return jsonify(detail(code.upper(), days))
    except KeyError:
        return jsonify({"error": f"市场页没有跟踪 {code}"}), 404
    except Exception as exc:
        log.exception("market etf failed: %s", code)
        return jsonify({"error": str(exc)}), 500


@bp.route("/market", methods=["GET"])
def market_tape():
    from app.services.market_tape import PARTS, build

    part = (request.args.get("part") or "").strip()
    if part not in PARTS:
        return jsonify({"error": "part 必须是 " + "、".join(PARTS)}), 400
    try:
        return jsonify(build(part))
    except Exception as exc:
        log.exception("market tape failed: %s", part)
        return jsonify({"error": str(exc)}), 500


@bp.route("/stocks/<ts_code>/dimensions/sector/reply", methods=["GET"])
def sector_reply(ts_code: str):
    from app.services.sector_reply import PARTS, build

    part = (request.args.get("part") or "").strip()
    if part not in PARTS:
        return jsonify({"error": "part 必须是 membership、tape、position、rank、heat"}), 400
    stock = resolve_stock(ts_code)
    if stock is None:
        return jsonify({"error": f"未找到股票: {ts_code}"}), 404
    try:
        return jsonify(build(stock, part))
    except Exception as exc:
        log.exception("sector reply failed: %s", stock.ts_code)
        return jsonify({"error": str(exc)}), 500


@bp.route("/stocks/<ts_code>/dimensions/sector/detail", methods=["GET"])
def sector_detail(ts_code: str):
    stock = resolve_stock(ts_code)
    if stock is None:
        return jsonify({"error": f"未找到股票: {ts_code}"}), 404
    from app.services.sector_detail import failed_payload

    try:
        return jsonify(_sector_page(stock))
    except Exception as exc:
        log.exception("%s detail failed: %s", "sector", stock.ts_code)
        return jsonify(failed_payload(stock, str(exc))), 500


@bp.route("/stocks/<ts_code>/dimensions/sector/stream", methods=["GET"])
def sector_stream(ts_code: str):
    stock = resolve_stock(ts_code)
    if stock is None:
        return jsonify({"error": f"未找到股票: {ts_code}"}), 404
    return _event_stream("sector", "板块分析师", lambda: _sector_page(stock, wait=True))


_ANALYSTS = {
    "news": "新闻分析师",
    "volume_flow": "资金流分析师",
    "fundamental": "基本面分析师",
    "finance": "财报分析师",
    "sector": "板块分析师",
}


def _analyst_page(stock, dimension: str) -> dict:
    if dimension == "news":
        from app.services.news_detail import build_detail, public

        return public(build_detail(stock))
    if dimension == "volume_flow":
        from app.services.volume_flow.rules import DEFAULT_WINDOW

        return _volume_flow_page(stock, DEFAULT_WINDOW, wait=True)
    if dimension == "fundamental":
        return _fundamental_page(stock, wait=True)
    if dimension == "finance":
        return _finance_page(stock, wait=True)
    return _sector_page(stock, wait=True)


@bp.route("/stocks/<ts_code>/dimensions/<dimension>/reply", methods=["POST"])
def dimension_reply(ts_code: str, dimension: str):
    """追问一位维度分析师。消息外壳和第一次分析相同，kind 为 reply。"""
    name = _ANALYSTS.get(dimension)
    if name is None:
        return jsonify({"error": f"没有这位分析师: {dimension}"}), 404
    body = request.get_json(silent=True) or {}
    question = str(body.get("question") or request.args.get("q") or "").strip()
    if not question:
        return jsonify({"error": "缺少 question"}), 400
    stock = resolve_stock(ts_code)
    if stock is None:
        return jsonify({"error": f"未找到股票: {ts_code}"}), 404
    from flask import Response

    from app.services.analyst_reply import answer, reply_message

    def generate():
        import json

        def send(event, data):
            return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

        yield send("start", {"dimension": dimension, "name": name, "kind": "reply"})
        try:
            page = _analyst_page(stock, dimension)
            headline = page.get("headline") if isinstance(page, dict) else None
            data = answer(name, question, headline)
            payload = reply_message(dimension, name, question, headline, data)
        except Exception as exc:
            log.exception("%s reply failed", dimension)
            yield send("error", {"dimension": dimension, "error": str(exc)})
            return
        yield send("message", payload)
        yield send("done", {"dimension": dimension})

    return Response(generate(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
