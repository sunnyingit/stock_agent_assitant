"""定时任务内核。设计见 docs/scheduler-and-notify-spec.md。

任务在代码里用 @task 注册；planner 每 30 秒 tick 一次，按触发器、交易日历、补跑策略生成 task_run，再展开成 job；
executor 用租约领 job，按队列限并发和限速，失败按类别重试。状态全在 SQLite 里，重启后接着跑。
"""
