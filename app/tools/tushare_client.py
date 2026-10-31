"""Tushare Pro 客户端。Token 从环境变量 TUSHARE_TOKEN 读取。"""

from __future__ import annotations

import tushare as ts

from app import config

_pro = None


def get_pro():
    global _pro
    if _pro is not None:
        return _pro
    if not config.TUSHARE_TOKEN:
        raise RuntimeError("未配置 TUSHARE_TOKEN")
    ts.set_token(config.TUSHARE_TOKEN)
    _pro = ts.pro_api()
    return _pro
