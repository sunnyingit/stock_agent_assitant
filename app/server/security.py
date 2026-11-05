"""设备令牌、管理员令牌、渠道密钥加密。令牌只存哈希；密钥用主密钥加密，主密钥来自环境变量。"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from functools import wraps
from pathlib import Path

from flask import current_app, g, jsonify, request

_KEY_FILE = Path(__file__).resolve().parents[2] / "data" / "relay.key"


def new_token() -> tuple[str, str]:
    secret = secrets.token_urlsafe(32)
    return secret, hashlib.sha256(secret.encode()).hexdigest()


def verify(token_secret: str, token_hash: str) -> bool:
    return hmac.compare_digest(hashlib.sha256(token_secret.encode()).hexdigest(), token_hash)


def _fernet():
    from cryptography.fernet import Fernet

    key = os.environ.get("RELAY_MASTER_KEY", "").strip()
    if not key:
        # 开发环境：没配就在本机生成一把并落盘。线上必须用环境变量或密钥管理服务。
        if _KEY_FILE.exists():
            key = _KEY_FILE.read_text().strip()
        else:
            key = Fernet.generate_key().decode()
            _KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
            _KEY_FILE.write_text(key)
            _KEY_FILE.chmod(0o600)
    return Fernet(key.encode())


def encrypt(plain: str, fernet=None) -> bytes:
    return (fernet or _fernet()).encrypt(plain.encode("utf-8"))


def decrypt(blob: bytes, fernet=None) -> str:
    return (fernet or _fernet()).decrypt(bytes(blob)).decode("utf-8")


def _err(status: int, code: str, message: str):
    return jsonify({"error": {"code": code, "message": message, "details": {}}}), status


def device_required(fn):
    """Authorization: Bearer <device_id>.<secret>。校验通过后 g.device_id 可用。"""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        header = request.headers.get("Authorization", "")
        token = header[7:].strip() if header.startswith("Bearer ") else ""
        device_id, _, secret = token.partition(".")
        store = current_app.config["SERVER_STORE"]
        clock = current_app.config["SERVER_CLOCK"]
        device = store.device(device_id) if device_id and secret else None
        if device is None or not verify(secret, device["token_hash"]):
            return _err(401, "unauthorized", "设备令牌无效或已撤销")
        store.touch_device(device_id, clock.now_ms())
        g.device_id = device_id
        return fn(*args, **kwargs)
    return wrapper


def admin_check():
    """管理接口：ADMIN_TOKEN 没配就整个关掉。"""
    expected = os.environ.get("ADMIN_TOKEN", "").strip()
    if not expected:
        return _err(403, "admin_disabled", "没有配置管理员令牌，管理接口已关闭")
    header = request.headers.get("Authorization", "")
    token = header[7:].strip() if header.startswith("Bearer ") else ""
    if not hmac.compare_digest(token, expected):
        return _err(401, "unauthorized", "管理员令牌无效")
    return None
