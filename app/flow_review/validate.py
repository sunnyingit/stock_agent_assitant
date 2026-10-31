"""资金面体检的逐项校验：通用实现在 app/reviews/validate.py，这里绑定资金流自己的禁用词和"状态由事实决定"的问题。"""

from __future__ import annotations

from app.flow_review.prompt import BANNED
from app.reviews import validate as shared
from app.reviews.validate import ANSWER_MAX, STATUSES
from app.synthesis.types import FactTable

# 这些问题的状态由事实按固定标准决定：模型的状态和参考状态正反相反时，以参考状态为准
GUARDED = ("q1", "q2", "q3", "q4", "q5", "q7", "q8")
__all__ = ["ANSWER_MAX", "GUARDED", "STATUSES", "check", "merge_items"]


def check(text: str, cites: list[str], table: FactTable, where: str, need_cites: bool = True, limit: int = ANSWER_MAX) -> list[str]:
    return shared.check(text, cites, table, where, banned=BANNED, need_cites=need_cites, limit=limit)


def merge_items(
    raw: dict, table: FactTable, template: list[dict], failed: dict | None = None, overridden: list | None = None,
    guarded: tuple = GUARDED,
) -> tuple[list[dict], list[str]]:
    return shared.merge_items(raw, table, template, banned=BANNED, guarded=guarded, failed=failed, overridden=overridden)
