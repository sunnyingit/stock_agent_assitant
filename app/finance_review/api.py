"""财报解读接口：GET /api/v1/stocks/<ts_code>/dimensions/finance/review。通用实现见 app/reviews/api.py。"""

from app.reviews.api import make_blueprint

bp = make_blueprint("finance_review", "finance", "app.finance_review.review")
