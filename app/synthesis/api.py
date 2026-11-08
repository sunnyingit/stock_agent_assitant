"""综合判断接口。页面只读存档；没有存档时先出规则版，再排队补 Agent 版。"""

from __future__ import annotations

import time

from flask import Blueprint, jsonify, request

from app.synthesis import policy, quotes, watchlist, worker
from app.synthesis.calendar import last_close_date, now_cn, session_of
from app.synthesis.gateway import Gateway
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
            # 还没有任何存档：先出一份不调模型的规则版让页面有东西看，真正的结论交给本机定时任务那条线排队生成
            reply = compute(stock, store, gateway=Gateway(None), mode="rule")
            body = {"display": reply["display"], "internal": reply["internal"]}
        else:
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
        **_brief_of(body, stock, mode, store.alerts(stock.ts_code)),
    }
    if request.args.get("debug") == "1":
        response["internal"] = body["internal"]
    return jsonify(response)


def _brief_of(body: dict, stock, mode: str, alerts: list[dict]) -> dict:
    """第一眼结构。存档里已有就直接用；旧存档用当前详情现装。"""
    internal = body.get("internal") or {}
    record_data = None
    if internal.get("run_id"):
        stored = worker.get_store().latest(stock.ts_code)
        record_data = (stored or {}).get("data") if stored else None
    brief = (record_data or {}).get("brief")
    if not brief:
        page = body["display"]
        try:
            from app.synthesis.brief import compose_live

            source = "model" if internal.get("source") == "agent" and mode == "agent" else "rule"
            brief = compose_live(stock, page, source=source)
        except Exception:
            return {}
    return _with_watch_alerts(brief, alerts)


def _with_watch_alerts(brief: dict, alerts: list[dict]) -> dict:
    watch = dict(brief.get("watch") or {})
    levels = []
    for level in watch.get("levels") or []:
        level = dict(level)
        hit = next((item for item in alerts if item["side"] == level.get("side") and abs(item["price"] - level["price"]) < 0.005), None)
        level["alert"] = {"subscribed": hit is not None, "alert_id": hit["id"] if hit else None}
        levels.append(level)
    watch["levels"] = levels
    brief = dict(brief)
    brief["watch"] = watch
    return brief


@bp.route("/stocks/<ts_code>/synthesis/refresh", methods=["POST"])
def refresh(ts_code: str):
    stock = resolve_stock(ts_code)
    if stock is None:
        return jsonify({"error": f"未找到股票: {ts_code}"}), 404
    # 数据服务不重算结论：结论由客户端的定时任务生成，「重新分析」请在客户端点。
    return jsonify({"status": "ignored", "ts_code": stock.ts_code, "note": "结论由客户端生成"}), 202


def _saved_detail_record(stock):
    """结论详情只读已经存下的那一版：优先本机定时任务出的结论包，没有才退回最近一次运行。"""
    record = worker.get_store().latest(stock.ts_code)
    data = (record or {}).get("data") or {}
    if data.get("display_agent") or data.get("display_rule"):
        return record
    return None


class ConclusionPending(LookupError):
    """这里没有存下这只股票的结论。结论由客户端的定时任务生成，数据服务从不就地生成。"""


def _agent_record(stock):
    """只读已经存下的结论；没有就如实说没有。"""
    saved = _saved_detail_record(stock)
    if saved is not None:
        return saved
    raise ConclusionPending("这里还没有这只股票的结论")


