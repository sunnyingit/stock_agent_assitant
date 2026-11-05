"""服务端接口 /v1/...：设备、订阅、离线转发、产物、就绪标记。设计第 4、8 节。"""

from __future__ import annotations

import json
import time

from flask import Blueprint, Response, current_app, g, jsonify, request

from app.server import security
from app.server.security import device_required

bp = Blueprint("server", __name__, url_prefix="/v1")
MAX_ALERTS = 200
MAX_SUBSCRIPTIONS = 500


def _store():
    return current_app.config["SERVER_STORE"]


def _now() -> int:
    return current_app.config["SERVER_CLOCK"].now_ms()


def _err(status: int, code: str, message: str, **details):
    return jsonify({"error": {"code": code, "message": message, "details": details}}), status


@bp.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "now": _now()})


# ------------------------------------------------------------ 设备
@bp.route("/devices", methods=["POST"])
def register():
    """匿名注册：返回设备令牌（只此一次明文）。随机 device_id，不关联账号。"""
    secret, token_hash = security.new_token()
    device_id = _store().add_device(token_hash, _now())
    return jsonify({"device_id": device_id, "token": f"{device_id}.{secret}"}), 201


@bp.route("/devices/me", methods=["DELETE"])
@device_required
def revoke():
    _store().delete_device(g.device_id)
    return jsonify({"deleted": g.device_id})


# ------------------------------------------------------------ 订阅
@bp.route("/subscriptions", methods=["PUT"])
@device_required
def subscriptions():
    body = request.get_json(silent=True) or {}
    codes = body.get("codes")
    if not isinstance(codes, list) or len(codes) > MAX_SUBSCRIPTIONS:
        return _err(400, "invalid_codes", f"codes 必须是不超过 {MAX_SUBSCRIPTIONS} 个的列表")
    clean = []
    for item in codes:
        code = str((item or {}).get("ts_code", "")).upper()
        if not (len(code) >= 8 and "." in code):
            return _err(400, "invalid_code", f"股票代码不对：{code}")
        clean.append({"ts_code": code, "tier": item.get("tier")})
    store = _store()
    rev = int(body.get("rev", 0))
    current = store.rev(g.device_id, "subscriptions")
    if rev <= current:
        return _err(409, "stale_rev", "版本太旧", rev=current)
    count = store.put_subscriptions(g.device_id, clean, _now())
    with store._conn() as conn:
        store.set_rev(conn, g.device_id, "subscriptions", rev)
    return jsonify({"rev": rev, "accepted": count})


# ------------------------------------------------------------ 离线转发
@bp.route("/relay/alerts", methods=["PUT"])
@device_required
def relay_put():
    body = request.get_json(silent=True) or {}
    alerts, channels = body.get("alerts"), body.get("channels") or []
    if not isinstance(alerts, list) or len(alerts) > MAX_ALERTS or not isinstance(body.get("rev"), int):
        return _err(400, "invalid_body", f"需要 rev（整数）和不超过 {MAX_ALERTS} 条的 alerts")
    clean = []
    for item in alerts:
        if not isinstance(item, dict) or item.get("side") not in ("up", "down") or item.get("confirm") not in ("touch", "close"):
            return _err(400, "invalid_alert", "提醒需要 side（up/down）和 confirm（touch/close）")
        try:
            price = float(item["price"])
            assert price > 0
        except Exception:
            return _err(400, "invalid_alert", "提醒的 price 要大于 0")
        if not item.get("alert_id") or "." not in str(item.get("ts_code", "")):
            return _err(400, "invalid_alert", "提醒需要 alert_id 和 ts_code")
        clean.append({**item, "price": price, "ts_code": item["ts_code"].upper(), "label": str(item.get("label", ""))[:40]})
    for channel in channels:
        if not channel.get("channel_id") or channel.get("type") not in ("wecom_webhook", "bark", "serverchan"):
            return _err(400, "invalid_channel", "渠道类型只支持 wecom_webhook、bark、serverchan")
    ok, rev = _store().put_relay(g.device_id, body["rev"], clean, channels, security.encrypt)
    if not ok:
        return _err(409, "stale_rev", "版本太旧，请拉取后合并再推", rev=rev)
    return jsonify({"rev": rev, "accepted": len(clean)})


