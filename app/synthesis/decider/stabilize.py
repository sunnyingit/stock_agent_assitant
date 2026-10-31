"""防止来回翻，以及"为什么变"的一句话。"""

from __future__ import annotations

from app.synthesis import policy


def anti_flip(candidate: dict, previous: dict | None) -> tuple[str, int, bool]:
    """candidate: {direction, level, spread}；previous: 上一条存档的 final。

    方向要改变时，必须满足其一：连续两次重算都指向新方向；或新结论的 |分差| ≥ FLIP_MIN_SPREAD。
    否则维持上次的方向，强度降一档。返回 (方向, 强度, 是否被压住)。
    """
    direction, level = candidate["direction"], candidate["level"]
    if not previous or not previous.get("direction"):
        return direction, level, False
    last = previous["direction"]
    if direction == last:
        return direction, level, False
    confirmed = previous.get("raw_direction") == direction
    big = abs(candidate.get("spread") or 0) >= policy.FLIP_MIN_SPREAD
    if confirmed or big:
        return direction, level, False
    kept_level = previous.get("level") or 0
    reduced = max(kept_level - 1, 1) if last in ("bullish", "bearish") else 0
    return last, reduced, True


def change_reason(previous: dict | None, direction: str, level: int, adopted: list[dict]) -> str:
    """对比前后两次被采信的论据，生成一句"为什么变"。方向和强度都没变时返回空串。"""
    if not previous or not previous.get("direction"):
        return ""
    last_direction, last_level = previous["direction"], previous.get("level") or 0
    if (last_direction, last_level) == (direction, level):
        return ""
    old = {item["claim"]: item for item in previous.get("adopted") or []}
    new = {item["claim"]: item for item in adopted}
    gained = [item for claim, item in new.items() if claim not in old]
    lost = [item for claim, item in old.items() if claim not in new]
    gained.sort(key=lambda item: -item["weight"])
    lost.sort(key=lambda item: -item["weight"])
    if last_direction != direction:
        head = f"从{policy.DIRECTION_TEXT[last_direction]}变为{policy.DIRECTION_TEXT[direction]}"
    else:
        word = "增强" if level > last_level else "减弱"
        head = f"{policy.DIRECTION_TEXT[direction]}的程度{word}"
    parts = []
    if gained:
        parts.append(f"新增：{gained[0]['claim']}")
    if lost:
        parts.append(f"不再成立：{lost[0]['claim']}")
    return head + ("：" + "；".join(parts) if parts else "")
