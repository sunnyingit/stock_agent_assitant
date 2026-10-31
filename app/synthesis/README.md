# 综合判断（synthesis）

Agent 直接读 5 个维度的结果、直接给出结论；代码只做五件事：准备输入、算数字、校验输出、保证稳定、留档对照。
规则版结论照常计算，只作安全网和对照。设计文档见项目外的《综合判断 Agent 实现思路》。

## 分层

```
producer/（有副作用）          decider/（纯函数 + 模型调用）        levels/（纯代码）
collect → adapters → health    debate（正反方）→ judge（主 Agent×N）   candidates → evaluate → resolve
        → facts（事实表）       → validate → strength → stabilize       defaults（参考表）/ render（模板）
                    ╲                    ╱
                  types.FactTable（唯一契约）
```

- `decider/` 只认识 `FactTable`，不 import `app.services`：线上和回测用同一种输入，可以用冻结的事实表回归。
- `service.compute()` 把各层串起来，并负责规则版回退、合并输出和存档。
- 页面接口只读存档（`api.py`）；重算由 `worker.py` 在后台做，同一只股票 30 秒内的多次触发合并成一次。

## 文件

| 文件 | 作用 |
|---|---|
| `policy.py` | 全部阈值、菜单、词表，带 `POLICY_VERSION` |
| `registry.py` | 事实注册表：编号、维度、单位、展示格式、数字等价写法 |
| `rules.py` | 规则版综合、可信度 |
| `calendar.py` | 交易时段与数据新鲜度（目前按工作日，无节假日表） |
| `gateway.py` | 模型网关：超时、并行、调用记录 |
| `store.py` | SQLite：存档、缓存、任务队列、战绩 |
| `outcomes.py` | 战绩回填 |
| `display.py` | 页面结构与规则版模板文案 |
| `cli.py` | `run` / `freeze` / `replay` / `settle` / `stats` |

## 运行

- `SYNTHESIS_MODE=rule`（默认）只用规则版；`shadow` 后台跑 Agent 并存档、页面仍用规则版；`agent` 页面用 Agent，失败回退规则版。
- `SYNTHESIS_DB` 指定 SQLite 路径，默认 `data/synthesis.db`。
- 接口：`GET /api/v1/stocks/{ts_code}/synthesis[?debug=1]`，`POST .../synthesis/refresh`。
- 模型沿用 `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL`。

## 改动要升版本的地方

- 提示词：`decider/prompts.py` 的 `PROMPT_VERSION`
- 阈值、菜单、词表：`policy.py` 的 `POLICY_VERSION`
- 战绩按这两个版本分开统计
