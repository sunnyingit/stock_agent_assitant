"""股票查询接口。"""

from flask import Blueprint, jsonify, request

from app.core.response import fail, ok, shown
from app.tools.stock import resolve_stock

bp = Blueprint("stocks", __name__, url_prefix="/api/stocks")


def _parse_request():
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        query = body.get("q") or body.get("code") or body.get("name") or ""
        price_raw = body.get("price", body.get("current_price"))
    else:
        query = request.args.get("q") or request.args.get("code") or request.args.get("name") or ""
        price_raw = request.args.get("price", request.args.get("current_price"))

    query = str(query).strip()
    if not query:
        return None, None, (jsonify({"error": "缺少参数，请传入 q=股票名称或代码"}), 400)

    price = None
    if price_raw not in (None, ""):
        try:
            price = float(price_raw)
        except (TypeError, ValueError):
            return None, None, (jsonify({"error": "price 必须是数字"}), 400)

    stock = resolve_stock(query)
    if stock is None:
        return None, None, (jsonify({"error": f"未找到股票: {query}"}), 404)
    return stock, price, None


def _days(default: int, maximum: int):
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        raw = body.get("days", "")
    else:
        raw = request.args.get("days", "")
    if raw in (None, ""):
        return default, None
    try:
        days = int(raw)
    except (TypeError, ValueError):
        return None, (jsonify({"error": "days 必须是正整数"}), 400)
    if days < 1:
        return None, (jsonify({"error": "days 必须是正整数"}), 400)
    return min(days, maximum), None


def _run(stock, fn):
    try:
        return ok(stock, fn())
    except Exception as exc:
        return fail(stock, exc)


def _show(stock, fn):
    try:
        return shown(stock, fn())
    except Exception as exc:
        return fail(stock, exc)


def _side(fn):
    try:
        return fn()
    except Exception as exc:
        return {"error": str(exc)}


@bp.route("/basic", methods=["GET", "POST"])
def basic():
    stock, _price, err = _parse_request()
    if err:
        return err
    from app.services.basic import get_basic

    return _run(stock, lambda: get_basic(stock))


@bp.route("/bars", methods=["GET", "POST"])
def bars():
    days, err = _days(250, 250)
    if err:
        return err
    stock, _price, err = _parse_request()
    if err:
        return err
    from app.services.series import get_bars

    return _run(stock, lambda: get_bars(stock, days))


@bp.route("/moneyflow", methods=["GET", "POST"])
def moneyflow():
    stock, _price, err = _parse_request()
    if err:
        return err
    from app.services.moneyflow import get_moneyflow

    return _run(stock, lambda: get_moneyflow(stock))


@bp.route("/moneyflow/daily", methods=["GET", "POST"])
def moneyflow_daily():
    days, err = _days(250, 250)
    if err:
        return err
    stock, _price, err = _parse_request()
    if err:
        return err
    from app.services.series import get_flow_daily

    return _run(stock, lambda: get_flow_daily(stock, days))


@bp.route("/news", methods=["GET", "POST"])
def news():
    stock, _price, err = _parse_request()
    if err:
        return err
    from app.services.news import get_news

    return _run(stock, lambda: get_news(stock))


@bp.route("/news/analysis", methods=["GET", "POST"])
def news_analysis():
    stock, _price, err = _parse_request()
    if err:
        return err
    from app.agents.news.graph import analyze_news

    return _run(stock, lambda: analyze_news(stock))


@bp.route("/news/debate", methods=["GET", "POST"])
def news_debate():
    stock, _price, err = _parse_request()
    if err:
        return err
    from app.agents.news.graph import debate_news

    return _show(stock, lambda: debate_news(stock))


@bp.route("/peers", methods=["GET", "POST"])
def peers():
    stock, price, err = _parse_request()
    if err:
        return err
    from app.services.peers.kr import get_kr_peers
    from app.services.peers.us import get_us_peers

    data = {
        "us": _side(lambda: get_us_peers(stock, price)),
        "kr": _side(lambda: get_kr_peers(stock, price)),
    }
    if data["us"].get("error") and data["kr"].get("error"):
        return fail(stock, RuntimeError(data["us"]["error"]))
    return ok(stock, data)


@bp.route("/fundamental", methods=["GET", "POST"])
def fundamental():
    stock, _price, err = _parse_request()
    if err:
        return err
    from app.services.fundamental import get_fundamental

    return _run(stock, lambda: get_fundamental(stock))


@bp.route("/industry", methods=["GET", "POST"])
def industry():
    stock, price, err = _parse_request()
    if err:
        return err
    from app.services.industry import get_industry

    return _run(stock, lambda: get_industry(stock, price))


@bp.route("/financial", methods=["GET", "POST"])
def financial():
    stock, price, err = _parse_request()
    if err:
        return err
    from app.services.financial import get_financials

    return _run(stock, lambda: get_financials(stock, price))


@bp.route("/financial/analysis", methods=["GET", "POST"])
def financial_analysis():
    stock, _price, err = _parse_request()
    if err:
        return err
    from app.agents.financial.graph import analyze_financials

    return _run(stock, lambda: analyze_financials(stock))


@bp.route("/financial/debate", methods=["GET", "POST"])
def financial_debate():
    stock, _price, err = _parse_request()
    if err:
        return err
    from app.agents.financial.graph import debate_financials

    return _show(stock, lambda: debate_financials(stock))


@bp.route("/sector", methods=["GET", "POST"])
def sector():
    days, err = _days(60, 120)
    if err:
        return err
    stock, _price, err = _parse_request()
    if err:
        return err
    from app.services.series import get_sector

    return _run(stock, lambda: get_sector(stock, days))


@bp.route("/market", methods=["GET", "POST"])
def market():
    days, err = _days(60, 120)
    if err:
        return err
    from app.services.series import get_market

    try:
        return jsonify({"status": "ok", "data": get_market(days)})
    except Exception as exc:
        return jsonify({"status": "error", "error": str(exc)}), 500


@bp.route("/margin", methods=["GET", "POST"])
def margin():
    days, err = _days(60, 60)
    if err:
        return err
    stock, _price, err = _parse_request()
    if err:
        return err
    from app.services.series import get_margin

    return _run(stock, lambda: get_margin(stock, days))


@bp.route("/toplist", methods=["GET", "POST"])
def toplist():
    days, err = _days(20, 30)
    if err:
        return err
    stock, _price, err = _parse_request()
    if err:
        return err
    from app.services.series import get_toplist

    return _run(stock, lambda: get_toplist(stock, days))
