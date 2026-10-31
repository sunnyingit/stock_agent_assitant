"""综合判断接口。页面只读存档；没有存档时先出规则版，再排队补 Agent 版。"""

from __future__ import annotations

import time

from flask import Blueprint, jsonify, request

from app.synthesis import policy, quotes, watchlist, worker
from app.synthesis.calendar import last_close_date, now_cn, session_of
from app.synthesis.service import compute, current_mode
from app.synthesis.types import FactTable
from app.tools.stock import resolve_stock

bp = Blueprint("synthesis", __name__, url_prefix="/api/v1")

REFRESH_AFTER_INTRADAY = 300   # 盘中存档超过 5 分钟就排队重算


def _page(record: dict, mode: str) -> dict:
    from app.synthesis.display import explain_fields

    data = record["data"]
    use_agent = mode == "agent" and data.get("display_agent")
    page = data["display_agent"] if use_agent else data["display_rule"]
    if all(page.get(key) for key in ("level_logic", "judgment_logic", "price_logic", "judgment")):
        return page
    page = dict(page)
    decision = data.get("decision") or {}
    final = decision.get("final") if use_agent else None
    page.update(explain_fields(
        data.get("levels_agent") if use_agent else data.get("levels_rule"),
        data.get("rule"),
        final,
        decision.get("sides") if use_agent else None,
        FactTable.from_dict(data["table"]) if data.get("table") else None,
        data.get("fallback_reason") or "",
        (page.get("position") or {}).get("text", ""),
    ))
    return page


def _stale(record: dict) -> bool:
    if record["policy_version"] != policy.POLICY_VERSION:   # 规则或菜单改过：旧存档要重算
        return True
    now = now_cn()
    if session_of(now) == "intraday":
        return time.time() - record["created_at"] > REFRESH_AFTER_INTRADAY
    return record["trade_date"] != last_close_date(now).isoformat() or record["session"] == "intraday"


@bp.route("/stocks/<ts_code>/synthesis", methods=["GET"])
def synthesis(ts_code: str):
    stock = resolve_stock(ts_code)
    if stock is None:
        return jsonify({"error": f"未找到股票: {ts_code}"}), 404
    store = worker.get_store()
    mode = current_mode()
    try:
        record = store.latest(stock.ts_code)
        if record is None:
            reply = compute(stock, store, mode="rule")
            if mode != "rule" or reply.get("pending"):
                worker.request(stock.ts_code, "first_view", delay=1.0)
            body = {"display": reply["display"], "internal": reply["internal"]}
        else:
            if _stale(record):
                worker.request(stock.ts_code, "stale", delay=5.0)
            body = {
                "display": _page(record, mode),
                "internal": {"run_id": record["id"], "source": record["source"], "mode": record["mode"]},
            }
    except Exception as exc:
        return jsonify({"status": "error", "ts_code": stock.ts_code, "stock_name": stock.name, "error": str(exc)}), 500
    response = {
        "status": "ok",
        "ts_code": stock.ts_code,
        "stock_name": stock.name,
        "computing": store.pending(stock.ts_code),
        "display": _with_alerts(body["display"], store.alerts(stock.ts_code)),
    }
    if request.args.get("debug") == "1":
        response["internal"] = body["internal"]
    return jsonify(response)


def _with_alerts(page: dict, alerts: list[dict]) -> dict:
    """价位卡上"已设提醒 / 到了提醒我"的状态。提醒的价和确认条件固定在提醒里，不随价位重新生成而变。"""
    page = dict(page)
    levels = {}
    for side, level in (page.get("levels") or {}).items():
        if level:
            level = dict(level)
            hit = next((a for a in alerts if a["side"] == side and abs(a["price"] - level["price"]) < 0.005), None)
            level["alert"] = {"subscribed": hit is not None, "alert_id": hit["id"] if hit else None}
        levels[side] = level
    page["levels"] = levels
    return page


@bp.route("/stocks/<ts_code>/synthesis/refresh", methods=["POST"])
def refresh(ts_code: str):
    stock = resolve_stock(ts_code)
    if stock is None:
        return jsonify({"error": f"未找到股票: {ts_code}"}), 404
    worker.request(stock.ts_code, "manual", delay=0.0)
    return jsonify({"status": "queued", "ts_code": stock.ts_code}), 202


# ---------------------------------------------------------------- 自选（侧栏"诊断"）
def _items(store, only=None):
    return watchlist.build(
        store, quotes.fetch, current_mode(), now_cn(),
        request_refresh=lambda code: worker.request(code, "watchlist", delay=5.0), only=only,
    )


