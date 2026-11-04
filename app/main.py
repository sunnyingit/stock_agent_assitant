"""启动查询服务。在项目目录执行: python -m app.main"""

import os

from flask import Flask

from app.api.stocks import bp
from app.api.v1 import bp as v1_bp
from app.flow_review.api import bp as flow_review_bp
from app.finance_review.api import bp as finance_review_bp
from app.fundamental_review.api import bp as fundamental_review_bp
from app.sector_review.api import bp as sector_review_bp
from app.synthesis.api import bp as synthesis_bp

_ROUTES = (
    ("基本行情", "/api/stocks/basic"),
    ("日线", "/api/stocks/bars"),
    ("新闻", "/api/stocks/news"),
    ("美股韩股走向", "/api/stocks/peers"),
    ("基本面", "/api/stocks/fundamental"),
    ("所属板块", "/api/stocks/industry"),
    ("财务报表", "/api/stocks/financial"),
    ("板块资金", "/api/stocks/sector"),
    ("大盘", "/api/stocks/market"),
    ("融资融券", "/api/stocks/margin"),
    ("龙虎榜", "/api/stocks/toplist"),
    ("资金流详情", "/api/v1/stocks/{ts_code}/dimensions/volume_flow/detail", "?window=5"),
    ("资金流流式", "/api/v1/stocks/{ts_code}/dimensions/volume_flow/stream", "?window=5"),
    ("新闻详情", "/api/v1/stocks/{ts_code}/dimensions/news/detail", "?days=3"),
    ("新闻流式", "/api/v1/stocks/{ts_code}/dimensions/news/stream", "?days=3"),
    ("基本面详情", "/api/v1/stocks/{ts_code}/dimensions/fundamental/detail", ""),
    ("基本面流式", "/api/v1/stocks/{ts_code}/dimensions/fundamental/stream", ""),
    ("财报详情", "/api/v1/stocks/{ts_code}/dimensions/finance/detail", ""),
    ("财报流式", "/api/v1/stocks/{ts_code}/dimensions/finance/stream", ""),
    ("板块详情", "/api/v1/stocks/{ts_code}/dimensions/sector/detail", ""),
    ("板块流式", "/api/v1/stocks/{ts_code}/dimensions/sector/stream", ""),
    ("分析师追问", "/api/v1/stocks/{ts_code}/dimensions/{dimension}/reply", "POST question"),
    ("综合判断", "/api/v1/stocks/{ts_code}/synthesis", ""),
    ("主分析师流式", "/api/v1/stocks/{ts_code}/chief/stream", ""),
    ("主分析师详情", "/api/v1/stocks/{ts_code}/chief/detail", ""),
    ("主分析师追问", "/api/v1/stocks/{ts_code}/chief/reply", "POST question"),
    ("看多流式", "/api/v1/stocks/{ts_code}/bull/stream", ""),
    ("看多追问", "/api/v1/stocks/{ts_code}/bull/reply", "POST question"),
    ("看空流式", "/api/v1/stocks/{ts_code}/bear/stream", ""),
    ("看空追问", "/api/v1/stocks/{ts_code}/bear/reply", "POST question"),
    ("自选诊断", "/api/v1/watchlist", ""),
    ("资金面体检", "/api/v1/stocks/{ts_code}/dimensions/volume_flow/review", ""),
    ("基本面体检", "/api/v1/stocks/{ts_code}/dimensions/fundamental/review", ""),
    ("财报解读", "/api/v1/stocks/{ts_code}/dimensions/finance/review", ""),
    ("板块解读", "/api/v1/stocks/{ts_code}/dimensions/sector/review", ""),
)


def create_app() -> Flask:
    app = Flask(__name__)

    @app.before_request
    def _cors_preflight():
        # 浏览器跨域 DELETE/POST 会先发 OPTIONS；缺 Allow-Methods 时删自选会被拦下
        from flask import request

        if request.method == "OPTIONS":
            from flask import make_response

            return make_response("", 204)

    @app.after_request
    def _cors(response):
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
        return response

    app.register_blueprint(bp)
    app.register_blueprint(v1_bp)
    app.register_blueprint(synthesis_bp)
    app.register_blueprint(flow_review_bp)
    app.register_blueprint(fundamental_review_bp)
    app.register_blueprint(finance_review_bp)
    app.register_blueprint(sector_review_bp)
    return app


def main() -> None:
    port = int(os.environ.get("PORT", "8090"))
    app = create_app()
    from app.reviews import attach
    from app.synthesis import worker

    worker.start()
    from app.services import market_job

    market_job.start()   # 交易日 08:30 / 15:35 的市场简报和快照
    attach.enable()   # 资金流、基本面详情里的 AI 体检在后台生成
    print(f"股票查询服务: http://localhost:{port}")
    for item in _ROUTES:
        label, path = item[0], item[1]
        if len(item) == 3:
            suffix = item[2]
        elif path.endswith("/market"):
            suffix = ""
        else:
            suffix = "?q=贵州茅台"
        sample = path.replace("{ts_code}", "600519.SH")
        print(f"  {label}: http://localhost:{port}{sample}{suffix}")
    app.run(host="0.0.0.0", port=port, debug=False)


if __name__ == "__main__":
    main()
