"""市场页的两个定时任务：盘前简报和收盘复盘。原来 market_job 里写死的 08:30 / 15:35 线程改成调度器任务。"""

from __future__ import annotations

from datetime import date, datetime, time

from app.scheduler import clock as clocks
from app.scheduler.spec import task

_AT = {"pre": time(8, 30), "close": time(15, 35)}


def _moment(trade_date: str, session: str, now: datetime) -> datetime:
    """补跑的是前一天的任务时，把「现在」换成那天的时刻，快照才落在对的日子。"""
    day = date.fromisoformat(trade_date) if trade_date else now.date()
    return now if day == now.date() else datetime.combine(day, _AT[session], tzinfo=clocks.ZONE)


def _run(ctx, session: str) -> dict:
    from app.services import market_job

    now = clocks.from_ms(ctx.now_ms())
    run = ctx.store.run(ctx.job["run_id"]) or {}
    # 补跑时不再推送：过了时点的简报推出来是噪音
    push = (lambda *_: None) if run.get("trigger") == "catchup" else None
    summary = ctx.step("build", lambda: _brief(market_job.run_session(session, _moment(ctx.trade_date, session, now), push)))
    return {"session": session, "trade_date": ctx.trade_date, **(summary or {})}


def _brief(summary: dict) -> dict:
    """run_session 返回整页数据，太大；任务输出只留结论。"""
    conclusion = (summary or {}).get("conclusion") or {}
    return {"level": conclusion.get("level"), "tilt": conclusion.get("tilt"), "headline": conclusion.get("headline", ""),
            "today": (summary or {}).get("today", "")}


@task("market.pre", "盘前简报", {"kind": "daily", "at": "08:30", "window_min": 20, "days": "trading"},
      queue="data", timeout_s=300, catchup_ttl_min=300, misfire="run_latest", overdue_after_min=90)
def market_pre(ctx):
    return _run(ctx, "pre")


@task("market.close", "收盘复盘", {"kind": "daily", "at": "15:35", "window_min": 20, "days": "trading"},
      queue="data", timeout_s=600, catchup_ttl_min=960, misfire="run_latest", overdue_after_min=120)
def market_close(ctx):
    return _run(ctx, "close")
