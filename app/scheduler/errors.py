"""任务失败分三类，决定怎么重试。处理函数抛对应的异常；别的异常一律按瞬时错误处理（重试有次数上限，不会无限重试）。"""

from __future__ import annotations


class TaskError(Exception):
    klass = "transient"
    code = "ERROR"

    def __init__(self, message: str = "", code: str | None = None):
        super().__init__(message)
        if code:
            self.code = code


class NotReady(TaskError):
    """数据还没出来。不消耗尝试次数，按间隔再试，直到截止。"""

    klass = "not_ready"
    code = "NOT_READY"

    def __init__(self, message: str = "", retry_after_s: int = 300, code: str | None = None):
        super().__init__(message, code)
        self.retry_after_s = retry_after_s


class Transient(TaskError):
    """超时、限流、网络：指数退避重试。"""

    klass = "transient"
    code = "TRANSIENT"


class Permanent(TaskError):
    """代码不存在、参数错、数据结构变了：重试也没用，直接进死信。"""

    klass = "permanent"
    code = "PERMANENT"


def classify(exc: BaseException) -> tuple[str, str]:
    """异常 → (类别, 代码)。"""
    if isinstance(exc, TaskError):
        return exc.klass, exc.code
    if isinstance(exc, (ValueError, KeyError, TypeError, LookupError)):
        return "permanent", type(exc).__name__.upper()
    return "transient", type(exc).__name__.upper()
