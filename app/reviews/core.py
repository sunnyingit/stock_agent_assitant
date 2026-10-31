"""AI 体检的通用骨架：资金流和基本面共用的状态文字、缓存、来源、输出渲染和收尾。事实表、模板答案、提示词各自实现。"""

from __future__ import annotations

import time

from app.reviews import attach

CACHE_SECONDS = 3600
STATUS_TEXT = {"support": "偏正面", "neutral": "中性", "warn": "需警惕", "missing": "数据缺失", "na": "不适用"}
SKIPPED = ("missing", "na")   # 程序已经判定无法回答或不适用的问题：模型不作答，保持模板答案


class FactSheet(list):
    """一次构建出来的事实列表，自己带着每条事实的名称（labels）和分组、类型（meta）。"""

    def __init__(self):
        super().__init__()
        self.labels: dict[str, str] = {}
        self.meta: dict[str, tuple[str, str]] = {}


class Unavailable(RuntimeError):
    """这只股票没法做体检，是定论（如港股、上市不足一年），不会反复重试。"""


def source_of(items: list[dict]) -> str:
    """template 全是模板答案；model 所有可判断的问题都由模型回答；mixed 两者都有。"""
    from_model = sum(1 for row in items if row["source"] == "model")
    answerable = len([row for row in items if row["status"] not in SKIPPED])
    return "template" if not from_model else "model" if from_model == answerable else "mixed"


def render_items(items: list[dict], questions: list[tuple[str, str, str]]) -> list[dict]:
    """把内部的答案补上问题标题、追问和状态文字，输出给前端。"""
    titles = {key: (title, ask) for key, title, ask in questions}
    return [
        {
            "key": row["key"], "title": titles[row["key"]][0], "ask": titles[row["key"]][1],
            "status": row["status"], "status_text": STATUS_TEXT[row["status"]], "answer": row["answer"],
            "cites": row["cites"], "source": row["source"],
        }
        for row in items
    ]


def cached(store, key: str, ts_code: str, kind: str, refresh: bool, as_of: str | None = None) -> dict | None:
    """同样的事实、提示词、模型已经算过就直接用，同时记为最近一份。"""
    if store is None or refresh:
        return None
    hit = store.get(key)
    if not hit:
        return None
    hit["cached"] = True
    if as_of:   # 事实没变、解读沿用，但"数据日期"要跟上今天，否则每天都会被当成旧的
        hit["as_of"] = as_of
    attach.remember(store, ts_code, hit, kind=kind)
    return hit


def finish(store, key: str, result: dict, ts_code: str, kind: str, use_model: bool, call_error: str) -> None:
    """收尾：模型调用失败时，模板版先顶着、不缓存、几分钟内不再重试；否则缓存并记为最近一份。"""
    if store is None:
        return
    if use_model and call_error:
        attach.remember(store, ts_code, result, retry_after=time.time() + attach.RETRY_SECONDS, kind=kind)
    else:
        store.set(key, result, CACHE_SECONDS)
        attach.remember(store, ts_code, result, kind=kind)
