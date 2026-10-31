"""逐项校验模型的输出：每一项单独判断，不通过的那一项换成模板答案，不影响其他项。资金流和基本面体检共用。

禁用词和"状态由事实决定"的问题编号随各自的提示词传入，这里不写死。
"""

from __future__ import annotations

import re
from typing import Callable

from app.reviews.core import SKIPPED
from app.synthesis.decider.validate import _PRICE, allowed_numbers, bad_numbers, known_ids
from app.synthesis.types import FactTable

STATUSES = ("support", "neutral", "warn")
_NAMES = re.compile(r"(沪深\s*300|中证\s*(?:500|1000)|上证\s*50|科创\s*50|创业板指)")   # 指数名称里的数字不是数据，核对数字时先去掉
ANSWER_MAX = 120


def check(
    text: str, cites: list[str], table: FactTable, where: str, *, banned: tuple,
    need_cites: bool = True, limit: int = ANSWER_MAX,
) -> list[str]:
    text = str(text or "").strip()
    if not text:
        return [f"格式:{where}为空"]
    errors = []
    if len(text) > limit:
        errors.append(f"格式:{where}超过 {limit} 字")
    ids = known_ids(table)
    unknown = [item for item in cites if item not in ids]
    if unknown:
        errors.append(f"引用:{where}引用了不存在的事实 {unknown}")
    if need_cites and not cites:
        errors.append(f"引用:{where}没有引用")
    for word in banned:
        if word in text:
            errors.append(f"文案:{where}含禁用词「{word}」")
    if _PRICE.search(text):
        errors.append(f"数字:{where}出现了价格")
    plain = _NAMES.sub("", text)
    wrong = bad_numbers(plain, allowed_numbers(table, [item for item in cites if item in ids]))
    if wrong:
        errors.append(f"数字:{where}里的数字 {wrong} 在所引用的事实中找不到")
    return errors


def cites_of(value) -> list[str]:
    return [str(item).strip() for item in value if str(item).strip()] if isinstance(value, list) else []


def merge_items(
    raw: dict, table: FactTable, template: list[dict], *, banned: tuple, guarded: tuple,
    failed: dict | None = None, overridden: list | None = None, allowed: dict[str, set] | None = None,
    extra: Callable[[str, str, list[str]], list[str]] | None = None,
) -> tuple[list[dict], list[str]]:
    """对每个问题：程序判定数据缺失的保持缺失；其余的优先用模型的回答，没通过校验就用模板的。

    failed 不为空时，把没通过的项记进去（{key: {"answer", "errors"}}），给"让模型只重写出错的那几项"用。
    overridden 不为空时，把状态被参考状态改回去的问题编号记进去。
    allowed 给出时，每题只能引用自己的事实子集（{问题编号: 事实编号集合}），越界引用判不通过。
    extra 给出时，对每个回答再追加一道核对：extra(问题编号, 回答, 引用) 返回问题列表，非空就判不通过。
    """
    by_key = {}
    for row in (raw.get("items") if isinstance(raw, dict) else None) or []:
        if isinstance(row, dict):
            by_key[str(row.get("key") or "")] = row
    out, errors = [], []
    for base in template:
        row = by_key.get(base["key"])
        if base["status"] in SKIPPED or row is None:
            out.append(base)
            continue
        status, cites = str(row.get("status") or ""), cites_of(row.get("cites"))
        problems = check(row.get("answer"), cites, table, base["key"], banned=banned)
        if allowed is not None:
            outside = [item for item in cites if item not in allowed.get(base["key"], set())]
            if outside:
                problems.append(f"引用:{base['key']}引用了不属于本题的事实 {outside}")
        if extra is not None:
            problems += extra(base["key"], str(row.get("answer") or ""), cites)
        if status not in STATUSES:
            problems.append(f"格式:{base['key']} 的状态 {status!r} 不在 support/neutral/warn 里")
        if problems:
            errors.extend(problems)
            if failed is not None:
                failed[base["key"]] = {"answer": str(row.get("answer") or ""), "status": status, "errors": problems}
            out.append(base)
            continue
        if base["key"] in guarded and {status, base["status"]} == {"support", "warn"}:
            if overridden is not None:
                overridden.append(base["key"])
            status = base["status"]
        out.append({"key": base["key"], "status": status, "answer": str(row["answer"]).strip(), "cites": cites, "source": "model"})
    return out, errors
