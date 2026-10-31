"""统一的 JSON 响应。"""

from flask import jsonify


def ok(stock, data):
    return jsonify({
        "status": "ok",
        "ts_code": stock.ts_code,
        "stock_name": stock.name,
        "data": data,
    })


def shown(stock, payload):
    """辩论接口：结论直接放在外层，不套进 data。"""
    return jsonify({
        "status": "ok",
        "ts_code": stock.ts_code,
        "stock_name": stock.name,
        "generated_at": payload["generated_at"],
        "display": payload["display"],
        "internal": payload["internal"],
    })


def fail(stock, exc: Exception):
    return jsonify({
        "status": "error",
        "ts_code": stock.ts_code,
        "stock_name": stock.name,
        "error": str(exc),
    }), 500