@bp.route("/stocks/<ts_code>/chief/stream", methods=["GET"])
def chief_stream(ts_code: str):
    """主持卡。整段核对完再推，不逐字输出。"""
    stock = resolve_stock(ts_code)
    if stock is None:
        return jsonify({"error": f"未找到股票: {ts_code}"}), 404
    from flask import Response

    from app.synthesis.chief_board import card

    def generate():
        import json

        def send(event, data):
            return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

        yield send("start", {"role": "chief", "name": "主分析师"})
        try:
            record = _agent_record(stock)
            payload = card((record or {}).get("data") or {})
        except Exception as exc:
            yield send("error", {"role": "chief", "error": str(exc)})
            return
        yield send("message", payload)
        yield send("done", {"role": "chief"})

    return Response(generate(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _side_context(stock, side: str, *, allow_empty: bool = False):
    """看多、看空读结论包里存下的那一轮，不再就地调模型。追问用的「上一轮」也是这一份。"""
    from app.synthesis.chief_board import side_message

    record = _agent_record(stock)
    data = (record or {}).get("data") or {}
    decision = data.get("decision") or {}
    packed = (decision.get("sides") or {}).get(side)
    if not isinstance(packed, dict) or not data.get("table"):
        if not allow_empty:
            raise RuntimeError("这份结论里没有这一方的发言")
        packed = {"points": [], "errors": ["这份结论里没有这一方的发言"], "failed": True}
    elif packed.get("failed"):
        if not allow_empty:
            raise RuntimeError("；".join(packed.get("errors") or ["模型没有返回"]))
        packed = {"points": [], "errors": packed.get("errors") or [], "failed": True}
    table = FactTable.from_dict(data["table"]) if data.get("table") else FactTable()
    final = decision.get("final") if decision.get("source") == "agent" and isinstance(decision.get("final"), dict) else None
    return side_message(side, packed, table, final), table


def _side_events(stock, side: str):
    return _side_context(stock, side)[0]


def _side_stream(ts_code: str, side: str, name: str):
    stock = resolve_stock(ts_code)
    if stock is None:
        return jsonify({"error": f"未找到股票: {ts_code}"}), 404
    from flask import Response

    def generate():
        import json

        def send(event, data):
            return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

        yield send("start", {"role": side, "name": name})
        try:
            payload = _side_events(stock, side)
        except Exception as exc:
            yield send("error", {"role": side, "error": str(exc)})
            return
        yield send("message", payload)
        yield send("done", {"role": side})

    return Response(generate(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@bp.route("/stocks/<ts_code>/bull/stream", methods=["GET"])
def bull_stream(ts_code: str):
    """看多分析师的发言。只等这一边，不跑看空和主分析师。"""
    return _side_stream(ts_code, "bull", "看多分析师")


@bp.route("/stocks/<ts_code>/bear/stream", methods=["GET"])
def bear_stream(ts_code: str):
    """看空分析师的发言。只等这一边，不跑看多和主分析师。"""
    return _side_stream(ts_code, "bear", "看空分析师")


def _sse_reply(role: str, name: str, load):
    from flask import Response

    def generate():
        import json

        def send(event, data):
            return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

        yield send("start", {"role": role, "name": name, "kind": "reply"})
        try:
            payload = load()
        except Exception as exc:
            yield send("error", {"role": role, "error": str(exc)})
            return
        yield send("message", payload)
        yield send("done", {"role": role})

    return Response(generate(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _side_reply(stock, side: str, question: str) -> dict:
    from app.synthesis.debate_reply import answer_side, table_facts

    previous, table = _side_context(stock, side, allow_empty=True)
    return answer_side(side, question, previous, facts=table_facts(table))


def _chief_reply(stock, question: str) -> dict:
    from app.synthesis.chief_board import card
    from app.synthesis.debate_reply import answer_chief, table_facts
    from app.synthesis.service import load_table
    from app.synthesis.types import FactTable

    record = _agent_record(stock)
    data = (record or {}).get("data") or {}
    previous = card(data or {})
    sides = ((data.get("decision") or {}).get("sides") or {})
    table = FactTable.from_dict(data["table"]) if data.get("table") else None
    if table is None:
        store = worker.get_store()
        *_, table, _now = load_table(stock, store, timeout=policy.COLLECT_TIMEOUT_BACKGROUND)
    return answer_chief(question, previous, sides, facts=table_facts(table))


@bp.route("/stocks/<ts_code>/bull/reply", methods=["POST"])
def bull_reply(ts_code: str):
    """追问看多分析师。消息外壳和第一次分析相同，kind 为 reply。"""
    stock = resolve_stock(ts_code)
    if stock is None:
        return jsonify({"error": f"未找到股票: {ts_code}"}), 404
    body = request.get_json(silent=True) or {}
    question = str(body.get("question") or request.args.get("q") or "").strip()
    if not question:
        return jsonify({"error": "缺少 question"}), 400
    return _sse_reply("bull", "看多分析师", lambda: _side_reply(stock, "bull", question))


@bp.route("/stocks/<ts_code>/bear/reply", methods=["POST"])
def bear_reply(ts_code: str):
    """追问看空分析师。消息外壳和第一次分析相同，kind 为 reply。"""
    stock = resolve_stock(ts_code)
    if stock is None:
        return jsonify({"error": f"未找到股票: {ts_code}"}), 404
    body = request.get_json(silent=True) or {}
    question = str(body.get("question") or request.args.get("q") or "").strip()
    if not question:
        return jsonify({"error": "缺少 question"}), 400
    return _sse_reply("bear", "看空分析师", lambda: _side_reply(stock, "bear", question))


@bp.route("/stocks/<ts_code>/chief/reply", methods=["POST"])
def chief_reply(ts_code: str):
    """追问主分析师。消息外壳和主持卡相同，kind 为 reply。"""
    stock = resolve_stock(ts_code)
    if stock is None:
        return jsonify({"error": f"未找到股票: {ts_code}"}), 404
    body = request.get_json(silent=True) or {}
    question = str(body.get("question") or request.args.get("q") or "").strip()
    if not question:
        return jsonify({"error": "缺少 question"}), 400
    return _sse_reply("chief", "主分析师", lambda: _chief_reply(stock, question))


@bp.route("/stocks/<ts_code>/chief/detail", methods=["GET"])
def chief_detail(ts_code: str):
    """结论详情：五维注意项、两个价，以及走势图的均线、价位线和每日判断。"""
    stock = resolve_stock(ts_code)
    if stock is None:
        return jsonify({"error": f"未找到股票: {ts_code}"}), 404
    from app.synthesis.chief_board import detail

    try:
        record = _saved_detail_record(stock)
        if record is None:
            record = _agent_record(stock)
    except ConclusionPending as exc:
        return jsonify({"status": "pending", "ts_code": stock.ts_code, "stock_name": stock.name, "error": str(exc)}), 202
    except Exception as exc:
        return jsonify({"status": "error", "ts_code": stock.ts_code, "stock_name": stock.name, "error": str(exc)}), 500
    body = detail((record or {}).get("data") or {})
    return jsonify({"status": "ok", "ts_code": stock.ts_code, "stock_name": stock.name, **body})


# ---------------------------------------------------------------- 自选（侧栏"诊断"）
def _items(store, only=None):
    # 打开列表只展示现有判断。过期或还没有判断的股票不在这里排队，免得一把发起全部诊断。
    return watchlist.build(store, quotes.fetch, current_mode(), now_cn(), only=only)


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
