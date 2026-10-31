"""财报解读的两道防幻觉核对（设计稿核对规则 3 和 4），接在逐项校验之后，不通过就用模板答案。

3. 因果归因必须有出处：回答里出现"受益于、由于、得益于、受……影响"这类因果词，所引用的事实里必须有能解释原因的那一类
   （利润桥的某一项、预告的变动原因、毛利率还是费用率这样由代码归因的结论）；没有就不通过，宁可只陈述数字。
4. 正反两面都要提：正面、负面信号候选两边都不为空时，第 1 题和第 12 题必须各引用至少一个正面、一个负面信号。
"""

from __future__ import annotations

import re
from typing import Callable

CAUSAL = re.compile(r"受益于|得益于|由于|因为|归因于|缘于|源于|导致|拖累|带动|受.{1,12}影响")
TWO_SIDED = ("q1", "q12")
_CAUSE_FACTS = {"ff.margin.driver", "ff.expect.reason", "ff.growth.consolidation"}   # 代码或公司文本给出的原因；利润桥上线后，ff.bridge.* 也算


def is_cause_source(fact_id: str) -> bool:
    return fact_id in _CAUSE_FACTS or fact_id.startswith("ff.bridge.")


def causal(key: str, answer: str, cites: list[str]) -> list[str]:
    word = CAUSAL.search(answer)
    if word and not any(is_cause_source(cite) for cite in cites):
        return [f"归因:{key}写了因果词「{word.group(0)}」，但引用里没有能解释原因的事实"]
    return []


def two_sided(key: str, cites: list[str], signals: dict[str, list[str]]) -> list[str]:
    if key not in TWO_SIDED or not signals.get("positive") or not signals.get("negative"):
        return []
    problems = []
    if not set(cites) & set(signals["positive"]):
        problems.append(f"两面:{key}没有引用任何正面信号 {signals['positive']}")
    if not set(cites) & set(signals["negative"]):
        problems.append(f"两面:{key}没有引用任何负面信号 {signals['negative']}")
    return problems


CYCLE_PHRASE = "周期高位"
CYCLE_KEYS = ("q1", "q12")


def cycle_note(key: str, answer: str, cycle_high: bool) -> list[str]:
    """毛利率处于近 5 年 90% 分位以上时，第 1、12 题必须提示利润可能处于周期高位；没到这条线，任何题都不能这么说。"""
    if cycle_high and key in CYCLE_KEYS and CYCLE_PHRASE not in answer:
        return [f"提示:{key}没有提示利润可能处于周期高位"]
    if not cycle_high and CYCLE_PHRASE in answer:
        return [f"提示:{key}写了周期高位，但单季毛利率没有到近 5 年 90% 分位"]
    return []


def make_extra(signals: dict[str, list[str]], cycle_high: bool = False) -> Callable[[str, str, list[str]], list[str]]:
    def extra(key: str, answer: str, cites: list[str]) -> list[str]:
        return causal(key, answer, cites) + two_sided(key, cites, signals) + cycle_note(key, answer, cycle_high)

    return extra
