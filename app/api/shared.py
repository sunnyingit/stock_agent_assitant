"""各人看到的都一样的数据：板块判断、跟踪的板块、大盘结论、资金流连续天数、日线、日历。

持仓页的计算（体检、想买条件、复盘）在客户端本机做，因为它用到持仓和成交；但这些输入和持仓无关，
所有人一样，所以由数据服务提供。这里不读也不存任何个人数据，不调模型。
"""

from __future__ import annotations

import logging
from datetime import date

from flask import Blueprint, jsonify, request

from app.services.holdings_data import Data

bp = Blueprint("shared", __name__, url_prefix="/api/v1/shared")
log = logging.getLogger(__name__)


def _data() -> Data:
    return Data(date.today())


@bp.route("/sector/<ts_code>", methods=["GET"])
def sector(ts_code: str):
    return jsonify({"status": "ok", **_data().sector(ts_code)})


@bp.route("/tracked-sectors", methods=["GET"])
def tracked_sectors():
    return jsonify({"status": "ok", "items": _data().tracked_sectors()})


@bp.route("/market", methods=["GET"])
def market():
    return jsonify({"status": "ok", "market": _data().market()})


@bp.route("/market-since", methods=["GET"])
def market_since():
    return jsonify({"status": "ok", "since": _data().market_since()})


@bp.route("/flow/<ts_code>", methods=["GET"])
def flow(ts_code: str):
    return jsonify({"status": "ok", **_data().flow(ts_code)})


@bp.route("/quotes", methods=["GET"])
def quotes():
    """最新价和今天的涨跌幅，一次最多 60 只。客户端的行情都从这里取，取不到的股票不在结果里。"""
    codes = []
    for part in (request.args.get("codes") or "").split(","):
        code = part.strip().upper()
        if code and code not in codes:
            codes.append(code)
    if not codes:
        return jsonify({"status": "error", "error": "codes 不能为空"}), 400
    if len(codes) > 60:
        return jsonify({"status": "error", "error": "一次最多 60 只"}), 400
    try:
        items = _data().quotes({code: code for code in codes})
    except Exception as exc:  # noqa: BLE001 - 行情源出错不能把接口拖垮，客户端会自己再试
        log.warning("shared quotes failed: %s", exc)
        return jsonify({"status": "error", "error": "行情暂时取不到"}), 502
    return jsonify({"status": "ok", "items": items})


@bp.route("/baidu-board", methods=["GET"])
def baidu_board():
    """百度热搜榜，默认财经榜：?board=finance&limit=30。榜单只是热度，不是投资建议；取不到时 502。"""
    from app.services import baidu_board as board

    try:
        limit = int(request.args.get("limit") or 30)
    except ValueError:
        return jsonify({"status": "error", "error": "limit 要是数字"}), 400
    try:
        return jsonify({"status": "ok", **board.fetch_board(request.args.get("board") or "finance", limit)})
    except ValueError as exc:
        return jsonify({"status": "error", "error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001 - 百度取不到不能把接口拖垮
        log.warning("baidu board failed: %s", exc)
        return jsonify({"status": "error", "error": "百度热搜暂时取不到"}), 502


@bp.route("/closes/<ts_code>", methods=["GET"])
def closes(ts_code: str):
    return jsonify({"status": "ok", "items": [[day, close] for day, close in _data().closes(ts_code)]})


@bp.route("/index-closes", methods=["GET"])
def index_closes():
    return jsonify({"status": "ok", "items": [[day, close] for day, close in _data().index_closes()]})


@bp.route("/calendar", methods=["GET"])
def calendar():
    """?codes=600036.SH:招商银行,000001.SZ:平安银行 —— 自选里的定期报告披露日和解禁，未来两周。"""
    names = {}
    for part in request.args.get("codes", "").split(","):
        code, _, name = part.partition(":")
        if code.strip():
            names[code.strip()] = name.strip() or code.strip()
    return jsonify({"status": "ok", "items": _data().calendar(names) if names else []})


@bp.route("/announcements/<ts_code>", methods=["GET"])
def announcements(ts_code: str):
    """最新公告，带分类（重要度 P0–P2、类别、主题、方向提示）。分类是规则，不调模型。"""
    from app.services import announcements as ann
    from app.services.news import fetch_announcements
    from app.synthesis.watchlist import stock_of

    try:
        items = ann.enrich(ts_code, fetch_announcements(stock_of({"ts_code": ts_code, "name": ts_code}), 40))
    except Exception as exc:
        log.warning("announcements %s failed: %s", ts_code, exc)
        return jsonify({"status": "error", "error": str(exc), "items": []}), 502
    return jsonify({"status": "ok", "items": items})


@bp.route("/signals/<ts_code>", methods=["GET"])
def signals(ts_code: str):
    """涨跌停、炸板、20/60 日新高新低（用日线估）。"""
    from app.services import signals as sig
    from app.services.series import get_bars
    from app.tools.stock import resolve_stock

    stock = resolve_stock(ts_code)
    if stock is None:
        return jsonify({"status": "error", "error": f"未找到股票: {ts_code}"}), 404
    try:
        items = sorted(get_bars(stock, 120).get("items") or [], key=lambda row: str(row.get("trade_date")))
    except Exception as exc:
        return jsonify({"status": "error", "error": str(exc)}), 502
    out = sig.compute(stock.ts_code, stock.name, items)
    day = out["trade_date"]
    out["trade_date"] = f"{day[:4]}-{day[4:6]}-{day[6:8]}" if len(day) == 8 else day
    return jsonify({"status": "ok", "name": stock.name, **out})


def _levels_of(ts_code: str) -> dict:
    """上方、下方各一个价位：从五个维度算出的候选价位里，按固定的参考表选。选法是规则，不调模型。"""
    from app.synthesis import policy, service, worker
    from app.synthesis.chief_board import _price
    from app.synthesis.levels import resolve_levels
    from app.tools.stock import resolve_stock

    stock = resolve_stock(ts_code)
    if stock is None:
        raise LookupError(f"未找到股票: {ts_code}")
    bundle, snaps, _market, _hygiene, _fresh, _rule, price, evaluator, candidates, _table, _now = service.load_table(
        stock, worker.get_store(), timeout=policy.COLLECT_TIMEOUT_BACKGROUND,
    )
    levels = resolve_levels(None, candidates, price, evaluator, snaps)
    return {"price": price, "down": _price(levels.get("down")), "up": _price(levels.get("up"))}


@bp.route("/levels/<ts_code>", methods=["GET"])
def levels(ts_code: str):
    """上方价、下方价和各自的依据、条件、结果。所有人一样，客户端的结论详情和「如果我错了」的提醒价用它。"""
    try:
        return jsonify({"status": "ok", **_levels_of(ts_code)})
    except LookupError as exc:
        return jsonify({"status": "error", "error": str(exc)}), 404
    except Exception as exc:
        log.warning("levels %s failed: %s", ts_code, exc)
        return jsonify({"status": "error", "error": str(exc)}), 502


@bp.route("/sector-series/<ts_code>", methods=["GET"])
def sector_series(ts_code: str):
    """这只票所在申万行业的日收盘序列（升序），预测战绩比较「跑没跑赢板块」用。"""
    return jsonify({"status": "ok", **_data().sector_series(ts_code)})
