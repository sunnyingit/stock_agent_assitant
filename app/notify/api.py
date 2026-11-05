"""通知接口：/notify/...  收件箱、渠道、规则、设置、桌面通知。密钥只收引用，不回显。"""

from __future__ import annotations

import logging

from flask import Blueprint, jsonify, request

from app.notify import channels as chan
from app.notify import pipeline, types
from app.notify.types import SEVERITY

bp = Blueprint("notify", __name__, url_prefix="/notify")
log = logging.getLogger("notify")
RULE_TYPES = ("price_alert", "price_move", "limit_hit", "price_extreme", "announcement", "news_item", "judgment_change", "reason_status",
              "wish_condition", "flow_streak", "calendar_upcoming", "market_change", "outcome_settled", "task_overdue")


def _n() -> pipeline.Notifier:
    return pipeline.current()


def _err(status: int, code: str, message: str, **details):
    return jsonify({"error": {"code": code, "message": message, "details": details}}), status


def _paging() -> tuple[int, int]:
    try:
        return max(1, min(int(request.args.get("limit", 50)), 200)), max(0, int(request.args.get("cursor") or 0))
    except ValueError:
        return 50, 0


def _notice(item: dict) -> dict:
    return {key: item[key] for key in ("notif_id", "event_id", "type", "title", "body", "tone", "link", "severity", "subject_id", "group_key", "created_at", "read_at")}


def _channel_view(channel: dict) -> dict:
    out = {key: channel[key] for key in ("channel_id", "type", "name", "config", "min_severity", "quiet_hours", "rate_limit", "relay", "enabled", "verified_at")}
    out["has_secret"] = bool(chan.resolve_secret(channel.get("secret_ref"))) if channel.get("secret_ref") else False
    out["secret_ref"] = channel.get("secret_ref")
    return out


# ------------------------------------------------------------ 收件箱
@bp.route("/inbox", methods=["GET"])
def inbox():
    limit, offset = _paging()
    rows = _n().store.inbox(request.args.get("unread") in ("1", "true"), limit + 1, offset, request.args.get("group"))
    return jsonify({"items": [_notice(row) for row in rows[:limit]], "next_cursor": str(offset + limit) if len(rows) > limit else None,
                    "unread": _n().store.unread_count()})


@bp.route("/unread", methods=["GET"])
def unread():
    return jsonify({"unread": _n().store.unread_count()})


@bp.route("/inbox/read", methods=["POST"])
def mark_read():
    body = request.get_json(silent=True) or {}
    ids = None if body.get("all") else list(body.get("ids") or [])
    count = _n().store.mark_read(ids, _n().clock.now_ms())
    return jsonify({"updated": count, "unread": _n().store.unread_count()})


@bp.route("/inbox/archive", methods=["POST"])
def archive():
    body = request.get_json(silent=True) or {}
    return jsonify({"updated": _n().store.archive(list(body.get("ids") or []), _n().clock.now_ms())})


@bp.route("/inbox/<notif_id>", methods=["GET"])
def notice(notif_id: str):
    store = _n().store
    item = store.notification(notif_id)
    if item is None:
        return _err(404, "not_found", "没有这条通知")
    out = _notice(item)
    out["event"] = store.event(item["event_id"])
    out["deliveries"] = [{k: d[k] for k in ("channel_id", "state", "attempts", "last_error", "suppressed_reason", "sent_at")} for d in store.deliveries(notif_id=notif_id)]
    return jsonify(out)


@bp.route("/events", methods=["GET"])
def events():
    limit, offset = _paging()
    rows = _n().store.events(request.args.get("type"), request.args.get("subject_id"), None, limit + 1, offset)
    return jsonify({"items": rows[:limit], "next_cursor": str(offset + limit) if len(rows) > limit else None})


@bp.route("/types", methods=["GET"])
def event_types():
    return jsonify({"items": [{"type": t.type, "severity": t.severity, "ttl_s": t.ttl_s, "group": t.group} for t in types.REGISTRY.values()]})


# ------------------------------------------------------------ 渠道
@bp.route("/channels", methods=["GET"])
def channels():
    return jsonify({"items": [_channel_view(c) for c in _n().store.channels()], "adapters": sorted(chan.ADAPTERS)})


