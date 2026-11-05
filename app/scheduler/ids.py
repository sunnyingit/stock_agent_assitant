"""ULID：26 位，按时间排序，可以在客户端离线生成。"""

from __future__ import annotations

import os
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def ulid(now_ms: int | None = None) -> str:
    stamp = int(time.time() * 1000) if now_ms is None else now_ms
    value = (stamp << 80) | int.from_bytes(os.urandom(10), "big")
    out = []
    for _ in range(26):
        out.append(_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(out))
