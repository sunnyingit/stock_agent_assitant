"""持仓页接口：/api/v1/holdings/...  读的都是现算的，写的只动 data/holdings.db 和提醒表。"""

import logging
from datetime import date

from flask import Blueprint, jsonify, request

from app.services import holdings_page as page
from app.services.holdings_data import Data
from app.services.holdings_store import get_store

bp = Blueprint("holdings", __name__, url_prefix="/api/v1/holdings")
log = logging.getLogger(__name__)

TABS = ("buy", "now", "chk", "rev")


def _ctx() -> page.Context:
    from app.notify import pipeline

    return page.Context(get_store(), Data(date.today()), date.today(), inbox=pipeline.current().store)


def _body() -> dict:
    return request.get_json(silent=True) or {}


def _fail(exc: Exception, label: str):
    if isinstance(exc, ValueError):
        return jsonify({"error": str(exc)}), 400
    log.exception("holdings %s failed", label)
    return jsonify({"error": str(exc)}), 500


@bp.route("/summary", methods=["GET"])
def summary():
    try:
        return jsonify({"status": "ok", **page.summary(_ctx())})
    except Exception as exc:
        return _fail(exc, "summary")


@bp.route("/tab/<name>", methods=["GET"])
def tab(name: str):
    if name not in TABS:
        return jsonify({"error": "name 必须是 " + "、".join(TABS)}), 400
    try:
        ctx = _ctx()
        body = {"buy": page.tab_buy, "chk": page.tab_check, "rev": page.tab_review}.get(name)
        return jsonify({"status": "ok", **(body(ctx) if body else {})})
    except Exception as exc:
        return _fail(exc, f"tab {name}")


@bp.route("/checkup", methods=["POST"])
def checkup():
    """重新体检：算一遍，存一份快照，下次体检拿它做对比。"""
    try:
        return jsonify({"status": "ok", **page.tab_check(_ctx(), save=True)})
    except Exception as exc:
        return _fail(exc, "checkup")


@bp.route("/stock/<ts_code>", methods=["GET"])
def stock(ts_code: str):
    try:
        ctx = _ctx()
        found = ctx.data.resolve(ts_code)
        detail = page.stock_detail(ctx, found["ts_code"] if found else ts_code)
        if detail is None:
            return jsonify({"error": f"持仓和想买里没有: {ts_code}"}), 404
        return jsonify({"status": "ok", **detail})
    except Exception as exc:
        return _fail(exc, "stock")


@bp.route("/stock/<ts_code>/reason", methods=["PUT"])
def stock_reason(ts_code: str):
    body = _body()
    try:
        ctx = _ctx()
        found = ctx.data.resolve(ts_code)
        row = page.set_reason(ctx, found["ts_code"] if found else ts_code, str(body.get("text") or ""), body.get("invalid"))
        return jsonify({"status": "ok", "reason": row})
    except Exception as exc:
        return _fail(exc, "reason")


@bp.route("/stock/<ts_code>/price-line", methods=["POST"])
def stock_price_line(ts_code: str):
    """详情里「设一个价格」：补一条收盘跌破的提醒。"""
    try:
        price = float(_body().get("price"))
    except (TypeError, ValueError):
        return jsonify({"error": "price 必须是数字"}), 400
    try:
        ctx = _ctx()
        found = ctx.data.resolve(ts_code)
        page.set_price_line(ctx, found["ts_code"] if found else ts_code, price)
        return jsonify({"status": "ok"})
    except Exception as exc:
        return _fail(exc, "price-line")


@bp.route("/stock/<ts_code>/rules", methods=["POST"])
def rule_add(ts_code: str):
    body = _body()
    try:
        found = Data().resolve(ts_code)
        row = get_store().add_rule(found["ts_code"] if found else ts_code, str(body.get("kind") or ""), float(body.get("value") or 0))
        return jsonify({"status": "ok", "rule": row}), 201
    except Exception as exc:
        return _fail(exc, "rule add")


@bp.route("/rules/<int:rule_id>", methods=["DELETE"])
def rule_remove(rule_id: int):
    if not get_store().remove_rule(rule_id):
        return jsonify({"error": f"没有这条规则: {rule_id}"}), 404
    return jsonify({"status": "ok", "id": rule_id})


# ---------------------------------------------------------------- 流水
@bp.route("/trades", methods=["POST"])
def trade_add():
    try:
        return jsonify({"status": "ok", **page.record_trade(_ctx(), _body())}), 201
    except Exception as exc:
        return _fail(exc, "trade add")


@bp.route("/trades/preview", methods=["POST"])
def trade_preview():
    try:
        return jsonify({"status": "ok", **page.trade_preview(_ctx(), _body())})
    except Exception as exc:
        return _fail(exc, "trade preview")


@bp.route("/trades/<int:trade_id>", methods=["DELETE"])
def trade_remove(trade_id: int):
    try:
        if not get_store().delete_trade(trade_id):
            return jsonify({"error": f"没有这笔记录: {trade_id}"}), 404
        return jsonify({"status": "ok", "id": trade_id})
    except Exception as exc:
        return _fail(exc, "trade remove")


@bp.route("/trades/<int:trade_id>/note", methods=["PUT"])
def trade_note(trade_id: int):
    if not get_store().set_trade_note(trade_id, str(_body().get("note") or "")):
        return jsonify({"error": f"没有这笔记录: {trade_id}"}), 404
    return jsonify({"status": "ok", "id": trade_id})


@bp.route("/import", methods=["POST"])
def import_text():
    text = str(_body().get("text") or "")
    if not text.strip():
        return jsonify({"error": "text 不能为空，把券商导出的持仓贴进来"}), 400
    try:
        return jsonify({"status": "ok", **page.import_positions(_ctx(), text)})
    except Exception as exc:
        return _fail(exc, "import")


# ---------------------------------------------------------------- 想买
@bp.route("/candidates", methods=["GET"])
def candidates():
    try:
        return jsonify({"status": "ok", **page.candidates(_ctx())})
    except Exception as exc:
        return _fail(exc, "candidates")


@bp.route("/wishes/preview", methods=["POST"])
def wish_preview():
    body = _body()
    try:
        plan = float(body.get("plan_pct") or 0)
        return jsonify({"status": "ok", **page.wish_preview(_ctx(), str(body.get("ts_code") or body.get("q") or ""), plan)})
    except Exception as exc:
        return _fail(exc, "wish preview")


@bp.route("/wishes", methods=["POST"])
def wish_add():
    try:
        return jsonify({"status": "ok", "wish": page.add_wish(_ctx(), _body())}), 201
    except Exception as exc:
        return _fail(exc, "wish add")


@bp.route("/wishes/<int:wish_id>", methods=["DELETE"])
def wish_remove(wish_id: int):
    if not get_store().close_wish(wish_id, "dropped"):
        return jsonify({"error": f"没有这条想买: {wish_id}"}), 404
    return jsonify({"status": "ok", "id": wish_id})


# ---------------------------------------------------------------- 通知
@bp.route("/notifications", methods=["GET"])
def notifications():
    try:
        return jsonify({"status": "ok", **page.notifications(_ctx())})
    except Exception as exc:
        return _fail(exc, "notifications")


@bp.route("/notifications/read", methods=["POST"])
def notifications_read():
    try:
        return jsonify({"status": "ok", "count": page.mark_read(_ctx())})
    except Exception as exc:
        return _fail(exc, "notifications read")
