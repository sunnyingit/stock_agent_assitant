"""项目根与可写数据目录。

安装包里代码在只读 runtime 下，数据库/缓存必须落到用户目录：由宿主设置 STOCK_DATA_DIR。
开发时不设环境变量，仍用仓库下的 data/。
"""

from __future__ import annotations

import os
from pathlib import Path


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def data_dir() -> Path:
    raw = (os.environ.get("STOCK_DATA_DIR") or "").strip()
    root = Path(raw).expanduser() if raw else project_root() / "data"
    root.mkdir(parents=True, exist_ok=True)
    return root