@bp.route("/watchlist", methods=["GET"])
def watchlist_list():
    store = worker.get_store()
    return jsonify({"status": "ok", "updated_at": now_cn().strftime("%Y-%m-%d %H:%M:%S"), "items": _items(store)})


@bp.route("/watchlist", methods=["POST"])
def watchlist_add():
    body = request.get_json(silent=True) or {}
    query = str(body.get("q") or body.get("code") or body.get("name") or "").strip()
    if not query:
        return jsonify({"error": "缺少参数，请传入 q=股票名称或代码"}), 400
    stock = resolve_stock(query)
    if stock is None:
        return jsonify({"error": f"未找到股票: {query}"}), 404
    store = worker.get_store()
    added = store.watch_add(stock.ts_code, stock.name)
    if added:
        worker.request(stock.ts_code, "watch_add", delay=1.0)   # 新加的股票马上排队出判断
    items = _items(store, only=[stock.ts_code])
    return jsonify({"status": "ok", "added": added, "item": items[0] if items else None}), 201 if added else 200


@bp.route("/watchlist/<ts_code>", methods=["DELETE"])
def watchlist_remove(ts_code: str):
    stock = resolve_stock(ts_code)
    code = stock.ts_code if stock else ts_code
    if not worker.get_store().watch_remove(code):
        return jsonify({"error": f"自选里没有: {ts_code}"}), 404
    return jsonify({"status": "ok", "ts_code": code})


@bp.route("/stocks/search", methods=["GET"])
def stocks_search():
    """搜索框：按名称或代码找股票，标出哪些已经在自选里。"""
    query = (request.args.get("q") or "").strip()
    held = {row["ts_code"] for row in worker.get_store().watch_list()}
    try:
        rows = quotes.search(query)
    except Exception as exc:
        return jsonify({"status": "error", "error": str(exc)}), 500
    return jsonify({"status": "ok", "items": [{**row, "in_watchlist": row["ts_code"] in held} for row in rows]})


# ---------------------------------------------------------------- 提醒
def _alert_body(row: dict) -> dict:
    return {
        "id": row["id"], "ts_code": row["ts_code"], "side": row["side"], "price": row["price"],
        "confirm": row["confirm"], "base_name": row["base_name"],
    }


@bp.route("/stocks/<ts_code>/alerts", methods=["GET"])
def alerts_list(ts_code: str):
    stock = resolve_stock(ts_code)
    if stock is None:
        return jsonify({"error": f"未找到股票: {ts_code}"}), 404
    return jsonify({"status": "ok", "items": [_alert_body(row) for row in worker.get_store().alerts(stock.ts_code)]})


@bp.route("/stocks/<ts_code>/alerts", methods=["POST"])
def alerts_add(ts_code: str):
    """两种设法：{"side": "down"} 按当前判断里的那个价位设；{"side": "down", "price": 56.5} 自己指定价。"""
    stock = resolve_stock(ts_code)
    if stock is None:
        return jsonify({"error": f"未找到股票: {ts_code}"}), 404
    body = request.get_json(silent=True) or {}
    side = body.get("side")
    if side not in ("down", "up"):
        return jsonify({"error": "side 必须是 down 或 up"}), 400
    store = worker.get_store()
    confirm, base_name = str(body.get("confirm") or "close"), ""
    if body.get("price") not in (None, ""):
        try:
            price = float(body["price"])
        except (TypeError, ValueError):
            return jsonify({"error": "price 必须是数字"}), 400
        if price <= 0:
            return jsonify({"error": "price 必须大于 0"}), 400
    else:
        record = store.latest(stock.ts_code)
        level = ((_page(record, current_mode()) if record else {}).get("levels") or {}).get(side)
        if not level:
            return jsonify({"error": "当前判断里没有这个方向的价位，请直接传 price"}), 409
        price, base_name = level["price"], level.get("condition") or ""
    row = store.alert_add(stock.ts_code, side, round(price, 2), confirm, base_name)
    return jsonify({"status": "ok", "alert": _alert_body(row)}), 201


@bp.route("/alerts/<int:alert_id>", methods=["DELETE"])
def alerts_remove(alert_id: int):
    if not worker.get_store().alert_remove(alert_id):
        return jsonify({"error": f"没有这条提醒: {alert_id}"}), 404
    return jsonify({"status": "ok", "id": alert_id})