@bp.route("/channels/<channel_id>", methods=["PUT"])
def put_channel(channel_id: str):
    body = request.get_json(silent=True) or {}
    store = _n().store
    existing = store.channel(channel_id)
    ctype = body.get("type") or (existing or {}).get("type")
    if ctype not in chan.ADAPTERS:
        return _err(400, "invalid_type", "渠道类型必须是 " + "、".join(sorted(chan.ADAPTERS)))
    if body.get("min_severity") and body["min_severity"] not in SEVERITY:
        return _err(400, "invalid_severity", "min_severity 必须是 info、normal、urgent")
    secret_ref = body.get("secret_ref", (existing or {}).get("secret_ref"))
    if body.get("secret"):
        try:
            import keyring
            keyring.set_password("stock-agent", channel_id, str(body["secret"]))
            secret_ref = "keychain:" + channel_id
        except Exception:
            return _err(501, "no_keychain", "这台电脑没有可用的系统钥匙串，请改用 secret_ref（例如 env:WECOM_WEBHOOK_URL）")
    if secret_ref and secret_ref.split(":", 1)[0] not in ("env", "keychain"):
        return _err(400, "invalid_secret_ref", "secret_ref 只能是 env:NAME 或 keychain:NAME")
    fields = {key: body[key] for key in ("name", "config", "min_severity", "quiet_hours", "rate_limit", "enabled", "relay") if key in body}
    channel = store.put_channel({"channel_id": channel_id, "type": ctype, "name": body.get("name") or (existing or {}).get("name") or channel_id,
                                 **fields, "secret_ref": secret_ref, **({"verified_at": None} if body.get("secret_ref") or body.get("secret") else {})})
    return jsonify(_channel_view(channel))


@bp.route("/channels/<channel_id>", methods=["DELETE"])
def delete_channel(channel_id: str):
    if not _n().store.delete_channel(channel_id):
        return _err(404, "not_found", "没有这个渠道")
    return jsonify({"deleted": channel_id})


@bp.route("/channels/<channel_id>/test", methods=["POST"])
def test_channel(channel_id: str):
    try:
        return jsonify(_n().send_test(channel_id))
    except KeyError:
        return _err(404, "not_found", "没有这个渠道")
    except ValueError as exc:
        return _err(400, "invalid_type", str(exc))


# ------------------------------------------------------------ 设置、规则
@bp.route("/settings", methods=["GET"])
def settings():
    return jsonify(_n().store.settings())


@bp.route("/settings", methods=["PUT"])
def put_settings():
    body = request.get_json(silent=True) or {}
    quiet = body.get("quiet_hours")
    if quiet is not None:
        try:
            from app.notify.policy import _minutes
            _minutes(quiet["from"]), _minutes(quiet["to"])
        except Exception:
            return _err(400, "invalid_quiet_hours", "quiet_hours 要写成 {\"from\":\"22:30\",\"to\":\"08:00\"}")
    return jsonify(_n().store.set_settings(body))


@bp.route("/rules", methods=["GET"])
def rules():
    store = _n().store
    items = []
    for rule in store.rules(request.args.get("type"), request.args.get("subject_id"), request.args.get("origin")):
        state = store.rule_state(rule["rule_id"])
        items.append({**rule, "state": {k: state[k] for k in ("state", "fire_seq", "fired_at", "last_eval_at", "last_value")}})
    return jsonify({"items": items})


