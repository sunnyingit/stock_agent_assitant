"""基本面体检接口：GET /api/v1/stocks/<ts_code>/dimensions/fundamental/review。通用实现见 app/reviews/api.py。"""

from app.reviews.api import make_blueprint

bp = make_blueprint("fundamental_review", "fundamental", "app.fundamental_review.review")
