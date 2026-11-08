"""板块解读接口：GET /api/v1/stocks/<ts_code>/dimensions/sector/review。通用实现见 app/reviews/api.py。"""

from app.reviews.api import make_blueprint

bp = make_blueprint("sector_review", "sector", "app.sector_review.review")
