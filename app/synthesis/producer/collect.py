"""并发读 5 个维度的详情。各维度自己的缓存照常生效，这里不重复缓存。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable

from app.synthesis import policy
from app.synthesis.calendar import now_cn


@dataclass
class Bundle:
    stock: object
    now: datetime
    payloads: dict = field(default_factory=dict)   # 维度 → 详情返回（失败为 None）
    errors: dict = field(default_factory=dict)     # 维度 → 失败原因
    aligned: dict | None = None                    # 量价模块已对齐的日线、资金流、板块、指数
    timed_out: set = field(default_factory=set)    # 超时没回来的维度：它们的线程还在跑，会填好各自的缓存


def default_loaders(stock, today: date, aligned: dict | None) -> dict[str, Callable[[], dict]]:
    from app.services import news_detail
    from app.services.fundamental_detail import build_detail as fundamental
    from app.services.finance_detail import build_detail as finance
    from app.services.sector_detail import build_detail as sector
    from app.services.volume_flow import build_detail as volume_flow

    return {
        "volume_flow": lambda: volume_flow(stock, policy.FLOW_WINDOW, aligned=aligned, today=today),
        "news": lambda: news_detail.build_detail(stock),
        "sector": lambda: sector(stock),
        "finance": lambda: finance(stock),
        "fundamental": lambda: fundamental(stock),
    }


def load_aligned(stock) -> dict | None:
    try:
        from app.services.volume_flow.align import load_aligned as load

        return load(stock)
    except Exception:
        return None


def collect(
    stock,
    now: datetime | None = None,
    loaders: dict[str, Callable[[], dict]] | None = None,
    aligned: dict | None = None,
    timeout: float | None = None,
) -> Bundle:
    """loaders 和 aligned 可以注入，回测和测试用同一个入口。"""
    now = now or now_cn()
    bundle = Bundle(stock=stock, now=now)
    if loaders is None:
        bundle.aligned = aligned if aligned is not None else load_aligned(stock)
        loaders = default_loaders(stock, now.date(), bundle.aligned)
    else:
        bundle.aligned = aligned
    pool = ThreadPoolExecutor(max_workers=len(policy.DIMENSIONS))
    futures = {pool.submit(loaders[key]): key for key in policy.DIMENSIONS if key in loaders}
    done, pending = wait(futures, timeout=timeout or policy.COLLECT_TIMEOUT)
    pool.shutdown(wait=False, cancel_futures=True)
    for future in done:
        key = futures[future]
        try:
            bundle.payloads[key] = future.result()
        except Exception as exc:
            bundle.payloads[key] = None
            bundle.errors[key] = f"取数失败: {exc}"
    for future in pending:
        key = futures[future]
        bundle.payloads[key] = None
        bundle.errors[key] = "取数超时"
        bundle.timed_out.add(key)
    for key in policy.DIMENSIONS:
        if key not in bundle.payloads:
            bundle.payloads[key] = None
            bundle.errors.setdefault(key, "没有取数入口")
    return bundle
