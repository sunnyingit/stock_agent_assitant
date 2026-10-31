"""命令行：python -m app.synthesis.cli run 600519.SH [--mode shadow] [--force]
                                      freeze 600519.SH 输出路径   把事实表冻结成回归样本
                                      replay 事实表.json         用冻结的事实表重跑决策层（改提示词前后对比）
                                      settle                    回填战绩
                                      stats [天数]               看命中率
                                      finance-settle            回填财报信号披露后 5、20 个交易日的超额收益
                                      finance-stats [5|20]      按 题 × 状态 看财报信号的平均超额收益和命中率
"""

from __future__ import annotations

import json
import sys

from app.synthesis import policy, worker
from app.synthesis.outcomes import settle
from app.synthesis.service import compute
from app.tools.stock import resolve_stock


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    command, rest = argv[0], argv[1:]
    store = worker.get_store()
    if command in ("run", "freeze"):
        stock = resolve_stock(rest[0]) if rest else None
        if stock is None:
            print("未找到股票")
            return 1
        mode = rest[rest.index("--mode") + 1] if "--mode" in rest else None
        reply = compute(stock, store, mode=mode, force="--force" in rest, timeout=policy.COLLECT_TIMEOUT_BACKGROUND)
        if command == "freeze":
            record = store.latest(stock.ts_code)
            path = rest[1] if len(rest) > 1 and not rest[1].startswith("--") else f"{stock.ts_code}.facts.json"
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(record["data"]["table"], handle, ensure_ascii=False, indent=2)
            print(f"事实表已写入 {path}")
        else:
            print(json.dumps(reply, ensure_ascii=False, indent=2))
        return 0
    if command == "replay":
        from app.synthesis import decider
        from app.synthesis.gateway import Gateway
        from app.synthesis.types import FactTable

        with open(rest[0], encoding="utf-8") as handle:
            table = FactTable.from_dict(json.load(handle))
        result = decider.decide(table, Gateway.from_config(), session="close")
        print(json.dumps({key: result[key] for key in ("source", "reason", "final")}, ensure_ascii=False, indent=2, default=str))
        return 0
    if command == "finance-settle":
        from app.finance_review import outcomes

        print(f"回填 {outcomes.settle(store)} 档")
        return 0
    if command == "finance-stats":
        from app.finance_review import outcomes

        for row in outcomes.stats(store, int(rest[0]) if rest else 5):
            print(row)
        return 0
    if command == "settle":
        print(f"回填 {settle(store)} 条")
        return 0
    if command == "stats":
        horizon = int(rest[0]) if rest else 5
        for row in store.stats(horizon):
            print(row)
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
