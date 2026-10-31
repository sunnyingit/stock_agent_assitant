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
    ("资金流", "/api/stocks/moneyflow"),
    ("资金流日序列", "/api/stocks/moneyflow/daily"),
    ("新闻", "/api/stocks/news"),
    ("新闻分析", "/api/stocks/news/analysis"),
    ("新闻辩论", "/api/stocks/news/debate"),
    ("美股韩股走向", "/api/stocks/peers"),
    ("基本面", "/api/stocks/fundamental"),
    ("所属板块", "/api/stocks/industry"),
    ("财务报表", "/api/stocks/financial"),
    ("财报分析", "/api/stocks/financial/analysis"),
    ("财报辩论", "/api/stocks/financial/debate"),
    ("板块资金", "/api/stocks/sector"),
    ("大盘", "/api/stocks/market"),
    ("融资融券", "/api/stocks/margin"),
    ("龙虎榜", "/api/stocks/toplist"),
    ("资金流详情", "/api/v1/stocks/{ts_code}/dimensions/volume_flow/detail", "?window=20"),
    ("新闻详情", "/api/v1/stocks/{ts_code}/dimensions/news/detail", "?days=3"),
    ("基本面详情", "/api/v1/stocks/{ts_code}/dimensions/fundamental/detail", ""),
    ("财报详情", "/api/v1/stocks/{ts_code}/dimensions/finance/detail", ""),
    ("板块详情", "/api/v1/stocks/{ts_code}/dimensions/sector/detail", ""),
    ("综合判断", "/api/v1/stocks/{ts_code}/synthesis", ""),
    ("自选诊断", "/api/v1/watchlist", ""),
    ("资金面体检", "/api/v1/stocks/{ts_code}/dimensions/volume_flow/review", ""),
    ("基本面体检", "/api/v1/stocks/{ts_code}/dimensions/fundamental/review", ""),
    ("财报解读", "/api/v1/stocks/{ts_code}/dimensions/finance/review", ""),
    ("板块解读", "/api/v1/stocks/{ts_code}/dimensions/sector/review", ""),
)


def create_app() -> Flask:
    app = Flask(__name__)

    @app.after_request
    def _cors(response):
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
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