def _rule_body(body: dict) -> tuple[dict | None, tuple | None]:
    if body.get("type") not in RULE_TYPES:
        return None, _err(400, "invalid_type", "规则类型必须是 " + "、".join(RULE_TYPES))
    subject = body.get("subject") or {}
    if not subject.get("id"):
        return None, _err(400, "invalid_subject", "需要 subject.id")
    params = body.get("params") or {}
    if body["type"] == "price_alert":
        if params.get("side") not in ("up", "down") or not isinstance(params.get("price"), (int, float)) or params["price"] <= 0:
            return None, _err(400, "invalid_params", "price_alert 需要 side（up/down）和大于 0 的 price")
        if params.get("confirm", "touch") not in ("touch", "close"):
            return None, _err(400, "invalid_params", "confirm 必须是 touch 或 close")
    if body["type"] == "price_move" and (not isinstance(params.get("threshold"), (int, float)) or params["threshold"] <= 0):
        return None, _err(400, "invalid_params", "price_move 需要大于 0 的 threshold（百分比）")
    rule = {"type": body["type"], "subject_kind": subject.get("kind") or "stock", "subject_id": subject["id"], "params": params}
    for key in ("mode", "rearm", "cooldown_s", "routing", "enabled", "origin", "origin_ref"):
        if key in body:
            rule[key] = body[key]
    if body["type"] == "price_move":
        rule.setdefault("rearm", {"kind": "next_day"})
    return rule, None


@bp.route("/rules", methods=["POST"])
def create_rule():
    rule, bad = _rule_body(request.get_json(silent=True) or {})
    if bad:
        return bad
    return jsonify(_n().store.put_rule(rule, _n().clock.now_ms())), 201


@bp.route("/rules/<rule_id>", methods=["PUT"])
def update_rule(rule_id: str):
    store = _n().store
    existing = store.rule(rule_id)
    if existing is None:
        return _err(404, "not_found", "没有这条规则")
    body = request.get_json(silent=True) or {}
    merged = {"type": existing["type"], "subject": {"kind": existing["subject_kind"], "id": existing["subject_id"]}, "params": existing["params"], **body}
    rule, bad = _rule_body(merged)
    if bad:
        return bad
    return jsonify(store.put_rule({**rule, "rule_id": rule_id}, _n().clock.now_ms()))


@bp.route("/rules/<rule_id>", methods=["DELETE"])
def delete_rule(rule_id: str):
    if not _n().store.delete_rule(rule_id):
        return _err(404, "not_found", "没有这条规则")
    return jsonify({"deleted": rule_id})


@bp.route("/rules/<rule_id>/rearm", methods=["POST"])
def rearm(rule_id: str):
    store = _n().store
    if store.rule(rule_id) is None:
        return _err(404, "not_found", "没有这条规则")
    store.put_rule_state(rule_id, state="armed", rearm_at=_n().clock.now_ms())
    return jsonify(store.rule_state(rule_id))


# ------------------------------------------------------------ 桌面通知（Electron 来取）
@bp.route("/desktop/pending", methods=["GET"])
def desktop_pending():
    return jsonify({"items": _n().desktop_pending(), "unread": _n().store.unread_count()})


@bp.route("/desktop/ack", methods=["POST"])
def desktop_ack():
    body = request.get_json(silent=True) or {}
    return jsonify({"acked": _n().desktop_ack(list(body.get("delivery_ids") or []))})


# ------------------------------------------------------------ 离线转发（客户端这一侧）
@bp.route("/relay", methods=["GET"])
def relay_status():
    from app.notify import relay_client

    client = relay_client.RelayClient(_n())
    conf = client.config()
    return jsonify({"enabled": client.enabled(), "url": conf["url"], "device_id": conf["device_id"], "authoritative": client.authoritative(),
                    "healthy_at": conf["healthy_at"], "rev": conf["rev"],
                    "channels": [c["channel_id"] for c in _n().store.channels() if c["relay"]]})


@bp.route("/relay", methods=["POST"])
def relay_enable():
    """明确开启：向服务端注册一个匿名设备。令牌存系统钥匙串（没有就存本机 0600 文件）。"""
    from app.notify import relay_client

    url = str((request.get_json(silent=True) or {}).get("url", "")).strip()
    if not url.startswith(("http://", "https://")):
        return _err(400, "invalid_url", "需要服务端地址，例如 https://example.com")
    try:
        return jsonify(relay_client.RelayClient(_n()).register(url)), 201
    except Exception as exc:
        return _err(502, "relay_unreachable", str(exc))


@bp.route("/relay", methods=["DELETE"])
def relay_disable():
    from app.notify import relay_client

    return jsonify({"revoked": relay_client.RelayClient(_n()).revoke()})
