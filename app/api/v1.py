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


def _review_of(stock, payload: dict, kind: str = "flow") -> dict:
    """并入 AI 体检：只读已经生成好的，不等模型；失败也不影响详情本身。"""
    try:
        from app.reviews.attach import attach
        from app.synthesis.worker import get_store

        return attach(dict(payload), stock, get_store(), kind=kind)
    except Exception as exc:
        return {"review": None, "review_state": "unavailable", "review_error": f"读取体检失败: {exc}"}


def _window():
    raw = request.args.get("window", "")
    if raw in (None, ""):
        return 20, None
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
    from app.services.volume_flow import build_detail

    try:
        payload = build_detail(stock, window)
        _notify(stock, "volume_flow")
        extra = _review_of(stock, payload)   # 不改 payload 本身：它是详情的缓存对象
        from app.services.volume_flow.detail import public
        from app.services.volume_flow.headline import headline

        return jsonify({**public(payload), **extra, "headline": headline(extra.get("review"))})
    except Exception as exc:
        log.exception("%s detail failed: %s", "volume_flow", stock.ts_code)
        return jsonify({
            "status": "error",
            "ts_code": stock.ts_code,
            "stock_name": stock.name,
            "error": str(exc),
        }), 500


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
    from app.services.news_detail import build_detail, public

    try:
        payload = build_detail(stock, days)
        _notify(stock, "news")
        return jsonify(public(payload))
    except Exception as exc:
        log.exception("%s detail failed: %s", "news", stock.ts_code)
        return jsonify({
            "ts_code": stock.ts_code,
            "stock_name": stock.name,
            "error": str(exc),
        }), 500


@bp.route("/stocks/<ts_code>/dimensions/fundamental/detail", methods=["GET"])
def fundamental_detail(ts_code: str):
    stock = resolve_stock(ts_code)
    if stock is None:
        return jsonify({"error": f"未找到股票: {ts_code}"}), 404
    from app.services.fundamental_detail import build_detail, failed_payload
    from app.services.fundamental_detail.detail import public
    from app.services.verdict_view import summary_headline

    try:
        payload = build_detail(stock)
        _notify(stock, "fundamental")
        meta = payload.get("meta") or {}
        if meta.get("state") == "ok":
            extra = _review_of(stock, payload, kind="fund")
        elif meta.get("state") == "computing":
            extra = {"review": None, "review_state": "computing", "review_error": ""}
        else:   # 数据不足（上市不足一年、没有可比同行、港股）：不做体检
            extra = {"review": None, "review_state": "unavailable", "review_error": meta.get("status_text") or ""}
        return jsonify({**public(payload), **extra, "headline": summary_headline(extra.get("review"), "q11")})
    except Exception as exc:
        log.exception("%s detail failed: %s", "fundamental", stock.ts_code)
        return jsonify(failed_payload(stock, str(exc))), 500


@bp.route("/stocks/<ts_code>/dimensions/finance/detail", methods=["GET"])
def finance_detail(ts_code: str):
    stock = resolve_stock(ts_code)
    if stock is None:
        return jsonify({"error": f"未找到股票: {ts_code}"}), 404
    from app.finance_review.review import headline
    from app.services.finance_detail import build_detail, failed_payload
    from app.services.finance_detail.detail import public

    try:
        payload = build_detail(stock)
        _notify(stock, "finance")
        meta = payload.get("meta") or {}
        if meta.get("state") == "ok":
            extra = _review_of(stock, payload, kind="fin")
        elif meta.get("state") == "computing":
            extra = {"review": None, "review_state": "computing", "review_error": ""}
        else:   # 港股、金融业、没有已披露的财报：不做解读
            extra = {"review": None, "review_state": "unavailable", "review_error": meta.get("status_text") or ""}
        days = ((meta.get("report") or {}).get("days_since"))
        return jsonify({**public(payload), **extra, "headline": headline(extra.get("review"), days)})
    except Exception as exc:
        log.exception("%s detail failed: %s", "finance", stock.ts_code)
        return jsonify(failed_payload(stock, str(exc))), 500


@bp.route("/stocks/<ts_code>/dimensions/sector/detail", methods=["GET"])
def sector_detail(ts_code: str):
    stock = resolve_stock(ts_code)
    if stock is None:
        return jsonify({"error": f"未找到股票: {ts_code}"}), 404
    from app.services.sector_detail import build_detail, failed_payload

    try:
        payload = build_detail(stock, sector_code=request.args.get("sector_code"))
        _notify(stock, "sector")
        return jsonify(payload)
    except Exception as exc:
        log.exception("%s detail failed: %s", "sector", stock.ts_code)
        return jsonify(failed_payload(stock, str(exc))), 500
