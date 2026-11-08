"""模型网关：统一超时、并行和调用记录。所有模型调用都从这里过，存档里的原始输出就是这里记下的。"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field

from app.synthesis import policy


@dataclass
class CallResult:
    role: str
    ok: bool
    data: dict = field(default_factory=dict)
    error: str = ""
    ms: int = 0


class Gateway:
    """model 可以是 LangChain 聊天模型，也可以是实现了 complete(system, user) 的测试替身。"""

    def __init__(self, model=None, timeout: float | None = None, error: str = ""):
        self.model = model
        self.timeout = timeout or policy.MODEL_TIMEOUT
        self.error = error
        self.calls: list[dict] = []

    @classmethod
    def from_config(cls) -> "Gateway":
        try:
            from app.agents.llm import get_chat_model

            return cls(get_chat_model())
        except Exception as exc:
            return cls(None, error=str(exc))

    @property
    def available(self) -> bool:
        return self.model is not None

    def _call(self, role: str, system: str, user: str, temperature: float, *, plain: bool = False) -> CallResult:
        started = time.monotonic()
        try:
            if plain:
                from app.agents.llm import complete_text

                data = {"text": complete_text(self.model, system, user, temperature)}
            else:
                from app.agents.llm import complete_json

                data = complete_json(self.model, system, user, temperature)
            result = CallResult(role, True, data)
        except Exception as exc:
            result = CallResult(role, False, error=str(exc))
        result.ms = int((time.monotonic() - started) * 1000)
        return result

    def run(self, jobs: list[tuple[str, str, str, float]], *, plain: bool = False) -> list[CallResult]:
        """并行调用，jobs 是 (角色, system, user, 温度)。超时的那一次记为失败。plain=True 时返回纯文本。"""
        if not self.available:
            return [CallResult(role, False, error=self.error or "没有可用的模型") for role, *_ in jobs]
        pool = ThreadPoolExecutor(max_workers=max(1, len(jobs)))
        futures = [pool.submit(self._call, *job, plain=plain) for job in jobs]
        done, _pending = wait(futures, timeout=self.timeout)
        pool.shutdown(wait=False, cancel_futures=True)
        results = []
        for job, future in zip(jobs, futures):
            result = future.result() if future in done else CallResult(job[0], False, error="模型调用超时")
            self.calls.append({
                "role": result.role, "ok": result.ok, "ms": result.ms,
                "error": result.error, "output": result.data if result.ok else None,
            })
            results.append(result)
        return results
