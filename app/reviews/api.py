"""AI 体检接口的通用实现：资金流和基本面各自注册一个，路径是 /api/v1/stocks/<ts_code>/dimensions/<维度>/review。只增不改：不碰各自的详情返回。"""

from __future__ import annotations

import importlib

from flask import Blueprint, jsonify, request

from app.synthesis import worker
from app.synthesis.gateway import Gateway
from app.tools.stock import resolve_stock


def make_blueprint(name: str, dimension: str, review_module: str) -> Blueprint:
    """review_module 要有 build() 和 Unavailable。模块在请求时才导入，避免和生成模块循环导入。"""
    bp = Blueprint(name, __name__, url_prefix="/api/v1")

    @bp.route(f"/stocks/<ts_code>/dimensions/{dimension}/review", methods=["GET"], endpoint=name)
    def review_of(ts_code: str):
        """?refresh=1 强制重算；?debug=1 带上校验错误等调试信息。"""
        stock = resolve_stock(ts_code)
        if stock is None:
            return jsonify({"error": f"未找到股票: {ts_code}"}), 404
        from app import config

        module = importlib.import_module(review_module)
        try:
            result = module.build(
                stock, store=worker.get_store(), gateway=Gateway.from_config(), model_tag=config.LLM_MODEL,
                refresh=request.args.get("refresh") == "1",
            )
        except module.Unavailable as exc:
            return jsonify({"status": "unavailable", "ts_code": stock.ts_code, "stock_name": stock.name, "error": str(exc)}), 200
        except Exception as exc:
            return jsonify({"status": "error", "ts_code": stock.ts_code, "stock_name": stock.name, "error": str(exc)}), 500
        if request.args.get("debug") != "1":
            result = {key: value for key, value in result.items() if key != "debug"}
        return jsonify({"status": "ok", **result})

    return bp
