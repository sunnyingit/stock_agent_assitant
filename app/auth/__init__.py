"""请求身份：X-User-Id / Bearer → user_id。"""

from app.auth.user import current_user_id, require_user_id, resolve_user_id

__all__ = ["current_user_id", "require_user_id", "resolve_user_id"]
