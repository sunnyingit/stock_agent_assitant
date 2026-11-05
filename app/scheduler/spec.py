"""任务定义和注册表。任务定义写在代码里，数据库只存用户的覆盖项（启停、改时间）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from app.scheduler import triggers

PLACEMENTS = ("client", "server")
MISFIRE = ("run_latest", "run_all", "skip")
QUEUES = {"llm": 2, "data": 4, "notify": 4, "light": 8}   # 队列 → 并发


@dataclass
class Unit:
    """fanout 展开出来的一个执行单元。"""
    unit_key: str
    priority: int = 50
    input: dict = field(default_factory=dict)


@dataclass
class TaskSpec:
    task_id: str
    title: str
    handler: Callable
    trigger: dict
    placement: str = "client"
    placement_reason: str = ""          # placement=server 时必填：shared / no_private / costly 或 offline
    queue: str = "light"
    priority: int = 50
    timeout_s: int = 300
    max_attempts: int = 3
    backoff_s: tuple = (60, 300, 1800)
    after: tuple = ()                   # ({"task": "market.close", "mode": "soft", "timeout_min": 30},)
    gates: tuple = ()
    fanout: str = "none"
    misfire: str = "run_latest"
    catchup_ttl_min: int = 720
    budget: dict = field(default_factory=dict)       # {"resource": "llm", "per_unit": {"calls": 8}}
    skip_if_fresh: str = ""
    min_interval_min: int = 0           # 用户改触发间隔时的下限
    expire_after_min: int | None = None  # job 过了这么久还没执行就作废（盘中提醒这类）
    run_on_start: bool = False          # 启动时如果没有任何运行记录，立即补一次
    overdue_after_min: int | None = None  # 应触发时刻之后这么久还没成功，看门狗产出 task.overdue

    def __post_init__(self):
        if self.placement not in PLACEMENTS:
            raise ValueError(f"{self.task_id}: placement 必须是 {PLACEMENTS}")
        if self.placement == "server" and not self.placement_reason.strip():
            raise ValueError(f"{self.task_id}: 放在服务端的任务必须写 placement_reason（shared / no_private / costly / offline）")
        if self.queue not in QUEUES:
            raise ValueError(f"{self.task_id}: 未知队列 {self.queue}")
        if self.misfire not in MISFIRE:
            raise ValueError(f"{self.task_id}: misfire 必须是 {MISFIRE}")
        self.trigger = triggers.validate(self.trigger, self.min_interval_min)


class Registry:
    def __init__(self):
        self._tasks: dict[str, TaskSpec] = {}
        self._fanouts: dict[str, Callable] = {"none": lambda ctx: [Unit("_")]}
        self._gates: dict[str, Callable[[str], bool]] = {}

    # ---- 任务
    def register(self, spec: TaskSpec) -> TaskSpec:
        if spec.task_id in self._tasks:
            raise ValueError(f"任务重复注册：{spec.task_id}")
        self._tasks[spec.task_id] = spec
        return spec

    def get(self, task_id: str) -> TaskSpec:
        try:
            return self._tasks[task_id]
        except KeyError:
            raise KeyError(f"没有这个任务：{task_id}") from None

    def all(self) -> list[TaskSpec]:
        return list(self._tasks.values())

    def for_profile(self, profile: str) -> list[TaskSpec]:
        return [spec for spec in self._tasks.values() if spec.placement == profile]

    # ---- 扇出和就绪门
    def add_fanout(self, name: str, fn: Callable) -> None:
        self._fanouts[name] = fn

    def fanout(self, name: str) -> Callable:
        if name not in self._fanouts:
            raise KeyError(f"没有这个扇出提供者：{name}")
        return self._fanouts[name]

    def add_gate(self, name: str, fn: Callable[[str], bool]) -> None:
        self._gates[name] = fn

    def gate_ready(self, name: str, trade_date: str) -> bool:
        probe = self._gates.get(name)
        if probe is None:
            return True            # 没注册的门当作已就绪，避免拼写错误把任务永远卡住；注册表有测试检查任务里的门都存在
        return bool(probe(trade_date))

    def unknown_gates(self) -> list[str]:
        used = {name for spec in self._tasks.values() for name in spec.gates}
        return sorted(used - set(self._gates))


registry = Registry()


def task(task_id: str, title: str, trigger: dict, **options):
    """装饰器：把处理函数注册成任务。处理函数签名 handler(ctx) -> dict | None。"""
    def wrap(fn: Callable) -> Callable:
        registry.register(TaskSpec(task_id=task_id, title=title, handler=fn, trigger=trigger, **options))
        return fn
    return wrap


def fanout(name: str):
    def wrap(fn: Callable) -> Callable:
        registry.add_fanout(name, fn)
        return fn
    return wrap


def gate(name: str):
    def wrap(fn: Callable) -> Callable:
        registry.add_gate(name, fn)
        return fn
    return wrap
