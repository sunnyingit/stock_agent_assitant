"""从请求头解析 user_id，并判断路径是否需要登录。"""

from __future__ import annotations

import re

from flask import g, has_request_context, jsonify, request

# 个人数据接口：缺身份返回 401。行情/shared 等保持可匿名。
_PERSONAL_PREFIXES = (
    "/api/v1/holdings",
    "/notify",
    "/api/v1/notify",
    "/api/v1/watchlist",
    "/sched",
)

_PERSONAL_EXACT_OR_PREFIX = (
    "/api/v1/market/settings",
    "/api/v1/market/notifications",
)

# 综合判断 / 自选相关写读个人表
_PERSONAL_REGEX = (
    re.compile(r"^/api/v1/stocks/[^/]+/synthesis"),
    re.compile(r"^/api/v1/stocks/[^/]+/(chief|bull|bear)/"),
    re.compile(r"^/api/v1/stocks/[^/]+/dimensions/[^/]+/review"),
)


def resolve_user_id(req=None) -> str | None:
    """X-User-Id 优先；否则 Authorization: Bearer <token> 把 token 原文当作 user_id。"""
    req = req or request
    header = (req.headers.get("X-User-Id") or "").strip()
    if header:
        return header
    auth = (req.headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        return token or None
    return None


def current_user_id() -> str | None:
    if has_request_context():
        return getattr(g, "user_id", None)
    return None


def require_user_id() -> str:
    uid = current_user_id()
    if not uid:
        raise PermissionError("missing user id")
    return uid


def is_personal_path(path: str) -> bool:
    path = path or ""
    for prefix in _PERSONAL_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return True
    for prefix in _PERSONAL_EXACT_OR_PREFIX:
        if path == prefix or path.startswith(prefix + "/"):
            return True
    for rx in _PERSONAL_REGEX:
        if rx.search(path):
            return True
    return False


def unauthorized_response():
    return jsonify({"error": "未登录：请在请求头带 X-User-Id 或 Authorization: Bearer <token>"}), 401
