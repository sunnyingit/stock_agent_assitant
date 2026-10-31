"""资金面体检接口：GET /api/v1/stocks/<ts_code>/dimensions/volume_flow/review。通用实现见 app/reviews/api.py。"""

from app.reviews.api import make_blueprint

bp = make_blueprint("flow_review", "volume_flow", "app.flow_review.review")