@bp.route("/relay/events", methods=["GET"])
@device_required
def relay_events():
    after = request.args.get("after", type=int, default=0)
    return jsonify({"items": _store().relay_events(g.device_id, after)})


@bp.route("/relay/ack", methods=["POST"])
@device_required
def relay_ack():
    seqs = [int(s) for s in (request.get_json(silent=True) or {}).get("seqs", []) if str(s).isdigit()]
    return jsonify({"acked": _store().ack_events(g.device_id, seqs, _now())})


@bp.route("/relay/stream", methods=["GET"])
@device_required
def relay_stream():
    """SSE：连着就视为在线。once=1 时只吐当前积压就结束（测试和轮询用）。"""
    store, device = _store(), g.device_id
    clock = current_app.config["SERVER_CLOCK"]
    after = request.args.get("after", type=int, default=0)
    once = request.args.get("once") == "1"
    limit_s = request.args.get("max_s", type=float, default=3600)

    def stream():
        seq, started = after, time.time()
        while True:
            store.touch_device(device, clock.now_ms(), stream=True)
            items = store.relay_events(device, seq)
            for item in items:
                seq = item["seq"]
                yield f"id: {seq}\nevent: relay\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"
            if once or time.time() - started > limit_s:
                return
            if not items:
                yield ": ping\n\n"
            time.sleep(5)

    return Response(stream(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ------------------------------------------------------------ 产物、清单、就绪
def _artifact_response(envelope: dict):
    etag = '"' + envelope["facts_hash"] + '"'
    if request.headers.get("If-None-Match") == etag:
        return Response(status=304, headers={"ETag": etag})
    resp = jsonify(envelope)
    resp.headers["ETag"] = etag
    return resp


def _lookup(kind: str, subject: str, as_of=None, session=None, schema=None):
    store = _store()
    hit = store.artifact(kind, subject, as_of, session, schema)
    if hit is None:
        producer = current_app.config.get("ARTIFACT_PRODUCERS", {}).get(kind)
        if producer is not None:                      # 兜底：没预热的就现算并缓存，第一次慢，之后命中
            try:
                hit = producer(store, subject, as_of, session)
            except Exception as exc:
                current_app.logger.warning("按需生成 %s/%s 失败：%s", kind, subject, exc)
    return hit


@bp.route("/artifacts/<kind>/<path:subject>", methods=["GET"])
@device_required
def artifact(kind: str, subject: str):
    _store().bump_demand(subject, _now())
    hit = _lookup(kind, subject, request.args.get("as_of"), request.args.get("session"), request.args.get("schema"))
    if hit is None:
        return _err(404, "not_found", "这份产物还没有")
    return _artifact_response(hit)


@bp.route("/artifacts:batchGet", methods=["POST"])
@device_required
def batch_get():
    items = (request.get_json(silent=True) or {}).get("items") or []
    if len(items) > 100:
        return _err(400, "too_many", "一次最多取 100 份")
    out = []
    for item in items:
        hit = _lookup(item.get("kind", ""), item.get("subject", ""), item.get("as_of"), item.get("session"), item.get("schema"))
        out.append({"request": item, "found": hit is not None, "artifact": hit})
    return jsonify({"items": out})


@bp.route("/manifest", methods=["GET"])
@device_required
def manifest():
    kind, as_of = request.args.get("kind"), request.args.get("as_of")
    if not kind or not as_of:
        return _err(400, "invalid_query", "需要 kind 和 as_of")
    return jsonify({"kind": kind, "as_of": as_of, "items": _store().manifest(kind, as_of)})


@bp.route("/ready", methods=["GET"])
def ready():
    date = request.args.get("date")
    if not date:
        return _err(400, "invalid_query", "需要 date")
    return jsonify({"v": 1, "date": date, "gates": _store().ready(date)})
