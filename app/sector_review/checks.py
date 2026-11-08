"""板块解读的额外核对（接在逐项校验之后，不通过就用模板答案）。

1. 归属题必须引用判定依据：第 1 题引用主板块的依据（sp.p.basis），第 2 题有次板块时引用 sp.s.basis。
   模型不能只说"属于"，而不给出业务占比或联动与地位的证据。
2. 只挂名的板块不能被说成"属于"：第 1、2 题的回答里出现 sp.hang 里的板块名，不通过。
3. 阶段题（第 5、6 题）必须引用对应板块的阶段。
"""

from __future__ import annotations

from typing import Callable

MUST_CITE = {"q1": "sp.p.basis", "q2": "sp.s.basis", "q5": "sp.p.stage", "q6": "sp.s.stage"}


def make_extra(available: set[str], hanging: list[str]) -> Callable[[str, str, list[str]], list[str]]:
    def extra(key: str, answer: str, cites: list[str]) -> list[str]:
        problems = []
        need = MUST_CITE.get(key)
        if need and need in available and need not in cites:
            problems.append(f"引用:{key}没有引用 {need}")
        if key in ("q1", "q2"):
            named = [name for name in hanging if name and name in answer]
            if named:
                problems.append(f"归属:{key}提到了只挂名的板块 {named}")
        return problems

    return extra
