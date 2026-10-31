"""把 AI 体检并进详情的返回：详情本身不等模型，只读已经生成好的体检；没有或过期就在后台生成。资金流和基本面共用。

review_state：
  ready       体检是新的（今天的数据、1 小时内生成）
  refreshing  返回的是上一份体检，新的正在后台生成
  computing   还没有体检，正在后台生成；前端稍后再请求详情
  unavailable 这只股票没法做体检（如港股），review_error 说明原因
  disabled    后台生成没有启用（只有 python -m app.main 启动时才启用）
"""

from __future__ import annotations

import importlib
import threading
import time
from typing import Callable

RESULT_TTL = 3600           # 体检在这么多秒内算"新的"
LATEST_TTL = 7 * 24 * 3600  # 最近一份体检保留多久（过期了也先返回，同时后台刷新）
RETRY_SECONDS = 300         # 模型调用失败后，多久之内不再重试，避免每次请求都调用一次
PARTIAL_SECONDS = 120       # 体检用的数据还没取全（partial）时，这么久以后就当旧的，重新生成一次

# 每一类体检：最近一份的存储键前缀、负责生成的模块（要有 build() 和 Unavailable）
KINDS = {
    "flow": {"prefix": "flow_review:latest4:", "module": "app.flow_review.review", "prompt": "app.flow_review.prompt", "thread": "flow-review"},
    "fund": {"prefix": "fund_review:latest1:", "module": "app.fundamental_review.review", "prompt": "app.fundamental_review.prompt", "thread": "fund-review"},
    "fin": {"prefix": "fin_review:latest1:", "module": "app.finance_review.review", "prompt": "app.finance_review.prompt", "thread": "fin-review"},
}
FAILED_PREFIX = "体检生成失败"

_lock = threading.Lock()
_inflight: set[str] = set()
_enabled = False


def enable() -> None:
    global _enabled
    _enabled = True


def enabled() -> bool:
    return _enabled


def latest_key(ts_code: str, kind: str = "flow") -> str:
    return f"{KINDS[kind]['prefix']}{ts_code}"


def remember(store, ts_code: str, result: dict | None, error: str = "", retry_after: float = 0.0, kind: str = "flow") -> None:
    """build 算完后调用：记下最近一份体检（或不可用的原因）。"""
    if store is None:
        return
    store.set(latest_key(ts_code, kind), {"ts": time.time(), "result": result, "error": error, "retry_after": retry_after}, LATEST_TTL)


_DETAIL_DROP = ("debug", "cached", "ts_code", "stock_name", "as_of", "disclaimer")


def _public(result: dict | None) -> dict | None:
    """并进详情的副本：股票名、日期、免责声明详情里已有，不重复。"""
    return None if result is None else {key: value for key, value in result.items() if key not in _DETAIL_DROP}


def _outdated_prompt(result: dict, kind: str) -> bool:
    """体检是用旧版提示词生成的：改了提示词以后，不再把它当成新的（先返回，同时后台重新生成）。没记录版本的当作没过期。"""
    stored = (result.get("debug") or {}).get("prompt_version")
    if not stored:
        return False
    current = importlib.import_module(KINDS[kind]["prompt"]).PROMPT_VERSION
    return stored != current


def _flight(ts_code: str, kind: str) -> str:
    return ts_code if kind == "flow" else f"{kind}:{ts_code}"


def _spawn(stock, store, kind: str = "flow") -> None:
    """后台线程里生成体检；同一只股票同一类体检同一时刻只跑一个。"""
    flight = _flight(stock.ts_code, kind)
    with _lock:
        if flight in _inflight:
            return
        _inflight.add(flight)

    def run() -> None:
        from app import config
        from app.synthesis.gateway import Gateway

        module = importlib.import_module(KINDS[kind]["module"])   # 放在函数里：生成模块也要 import 本模块，避免循环导入
        try:
            module.build(stock, store=store, gateway=Gateway.from_config(), model_tag=config.LLM_MODEL)
        except module.Unavailable as exc:
            remember(store, stock.ts_code, None, error=str(exc), kind=kind)
        except Exception as exc:   # 后台线程里的任何错误都不能影响详情接口
            remember(store, stock.ts_code, None, error=f"{FAILED_PREFIX}: {exc}", retry_after=time.time() + RETRY_SECONDS, kind=kind)
        finally:
            with _lock:
                _inflight.discard(flight)

    threading.Thread(target=run, daemon=True, name=f"{KINDS[kind]['thread']}-{stock.ts_code}").start()


def attach(payload: dict, stock, store, now: float | None = None, spawn: Callable | None = None, kind: str = "flow") -> dict:
    """返回要并进详情的 {"review": ..., "review_state": ..., "review_error": ...}。不会等待模型。

    spawn 可以注入（测试用）；不注入时，只有 enable() 之后才会真的起后台线程。
    """
    now = now if now is not None else time.time()
    launch = spawn or (lambda target, where: _spawn(target, where, kind))
    can_spawn = spawn is not None or _enabled
    entry = store.get(latest_key(stock.ts_code, kind)) if store is not None else None
    if entry and entry.get("error") and not entry.get("result"):
        # 不可用（如港股）是定论；"生成失败"可以隔一段时间再试
        if entry["error"].startswith(FAILED_PREFIX) and now >= entry.get("retry_after", 0) and can_spawn:
            launch(stock, store)
        return {"review": None, "review_state": "unavailable", "review_error": entry["error"]}
    result = entry["result"] if entry else None
    if result is None:
        state = "computing"
    else:
        age = now - entry["ts"]
        fresh = (
            age <= RESULT_TTL
            and result.get("as_of") == (payload.get("meta") or {}).get("as_of")
            and not (result.get("partial") and age > PARTIAL_SECONDS)
            and not _outdated_prompt(result, kind)
        )
        state = "ready" if fresh else "refreshing"
    if state != "ready":
        if not can_spawn:
            return {"review": _public(result), "review_state": "disabled" if result is None else state, "review_error": ""}
        if now >= (entry or {}).get("retry_after", 0):
            launch(stock, store)
    return {"review": _public(result), "review_state": state, "review_error": ""}
