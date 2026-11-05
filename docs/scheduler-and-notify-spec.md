# 定时任务与通知：架构与数据规范

状态：第 1–4 期已实现（见第 15 节的实现状态和与设计的差异）。范围：定时任务、数据预热缓存、事件与触发、通知投递、离线提醒转发、客户端常驻。
读者：后面要实现或新增定时任务的人。新增任务只需要读第 2、3 节；新增通知类型只需要读第 5、6 节。

## 0. 已定的决策

| 决策 | 结论 |
|---|---|
| 任务放哪 | 默认在**客户端**跑。服务端只做「共享、不含私有数据、取起来贵或受限流」的数据缓存，外加一个最小的**价格提醒离线转发** |
| LLM | 在客户端，用用户自己的密钥；服务端不跑 LLM |
| 使用对象 | 给别人用。私有数据（持仓、理由、提醒规则、分析结果）留在各自客户端 |
| 客户端常驻 | **托盘常驻 + 登录自启**。窗口关闭只是隐藏，调度器继续跑 |
| 事实表缓存 | **预热**：客户端上报关注的股票，服务端取并集，数据就绪后提前算好 |
| 关机也要提醒 | 要。靠服务端的离线转发，见第 8 节 |

## 1. 总体架构

```
┌────────────────────────── 客户端（每个用户一份）──────────────────────────┐
│ Electron 壳（托盘常驻、登录自启、休眠/唤醒事件）                              │
│   └─ 本机后端（Python，profile=client）                                      │
│        调度内核 ─▶ 任务（市场拉取、个股分析、规则评估、体检、结算…）            │
│            │                                                                │
│            ├─ 事实：从服务端拉「产物」，拉不到就自己取                          │
│            ├─ LLM 网关（本机一个出口，密钥在系统钥匙串）                        │
│            └─ 事件 ─▶ 规则 ─▶ 策略 ─▶ 收件箱 + 投递（桌面、用户自己的渠道）      │
└──────────────┬───────────────────────────────────────────────▲──────────────┘
               │ 拉取产物 / 上报订阅 / 同步提醒定义                  │ 事件流(SSE)+确认
┌──────────────▼───────────────────────────────────────────────┴──────────────┐
│ 服务端（常开，profile=server，同一套代码）                                      │
│   调度内核（只领 placement=server 的任务）                                       │
│   · 数据就绪标记   · 产物缓存（行情日线、市场快照、个股事实表）   · 订阅并集预热    │
│   · 离线转发：价格提醒评估 + 外部渠道推送（只存最少信息）                         │
└────────────────────────────────────────────────────────────────────────────┘
```

**放置规则。**每个任务声明 `placement`，默认 `client`。声明为 `server` 的任务必须同时满足并在 `placement_reason` 里写明：

1. `shared`：输出对所有用户一样；
2. `no_private`：输入和输出都不含任何用户的私有数据；
3. `costly`：取数昂贵或受限流（或 `offline`：必须在客户端关机时运行）。

`offline` 目前只有一个：价格提醒转发。

**同一份代码，两种 profile。**`profile=client|server` 决定调度器领哪些任务、暴露哪些接口。开发时两个 profile 可以跑在同一台机器上。

## 2. 通用约定

**时间。**
- 时刻一律用整数毫秒时间戳（UTC），字段名以 `_at` 结尾。
- 交易日用字符串 `YYYY-MM-DD`，字段名 `trade_date` 或 `as_of`，含义是「这份数据属于哪个交易日」，按 `Asia/Shanghai`。
- 一个时刻对应哪个交易日，由交易日历决定，**不要用日期字符串去反推时刻**。
- `session` 取值：`pre`（盘前）、`intraday`（盘中）、`close`（收盘后）、`night`（夜间）。

**ID。**
- 事件、通知、投递、任务实例、规则、设备用 ULID（可按时间排序、客户端可离线生成）。
- 任务本身用稳定的点分名：`market.close`、`stock.analyze`。
- 股票用 `ts_code`（`600036.SH`）。

**命名。**事件和规则类型用 `域.对象.动作`，小写点分，例如 `price.alert.fired`。

**版本与兼容。**
- 所有落库和跨进程的 JSON 都带 `v`（整数）。只增字段不算破坏；删字段或改含义要升 `v`，同时保留旧版本的读取。
- 读到不认识的字段忽略，读到不认识的 `type` 存下来但不处理。

**幂等。**每一个会产生副作用的东西都有幂等键，规则统一：`<域>:<对象>:<周期或序号>`。
- 任务实例：`(task_id, period_key)`；
- 任务单元：`(task_id, unit_key, period_key)`；
- 事件：`dedupe_key`；
- 投递：`(channel_id, notif_id)`。

**错误分类。**所有失败要归入三类，决定重试策略：

| class | 含义 | 处理 |
|---|---|---|
| `not_ready` | 数据还没出来 | 不消耗尝试次数，按就绪门的间隔重试，直到截止 |
| `transient` | 超时、限流、网络 | 指数退避，最多 `max_attempts` 次 |
| `permanent` | 代码不存在、参数错、数据结构变了 | 直接进 `dead`，记错误，聚合告警 |

## 3. 调度数据规范

任务定义写在代码里（注册表），数据库只存用户的覆盖项（启停、改时间）。

### 3.1 任务定义（代码里声明）

```json
{
  "v": 1,
  "task_id": "stock.nightly",
  "title": "每晚个股分析",
  "placement": "client",
  "trigger": { "...见 3.2..." },
  "after": [{ "task": "market.close", "mode": "soft", "timeout_min": 30 }],
  "gates": ["daily_bars", "moneyflow"],
  "fanout": "subjects.watch_union",
  "queue": "llm",
  "priority": 50,
  "timeout_s": 900,
  "retry": { "max_attempts": 3, "backoff_s": [120, 600, 1800] },
  "budget": { "resource": "llm", "per_run": { "calls": 400, "tokens": 2000000 }, "per_unit": { "tokens": 60000 } },
  "skip_if_fresh": "facts_hash",
  "misfire": "run_latest",
  "catchup_ttl_min": 720
}
```

字段含义：
- `after`：依赖。`soft` 表示依赖没跑完最多等 `timeout_min` 就继续；`hard` 表示必须成功。
- `gates`：数据就绪门，见 3.3。所有门都就绪才开始。
- `fanout`：怎么从一个任务实例展开成多个单元。例如 `subjects.watch_union` 展开成「持仓 ∪ 想买 ∪ 自选」里的每只股票；`none` 表示不展开。
- `queue`：决定并发和限速，见 3.6。
- `skip_if_fresh`：单元开始前先判断产物是否已是最新（例如事实表哈希没变），是则标 `skipped`，不消耗预算。
- `misfire`：错过触发时刻后怎么办：`run_latest`（只补最近一次）、`run_all`（逐个补）、`skip`（作废）。
- `catchup_ttl_min`：补跑的有效期。超过就标 `expired`，不再补。

### 3.2 触发器（可判别联合）

```json
{ "kind": "daily",            "at": "21:00", "window_min": 210, "days": "trading" }
{ "kind": "session_interval", "every_min": 3,  "sessions": ["am", "pm"] }
{ "kind": "cron",             "expr": "40 8 * * 1", "days": "trading" }
{ "kind": "event",            "topic": "holding.changed", "debounce_s": 30, "max_wait_s": 120 }
{ "kind": "data_ready",       "gate": "fund_share.d-1", "deadline": "09:20" }
{ "kind": "manual" }
```

- `days`：`trading`（交易日）、`any`、`weekday`。非交易日不触发，`session_interval` 只在交易时段内。
- `window_min`：`daily` 不是准点，而是「从 `at` 起这个窗口内的任一时刻都算在时间内」。客户端休眠醒来在窗口内就补跑；窗口过了，按 `misfire` 处理。
- `event` 触发按 `coalesce_key` 合并：同一个键在 `debounce_s` 内重复触发只算一次，最长等 `max_wait_s`。这取代现在 synthesis worker 里「30 秒合并」的做法。
- `session_interval` 的交易时段由日历服务给出：上午 09:30–11:30，下午 13:00–15:00，中间不触发。

### 3.3 数据就绪门

任务通过 `gates` 声明依赖的数据，调度器不关心具体取数，只问「就绪了吗」。

| 门 | 含义 | 典型就绪时间（以实际探测为准） |
|---|---|---|
| `daily_bars` | 当日日线已发布 | 盘后 |
| `limit_list` | 当日涨跌停名单 | 盘后 |
| `moneyflow` | 当日个股资金流 | 晚间 |
| `fund_share.d-1` | 前一交易日的 ETF 份额 | 下一个交易日早上 |
| `margin.d-1` | 前一交易日融资融券 | 下一个交易日早上 |

就绪标记由服务端发布，客户端先问服务端，问不到再自己探测：

```
GET /v1/ready?date=2026-11-05
→ { "v": 1, "date": "2026-11-05",
    "gates": {
      "daily_bars":  { "ready": true,  "at": 1793863200000 },
      "moneyflow":   { "ready": false, "checked_at": 1793870400000 }
    } }
```

未就绪时任务单元进入 `not_ready`：不计失败，退避重试（间隔 5 分钟，逐步拉到 20 分钟），到 `deadline` 还没好就标 `expired` 并发 `task.overdue` 事件。

### 3.3.1 扇出提供者

`fanout` 引用注册表里的一个提供者，函数签名是「上下文 → 单元列表」，每个单元有 `unit_key`、`priority`、`input`：

| 提供者 | 位置 | 展开成 |
|---|---|---|
| `none` | 任意 | 一个单元，`unit_key='_'` |
| `subjects.watch_union` | 客户端 | 持仓 ∪ 想买 ∪ 自选的每只股票；持仓优先级最高，想买次之，自选最低 |
| `subscriptions.union` | 服务端 | 所有设备订阅的股票并集；有设备持有的优先，其次按订阅设备数 |
| `rules.by_subject` | 客户端 | 有启用规则的每个对象，用于规则评估 |
| `devices.active` | 服务端 | 近 30 天有心跳的设备，用于离线转发的对账 |

### 3.3.2 `stock.nightly` 的步骤

每只股票一个单元，内部步骤（对应 `job_step`）：

1. `collect`：拉取 `stock.facts`（服务端缓存），没有就本机现算；记下 `facts_hash`；
2. `skip_check`：本机上这个交易日已有基于同一 `facts_hash` 的判断 → 后面的 LLM 步骤全部标 `skipped`；
3. `review.news` / `review.sector` / `review.volume_flow` / `review.fundamental` / `review.finance`：各维度分析师，彼此并行；
4. `bull`、`bear`：正反方，依赖第 1 步，可与第 3 步并行；
5. `chief`：主分析师，依赖 3、4；
6. `record`：存判断和战绩原始记录；
7. `diff`：和上一个交易日的判断比较，产出 `judgment.changed` 事件。

重试从失败的那一步继续，已成功的步骤（以及它们消耗的 token）不再重复。

### 3.4 表结构（SQLite，写法兼容 Postgres）

```sql
-- 一次周期性实例。(task_id, period_key) 唯一，保证同一周期只规划一次。
-- period_key 约定：daily/cron → 'trading_day:YYYY-MM-DD'；session_interval → 'min:YYYY-MM-DDTHH:MM'；
--                event → 'event:<coalesce_key>:<合并窗口起点 ms>'；manual → 'manual:<ulid>'
CREATE TABLE task_run (
  run_id        TEXT PRIMARY KEY,          -- ULID
  task_id       TEXT NOT NULL,
  period_key    TEXT NOT NULL,             -- 'trading_day:2026-11-05' / 'min:2026-11-05T14:30' / 'manual:<ulid>'
  trigger       TEXT NOT NULL,             -- schedule | catchup | event | manual
  scheduled_for INTEGER NOT NULL,          -- 应当触发的时刻 ms
  state         TEXT NOT NULL,             -- planned | waiting | running | succeeded | partial | failed | skipped | expired
  counts        TEXT NOT NULL DEFAULT '{}',-- {"jobs":40,"succeeded":37,"failed":1,"skipped":2}
  started_at    INTEGER, finished_at INTEGER,
  note          TEXT NOT NULL DEFAULT '',
  config        TEXT NOT NULL DEFAULT '{}',-- 这次运行实际生效的配置快照（触发器、参数、预算），事后能还原当时为什么这样跑
  code_version  TEXT NOT NULL DEFAULT '',  -- 任务定义和处理函数的版本（构建号），用来解释「为什么结果和上周不一样」
  UNIQUE (task_id, period_key)
);

-- 一个最小执行单元。(task_id, unit_key, period_key) 唯一，是幂等的根。
CREATE TABLE job (
  job_id        TEXT PRIMARY KEY,
  run_id        TEXT NOT NULL REFERENCES task_run(run_id),
  task_id       TEXT NOT NULL,
  unit_key      TEXT NOT NULL,             -- '600036.SH' 或 '_'（无扇出）
  period_key    TEXT NOT NULL,
  queue         TEXT NOT NULL,
  state         TEXT NOT NULL,             -- pending | leased | running | succeeded | retrying | not_ready | dead | skipped | cancelled | expired
  priority      INTEGER NOT NULL DEFAULT 50,
  attempt       INTEGER NOT NULL DEFAULT 0,
  max_attempts  INTEGER NOT NULL DEFAULT 3,
  due_at        INTEGER NOT NULL,          -- 最早可以领取的时刻
  expire_at     INTEGER,                   -- 过了就不再执行（盘中提醒这类）
  lease_owner   TEXT, lease_until INTEGER, -- 执行器租约；过期未续则回收
  coalesce_key  TEXT,                      -- 事件触发的合并键
  input         TEXT NOT NULL DEFAULT '{}',
  output        TEXT NOT NULL DEFAULT '{}',-- 只放摘要和产物引用，不放大对象
  error         TEXT,                      -- {"class":"transient","code":"RATE_LIMIT","message":"..."}
  created_at    INTEGER NOT NULL, updated_at INTEGER NOT NULL,
  UNIQUE (task_id, unit_key, period_key)
);
CREATE INDEX job_claim ON job (queue, state, due_at, priority);

-- 单元内部的步骤检查点：失败重试时从断点接着做，不重复调用 LLM。
CREATE TABLE job_step (
  job_id TEXT NOT NULL REFERENCES job(job_id),
  step   TEXT NOT NULL,                    -- 'collect' | 'review.news' | 'bull' | 'bear' | 'chief' | 'diff'
  state  TEXT NOT NULL,                    -- pending | running | succeeded | failed | skipped
  attempt INTEGER NOT NULL DEFAULT 0,
  output_ref TEXT,                         -- 指向 runs / 产物表的主键，不内联
  tokens INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (job_id, step)
);

-- 每一次尝试单独留痕：重试几次、每次为什么失败、花了多久和多少 token。
CREATE TABLE job_attempt (
  job_id TEXT NOT NULL REFERENCES job(job_id),
  attempt INTEGER NOT NULL,
  lease_owner TEXT,
  started_at INTEGER NOT NULL, ended_at INTEGER,
  state TEXT NOT NULL,                     -- running | succeeded | failed | not_ready | abandoned（租约过期被回收）
  error TEXT,                              -- 同 job.error 的结构
  tokens INTEGER NOT NULL DEFAULT 0, calls INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (job_id, attempt)
);

-- 任务产出的结果。小的内联，大的落文件；job.output 和 job_step.output_ref 只放引用。
CREATE TABLE result (
  ref          TEXT PRIMARY KEY,           -- 'res:<ulid>'
  owner_job    TEXT, step TEXT,
  kind         TEXT NOT NULL,              -- json | text | markdown | blob
  content_type TEXT NOT NULL,
  size         INTEGER NOT NULL, sha256 TEXT NOT NULL,
  inline       TEXT,                       -- 不超过 8 KB 直接放这里
  path         TEXT,                       -- 更大的放文件：data/results/<yyyy>/<mm>/<sha256>
  created_at   INTEGER NOT NULL, expires_at INTEGER
);

-- LLM 调用审计：谁、哪一步、用哪个模型、花多少、结果是否通过校验。默认不存提示词和回复原文，只存哈希。
CREATE TABLE llm_call (
  call_id TEXT PRIMARY KEY, job_id TEXT, step TEXT,
  model TEXT NOT NULL, prompt_version TEXT NOT NULL,
  prompt_sha256 TEXT NOT NULL, response_sha256 TEXT,
  tokens_in INTEGER, tokens_out INTEGER, latency_ms INTEGER,
  outcome TEXT NOT NULL,                   -- ok | invalid | timeout | rate_limit | error
  content_ref TEXT,                        -- 只有开启调试时才有：指向 result 里的原文
  created_at INTEGER NOT NULL
);

-- 预算流水：按资源、按窗口累计，调度前检查、执行后记账。
CREATE TABLE budget_ledger (
  resource TEXT NOT NULL,                  -- 'llm' | 'tushare' | 'notify:wecom'
  window   TEXT NOT NULL,                  -- '2026-11-05'（日）或 '2026-11-05T21'（小时）
  used     TEXT NOT NULL DEFAULT '{}',     -- {"calls":120,"tokens":812345}
  PRIMARY KEY (resource, window)
);

-- 用户对任务的覆盖。
CREATE TABLE task_override (
  task_id TEXT PRIMARY KEY, enabled INTEGER, trigger TEXT, params TEXT, updated_at INTEGER NOT NULL
);

-- 调度器自身状态：领导者租约、最近一次 tick。
CREATE TABLE sched_state (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at INTEGER NOT NULL);
```

### 3.5 状态机

```
job:  pending ─领取→ leased ─开始→ running ─成功→ succeeded
        │                     │      └失败(transient)→ retrying ─到点→ pending
        │                     │      └数据未好→ not_ready ─到点→ pending
        │                     └失败(permanent) 或 次数用尽→ dead
        └ expire_at 已过 → expired        └ skip_if_fresh 命中 → skipped
租约：running 期间每 lease/3 续约；lease_until 已过且状态仍是 leased/running → 回收成 pending（attempt 不加）

task_run:  planned → waiting(等 after/gates) → running → succeeded | partial(部分单元 dead) | failed | skipped | expired
```

### 3.6 队列、并发与限速

| 队列 | 并发 | 限速 | 典型任务 |
|---|---|---|---|
| `llm` | 2 | 每分钟调用数、每日 token（来自 `budget_ledger`） | 个股分析各步 |
| `data` | 4 | tushare 每分钟请求数 | 取数、事实表 |
| `notify` | 4 | 每渠道每分钟 / 每日条数 | 投递 |
| `light` | 8 | 无 | 规则评估、对账 |

- 限速用令牌桶，桶满前不领取，**不是领取后再失败重试**。
- 预算用尽的处理：优先级低的单元标 `skipped`（`note=budget`），当晚汇总进「今晚跑了几只、几只因预算跳过」的摘要事件。

### 3.7 调度器的一次 tick（每 30 秒）

1. 取得领导者租约（客户端只有一个调度器，仍走同一套逻辑；服务端多实例时靠它选主）。
2. 对每个启用的任务算「应触发而未规划」的周期：按 `trigger`、交易日历、`misfire`、`catchup_ttl_min`，插入 `task_run`（唯一键保证重复 tick 无害）。
3. 对 `waiting` 的 run 检查 `after` 和 `gates`，满足就按 `fanout` 展开成 `job`。
4. 回收过期租约，把 `retrying` / `not_ready` 到点的转回 `pending`。
5. 更新 `sched_state.last_tick`（看门狗用它判断调度器是不是活着）。

执行器独立循环领任务，不在 tick 里执行。

## 4. 共享产物与预热

产物是「某个对象、某一天、某个口径」的带版本数据，由服务端生成并缓存，客户端只读。**产物里不放 LLM 输出，也不放任何用户信息。**

### 4.1 产物信封

```json
{
  "v": 1,
  "kind": "stock.facts",
  "subject": "600036.SH",
  "as_of": "2026-11-05",
  "session": "close",
  "schema": "facts@3",
  "policy_version": "p12",
  "facts_hash": "sha256:9f2c…",
  "producer": { "host": "server", "build": "a1b2c3d" },
  "created_at": 1793870000000,
  "expires_at": 1796462000000,
  "payload": { }
}
```

- 唯一键：`(kind, subject, as_of, session, schema)`。
- `facts_hash` 是 `payload` 规范化后的哈希，同时用作 `ETag`。客户端用 `If-None-Match` 拉取，没变化返回 304。
- `policy_version` 沿用现有综合判断的版本号，保证「事实表 → 判断」的可回溯。

### 4.2 产物种类

| kind | subject | payload |
|---|---|---|
| `stock.facts` | 股票 | 现有 `FactTable` 中**确定性**的部分：`facts`、`unavailable`、`events`、`candidates`、`rule`、`context`。**不含** `headlines`（可能来自 LLM 体检）和任何模型输出 |
| `stock.bars` | 股票 | 日线序列（250 日） |
| `market.snapshot` | `market` | 市场页的各切片和结论（规则版，无 LLM） |
| `market.sectors` | `market` | 板块行（钱、价、热度、状态） |
| `market.team` | `market` | 国家队判断和历史 |
| `stock.news` | 股票 | 当天已采集并**规则分类**的公告和新闻条目（见 6.6），不含 LLM 输出 |
| `calendar.trading` | `market` | 交易日历（年） |

`stock.facts` 是预热的核心：把「取五个维度数据并整理成事实表」这一步——最贵、最受限流——集中到服务端做一次，所有用户共用。客户端的 LLM 分析从这份事实表起步。

### 4.3 接口

```
GET  /v1/artifacts/{kind}/{subject}?as_of=2026-11-05&session=close   → 信封（支持 ETag / 304）
POST /v1/artifacts:batchGet   { "items": [{"kind":"stock.facts","subject":"600036.SH","as_of":"…"}] }
GET  /v1/manifest?kind=stock.facts&as_of=2026-11-05                  → 当天已有的 subject 与 facts_hash 清单
GET  /v1/ready?date=2026-11-05                                       → 数据就绪标记（3.3）
```

客户端的拉取顺序：先看 `manifest`，只拉自己需要、且本地哈希不同的；服务端没有的，客户端按自己的取数能力现算，并在本地标记 `producer.host=client`。

### 4.4 订阅与预热

客户端上报自己关注的股票，服务端取并集提前算。

```
PUT /v1/subscriptions
{ "v": 1, "device_id": "01J…", "rev": 7,
  "codes": [ { "ts_code": "600036.SH", "tier": "hold" }, { "ts_code": "600519.SH", "tier": "other" } ] }
→ { "rev": 7, "accepted": 42 }
```

- `tier` 只有两档：`hold`（持仓）和 `other`（想买、自选）。服务端只用它排优先级，不保存更多语义。
- 服务端维护 `subscription(device_id, ts_code, tier, updated_at)`，并集 = 所有设备的 `ts_code` 去重。
- 预热优先级：`(有设备持有, 订阅设备数, 最近被请求次数)`，先算持仓、再算热门。
- 设备 30 天没有心跳，其订阅作废。没有任何订阅的股票不再预热，也不再保留。
- 预热任务 `stock.facts.warm`（`placement=server`，`shared`、`no_private`、`costly`）：数据就绪后按并集展开，`skip_if_fresh=facts_hash`，每只产物保留 30 天。
- **未预热的股票兜底**：客户端请求时服务端按需生成并缓存，第一次会慢，之后命中。

### 4.5 隐私

服务端会知道「哪些设备关注了哪些股票」，但不知道持仓数量、成本、理由。设备用随机 `device_id`，不关联账号时服务端无从关联到人；一旦绑定账号，要在隐私说明里写明。

## 5. 事件规范

事件是**已经发生的事实**，只描述发生了什么，不决定发给谁、怎么发。所有产出方（任务、评估器、转发服务）都只写事件。

### 5.1 信封

```json
{
  "v": 1,
  "event_id": "01JC…",
  "type": "price.alert.fired",
  "subject": { "kind": "stock", "id": "600036.SH" },
  "occurred_at": 1793865600000,
  "trade_date": "2026-11-05",
  "severity": "urgent",
  "dedupe_key": "price.alert.fired:01JB…:3",
  "source": { "task_id": "alerts.intraday", "run_id": "01JC…", "job_id": "01JC…", "host": "client" },
  "correlation_id": "01JC…",
  "ttl_s": 86400,
  "payload": { }
}
```

- `subject.kind`：`stock`、`market`、`portfolio`、`system`。
- `severity`：`info` 只进收件箱；`normal` 进收件箱并可摘要；`urgent` 立即投递。
- `dedupe_key` 唯一；写入遇到重复直接忽略，这是整条链路防重复的第一道。
- `ttl_s`：过了就不再投递（盘中提醒过时没意义），收件箱仍可保留。
- `correlation_id`：把同一次分析产生的多个事件串起来。

### 5.2 事件类型注册表

注册表是代码里的一张表：类型、默认严重度、`payload` 的 JSON Schema、标题和正文模板、默认链接。新增通知类型就是往里加一行。

| type | 默认严重度 | payload 关键字段 | dedupe_key 规则 |
|---|---|---|---|
| `price.alert.fired` | urgent | `alert_id, ts_code, name, side, trigger_price, observed_price, observed_at, confirm, source` | `{alert_id}:{fire_seq}` |
| `price.move` | normal | `ts_code, name, metric(day_pct/speed), pct, window_min, observed_price, prev_close, observed_at` | `{rule_id}:{trade_date}:{fire_seq}` |
| `limit.hit` | normal | `ts_code, name, kind(limit_up/limit_down/limit_open), limit_pct, times, close, trade_date` | `{ts_code}:{trade_date}:{kind}` |
| `price.extreme` | info | `ts_code, name, kind(high/low), window_days(20/60/250), close, trade_date` | `{ts_code}:{trade_date}:{kind}:{window_days}` |
| `announcement.published` | urgent / normal / info（按 `importance`） | `ts_code, name, item_id, title, url, source, source_grade, published_at, category, topic, importance(P0/P1/P2), direction_hint, matched[]` | `ann:{item_id}` |
| `news.item` | info | `ts_code, name, item_id, title, url, source, source_grade, published_at, category` | `news:{item_id}` |
| `news.impact_assessed` | info | `item_id, ts_code, direction, magnitude, basis, scope`（客户端用 LLM 复判后产出，补充而不是重复通知） | `impact:{item_id}` |
| `judgment.changed` | normal | `ts_code, name, trade_date, from{lean,level}, to{lean,level}, headline, flip_hint` | `{ts_code}:{trade_date}:{to.lean}:{to.level}` |
| `reason.status_changed` | normal | `ts_code, name, from, to, line` | `{ts_code}:{trade_date}:{to}` |
| `wish.condition_met` | normal | `wish_id, ts_code, name, met[], auto, status` | `{wish_id}:{trade_date}` |
| `flow.streak` | normal | `ts_code, name, direction(in/out), days` | `{ts_code}:{direction}:{days}:{trade_date}` |
| `calendar.upcoming` | normal | `ts_code, name, kind(disclosure/unlock), date, text` | `{ts_code}:{kind}:{date}` |
| `outcome.settled` | info | `ts_code, name, entry_date, horizon, hit, ret` | `{ts_code}:{entry_date}:{horizon}` |
| `market.level_changed` | normal | `from, to, headline` | `{trade_date}:{to}` |
| `market.team_changed` | normal | `from, to, summary` | `{trade_date}:{to}` |
| `market.sector_phase_changed` | info | `sector, from, to, why` | `{trade_date}:{sector}:{to}` |
| `market.brief` | normal | `session(pre/close), level, tilt, action, changed[]` | `{trade_date}:{session}` |
| `portfolio.checkup_changed` | normal | `attention, flags[], compare` | `{trade_date}` |
| `task.failed` | normal | `task_id, run_id, dead, total, first_error` | `{run_id}` |
| `task.overdue` | urgent | `task_id, expected_by, state` | `{task_id}:{period_key}` |
| `digest.summary` | normal | `period, items[], counts` | `digest:{period}` |

### 5.3 示例

```json
{
  "v": 1, "event_id": "01JC7K…", "type": "judgment.changed",
  "subject": { "kind": "stock", "id": "002050.SZ" },
  "occurred_at": 1793871300000, "trade_date": "2026-11-05", "severity": "normal",
  "dedupe_key": "002050.SZ:2026-11-05:unclear:0",
  "source": { "task_id": "stock.nightly", "run_id": "01JC7A…", "job_id": "01JC7B…", "host": "client" },
  "correlation_id": "01JC7A…", "ttl_s": 604800,
  "payload": {
    "ts_code": "002050.SZ", "name": "三花智控", "trade_date": "2026-11-05",
    "from": { "lean": "bullish", "level": 2 }, "to": { "lean": "unclear", "level": 0 },
    "headline": "资金流转为流出，新闻没有新的催化", "flip_hint": "重新转偏多还差 2 条"
  }
}
```

### 5.4 存储

```sql
CREATE TABLE event (
  event_id TEXT PRIMARY KEY,
  type TEXT NOT NULL,
  subject_kind TEXT NOT NULL, subject_id TEXT NOT NULL,
  occurred_at INTEGER NOT NULL, trade_date TEXT,
  severity TEXT NOT NULL,
  dedupe_key TEXT NOT NULL UNIQUE,
  source TEXT NOT NULL DEFAULT '{}',
  correlation_id TEXT, ttl_s INTEGER,
  schema_v INTEGER NOT NULL DEFAULT 1,
  payload TEXT NOT NULL
);
CREATE INDEX event_by_subject ON event (subject_kind, subject_id, occurred_at DESC);
CREATE INDEX event_by_type ON event (type, occurred_at DESC);
```

## 6. 规则与触发规范

规则是**用户（或系统）声明「什么情况要通知我」**。评估器读数据、比对规则、产出事件。

### 6.1 规则

```sql
CREATE TABLE rule (
  rule_id    TEXT PRIMARY KEY,
  type       TEXT NOT NULL,                -- 见 6.3，和事件类型对应
  subject_kind TEXT NOT NULL, subject_id TEXT NOT NULL,   -- 'stock'+'600036.SH' / 'market'+'market' / 'portfolio'+'*'
  params     TEXT NOT NULL DEFAULT '{}',
  mode       TEXT NOT NULL DEFAULT 'edge', -- edge 边沿 | stream 每条新数据发一次（公告、新闻）| daily_once 每交易日最多一次 | level 每次评估成立都发（极少用）
  rearm      TEXT NOT NULL DEFAULT '{"kind":"hysteresis","pct":1.0}',
  cooldown_s INTEGER NOT NULL DEFAULT 0,
  routing    TEXT NOT NULL DEFAULT '{}',   -- {"severity":"urgent","channels":["ch1"]}，缺省走全局策略
  origin     TEXT NOT NULL DEFAULT 'user', -- user | reason_invalid | wish | system
  origin_ref TEXT,                         -- 来源对象，如 wish_id；来源删除则规则跟着删
  enabled    INTEGER NOT NULL DEFAULT 1,
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
  v          INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE rule_state (
  rule_id TEXT PRIMARY KEY REFERENCES rule(rule_id),
  state TEXT NOT NULL DEFAULT 'armed',     -- armed | fired | suspended
  last_value TEXT,                         -- 最近一次观测值（用于回差判断和审计）
  last_eval_at INTEGER,
  fired_at INTEGER, rearm_at INTEGER,
  fire_seq INTEGER NOT NULL DEFAULT 0,     -- 第几次触发，进入事件 dedupe_key
  cursor TEXT                              -- stream 规则的水位：已处理到哪条数据（时间加 item_id），重启后不重复、不漏
);
```

### 6.2 边沿触发规范

评估器对每条规则每次拿到一个**观测**，调用统一的转移函数：

```
observe(rule, state, obs) -> (new_state, [event])
  armed  + 条件成立            → fired，fire_seq+1，产出事件
  fired  + 条件仍成立          → 不产出
  fired  + 满足 rearm 条件     → armed
  suspended                    → 不评估
```

`rearm`：
- `{"kind":"hysteresis","pct":1.0}`：价格离开触发价至少 1% 才重新待命，防止在线上来回抖动；
- `{"kind":"next_day"}`：下一个交易日自动重新待命；
- `{"kind":"manual"}`：用户手动重置；
- `{"kind":"never"}`：一次性。

`cooldown_s`：重新待命后，在冷却内再次成立也不产出。

### 6.3 规则类型与评估器

每个评估器在代码里声明：输入数据、评估节奏（由哪个任务调用）、放在哪（client / server）。

| 规则类型 | params | 输入 | 评估任务与节奏 | 位置 |
|---|---|---|---|---|
| `price_alert` | `{side:down\|up, price, confirm:touch\|close, note}` | 行情 / 收盘价 | `alerts.intraday`（交易时段每 3 分钟）；`confirm=close` 在收盘就绪后 | 同步到服务端后以服务端为准；客户端在线评估为降级 |
| `price_move` | `{metric:day_pct\|speed, threshold, window_min?, direction:up\|down\|both}` | 行情（保留最近 30 分钟观测） | `alerts.intraday`（交易时段每 3 分钟） | client（以后可并入离线转发） |
| `limit_hit` | `{kinds:[limit_up,limit_down,limit_open]}` | 当日涨跌停名单 | `alerts.close`（`limit_list` 就绪后） | client |
| `price_extreme` | `{window_days:[20,60,250], kinds:[high,low]}` | 日线 | `alerts.close` | client |
| `announcement` | `{min_importance, categories?, topics?, keywords_any?, keywords_none?}` | `stock.news` 产物 | `news.watch`（拉取新产物后立即评估） | client |
| `news_item` | `{categories?, min_grade, keywords_any?, keywords_none?, digest_only:true}` | `stock.news` 产物 | 同上，默认只进摘要 | client |
| `judgment_change` | `{min_level_delta?}` | 判断历史 | `stock.nightly` 单元的最后一步 | client |
| `reason_status` | `{}` | 买入理由、行情、板块、AI | 同上 | client |
| `wish_condition` | `{wish_id}` | 想买计划、行情、板块、资金流 | `alerts.close`、`stock.nightly` 之后 | client |
| `flow_streak` | `{direction, days}` | 个股资金流 | `alerts.close` | client |
| `calendar_upcoming` | `{kinds, lead_days}` | 披露日、解禁 | `alerts.daily`（盘前） | client |
| `market_change` | `{kinds[]}` | 市场快照 | `market.pre` / `market.close` 之后 | client |
| `outcome_settled` | `{}` | 战绩回填 | `settle.nightly` 之后 | client |
| `task_overdue` | `{task_ids[]}` | 任务运行记录 | `watchdog` | 所在宿主 |

现有「理由不成立」的价格提醒（`ALERT_TAG`）在新规范里是 `origin=reason_invalid` 的 `price_alert` 规则；想买计划里的价格条件是 `origin=wish`，随计划关闭自动删除。

### 6.4 价格提醒细则

**观测对象：**

```json
{ "ts_code": "600036.SH", "price": 36.42, "prev_close": 36.9, "observed_at": 1793865600000,
  "source": "tencent", "suspended": false }
```

**评估规则：**
- `touch`：`side=down` 时 `price <= trigger`，`up` 时 `price >= trigger`。只在交易时段评估。
- `close`：用**官方收盘价**（日线，不用最后一笔行情），在 `daily_bars` 就绪后评估一次。
- 观测陈旧（`now - observed_at > 120s`）、停牌、取价失败：**不评估，也不当作未触发**，状态保持不变，记 `last_eval_at` 不更新。连续取不到行情超过 10 分钟，产出 `task.failed`（系统事件）。
- 行情价格相对上一次观测跳变超过 20% 且非除权日：当作异常观测丢弃一次，连续两次一致才采信。
- 数据源有主备顺序，配置化；切换到备用源时，在事件 `source` 里体现。

### 6.5 价格类规则

「股价变化」不只是到价。四类，评估节奏和数据都不同：

| 规则 | 含义 | 观测与算法 | 默认 `rearm` |
|---|---|---|---|
| `price_alert` | 到某个价位 | 见 6.4 | 回差 1% |
| `price_move`（`day_pct`） | 相对昨收涨跌超过 X%，例如 ±5%、±7% | `(price - prev_close) / prev_close`，只在交易时段评估；停牌不评估 | 下一交易日 |
| `price_move`（`speed`） | N 分钟内涨跌超过 X%（快速拉升、跳水），例如 15 分钟 ±3% | 评估器保留每只股票最近 30 分钟的观测环形缓冲；取窗口起点的价格比较；观测稀疏（少于 3 个点）时不评估 | 冷却 30 分钟 |
| `limit_hit` | 涨停、跌停、炸板 | 收盘后用当日涨跌停名单（比盘中价格更准）：`limit_up`/`limit_down` 直接取，`limit_open` 取开板次数大于 0 且收盘未封；涨跌停幅度按板块（主板 10%、创业板和科创板 20%、北交所 30%），名单里没有 ST | 每交易日一次 |
| `price_extreme` | 创 20 / 60 / 250 日新高或新低 | 收盘后，收盘价与前 N 日收盘比较 | 每交易日一次 |

**默认不打扰：**
- 新增持仓时，只自动生成 `price_move(day_pct, ±7%)` 和 `limit_hit` 两条，用户可关；自选和想买不自动生成，由用户自己添加；
- `price.extreme` 默认只进摘要（`info`），不会单独弹出；
- 同一只股票同一天的 `price.move` 最多两条（一条上涨方向、一条下跌方向），超出折叠进当天摘要。

**为什么盘中价格类规则暂不进离线转发：**转发只做「到价」这一件事，保持服务端最小；`price_move` 在客户端在线时评估，客户端离线时不会发。以后要补，只需要在 `relay_alert` 里加 `kind` 和 `params`，协议不变。

### 6.6 公告与新闻规则

公告和新闻是**数据流**，不是「条件成立或不成立」，所以用 `stream` 模式：每条**新**数据最多触发一次，靠 `item_id` 去重，靠 `rule_state.cursor` 记水位。

**1. 采集（服务端，共享，不含私有数据，不用 LLM）。**任务 `news.collect`（`placement=server`，`shared`、`no_private`、`costly`），对象是订阅并集里的股票：
- 公告（来源等级 A：交易所、巨潮、东方财富公告）：每 10 分钟，07:00–22:00；晚间 17:00–22:00 是公告高峰；
- 个股新闻：每 30 分钟，沿用现有的东方财富个股新闻和搜索；
- 每条生成 `item_id = sha1(source | url | 规范化标题 | 发布时间)`；同一天、标题相似度高的转载并入公告（沿用现有「窗口外的媒体稿只是公告转载就并进公告」的规则），保留最高等级来源；
- 结果写成 `stock.news` 产物（追加式，按股票、交易日），并更新 `manifest`，客户端可以按水位只拉新增部分。

**2. 规则分类（采集时做，纯规则，不调模型）。**每条公告得到 `category`、`topic`、`importance`、`direction_hint`：

| importance | 典型 topic（标题关键词） | 默认严重度 | category（沿用现有新闻事件分类） |
|---|---|---|---|
| **P0 重大** | 立案、处罚、监管函、退市风险、停牌、复牌、重大诉讼、被问询（交易所问询函）、业绩预亏、控制权变更、重大重组 | `urgent` | 风险与治理、资本 |
| **P1 重要** | 业绩预告和快报、定期报告披露、回购、增持、减持、重大合同、中标、并购、再融资、分红送转、股权激励 | `normal` | 业绩、资本、经营 |
| **P2 常规** | 董事会决议通知、股东大会通知、独立董事意见、律师意见、日常关联交易 | `info` | 经营 |

`direction_hint` 只给规则能判断的：回购、增持、业绩预增、重大合同 → `利好`；减持、业绩预亏预减、立案、处罚、问询 → `利空`；其余 `中性`。**这只是提示，不是结论**，页面上要写成「标题提到 X」，不写「利好」。真正的方向和量级，仍由现有新闻分析师（客户端 LLM）在夜间分析里复判，完成后产出 `news.impact_assessed`，补充到同一条通知，不会再弹一次。

**3. 客户端评估。**拉到新产物后，对每条规则用 `params` 匹配新条目：

```json
{
  "type": "announcement", "mode": "stream",
  "subject": { "kind": "stock", "id": "601689.SH" },
  "params": { "min_importance": "P1", "categories": ["业绩", "资本", "风险与治理"],
              "keywords_any": [], "keywords_none": ["董事会决议通知"] }
}
```

**4. 默认规则（按关注程度分层）：**

| 关注层 | 公告 | 新闻 |
|---|---|---|
| 持仓 | P0 立即（`urgent`），P1 单条（`normal`），P2 只进收件箱 | 默认不发，进摘要的「新闻线索 N 条」 |
| 想买 | P0、P1 | 同上 |
| 自选 | P0 | 同上 |

**5. 降噪：**
- 同一只股票 10 分钟内的多条公告折叠成一条（标题列出前三条，其余写「还有 N 条」）；
- 同一 `item_id` 绝不重复；跨来源重复按 3 里的规则合并；
- 每只股票每天 `normal` 级公告通知最多 5 条，超出的进当天摘要；
- P0 在静默时段对**持仓**仍然立即发（用户可在渠道设置里改成「静默时段不发」）。

**6. 通知文案：**标题 = 股票名 + 公告类别（如「拓普集团 · 减持计划」），正文 = 原标题 + 来源和发布时间 + 规则识别到的关键词；链接直达持仓详情或诊断页，带 `item_id`，页面可以滚动到对应条目。

**7. 时效：**公告类事件 `ttl_s` = 3 天；新闻 7 天；过期后不再投递，收件箱仍保留。

## 7. 通知规范

### 7.1 管线

```
event ──▶ 策略 policy.decide ──▶ notification（收件箱，每个事件至多一条）
                │
                └─▶ delivery（每个渠道一条）──▶ 渠道适配器 ──▶ 外部/桌面
```

**策略输入**：事件、用户的通知设置（全局开关、渠道、静默时段、摘要时间）、规则的 `routing` 覆盖。**策略输出**：

```json
{ "inbox": true,
  "deliver": [{ "channel_id": "ch_desktop", "when": "now" }, { "channel_id": "ch_wecom", "when": "after_quiet" }],
  "digest": false, "suppressed": null }
```

### 7.2 策略规则

1. **严重度**：`urgent` 立刻投递所有启用且 `min_severity<=urgent` 的渠道；`normal` 进收件箱，按 `digest` 设置决定是单条投递还是进摘要；`info` 只进收件箱。
2. **静默时段**（默认 22:30–08:00）：`normal` 和 `info` 推迟到时段结束；`urgent` 不受影响，但价格提醒本来只在交易时段产生。
3. **去重与折叠**：`dedupe_key` 相同直接丢弃；同一 `subject` 在 10 分钟内的同类 `normal` 事件折叠成一条。
4. **摘要**：每个交易日两次（默认 08:35、21:30），把期间所有 `normal` 事件合成一个 `digest.summary`，按股票分组，例如「今晚分析了 12 只，3 只改判」。摘要本身也是一个事件，走同样的投递。
5. **离线转发的外发规则**：只针对服务端转发的价格提醒（第 8 节）。客户端在线时只弹桌面通知和写收件箱，外部渠道在「客户端离线或 N 分钟未确认」后才由服务端发，开关是 `relay_channel.only_offline`（默认开）。**客户端自己产生的事件**（判断变化、条件满足等）因为产生时客户端必然在线，外部渠道由客户端按本节的其他规则直接发，不适用「只在离线时」。
6. **频率上限**：每渠道每分钟、每日条数。超限的标 `suppressed(rate_limit)`，不重试，汇总进当天摘要。

### 7.3 收件箱

```sql
CREATE TABLE notification (
  notif_id   TEXT PRIMARY KEY,
  event_id   TEXT NOT NULL UNIQUE REFERENCES event(event_id),   -- 一个事件至多一条
  title      TEXT NOT NULL, body TEXT NOT NULL,
  tone       TEXT NOT NULL,                  -- good | warn | bad | flat（涨红跌绿的语义，由类型决定）
  link       TEXT NOT NULL DEFAULT '{}',     -- {"page":"holdings","tab":"now","code":"600036.SH"}
  group_key  TEXT,                           -- 页面里「今天 / 昨天 / 即将发生」的分组依据
  digest_id  TEXT,                           -- 被摘要吸收时指向摘要的 notif_id
  created_at INTEGER NOT NULL,
  read_at    INTEGER, archived_at INTEGER
);
CREATE INDEX notif_unread ON notification (read_at, created_at DESC);
```

收件箱是页面里通知抽屉的唯一数据源。现在市场页和持仓页各自拼通知的做法（一个读 `market_store` 事件，一个现算），都改为读这张表，已读状态也统一在这里。

### 7.4 渠道

```sql
CREATE TABLE channel (
  channel_id   TEXT PRIMARY KEY,
  type         TEXT NOT NULL,                -- desktop | inapp | wecom_webhook | bark | serverchan | email
  name         TEXT NOT NULL,
  secret_ref   TEXT,                         -- 密钥在系统钥匙串里的引用，库里不存明文
  config       TEXT NOT NULL DEFAULT '{}',   -- 非敏感配置（如群名、邮箱域）
  min_severity TEXT NOT NULL DEFAULT 'normal',
  quiet_hours  TEXT NOT NULL DEFAULT '{"from":"22:30","to":"08:00"}',
  rate_limit   TEXT NOT NULL DEFAULT '{"per_min":10,"per_day":100}',
  relay        INTEGER NOT NULL DEFAULT 0,   -- 1 = 用户同意把这个渠道的密钥上传到服务端，用于离线转发（对应一条 relay_channel）
  enabled      INTEGER NOT NULL DEFAULT 1,
  verified_at  INTEGER                       -- 发过测试消息并确认收到
);
```

渠道适配器统一接口：`send(notification, channel) -> {provider_msg_id}`，抛出带 `class`（transient / permanent）的异常。新增一个渠道只需要写一个适配器并在 `type` 里注册。

### 7.5 投递

```sql
CREATE TABLE delivery (
  delivery_id TEXT PRIMARY KEY,
  notif_id    TEXT NOT NULL REFERENCES notification(notif_id),
  channel_id  TEXT NOT NULL REFERENCES channel(channel_id),
  state       TEXT NOT NULL,                 -- pending | sending | sent | failed | dead | suppressed
  deliver_after INTEGER NOT NULL,            -- 静默时段内的推迟到这个时刻
  attempts    INTEGER NOT NULL DEFAULT 0,
  next_attempt_at INTEGER,
  last_error  TEXT,
  provider_msg_id TEXT,
  suppressed_reason TEXT,                    -- quiet | rate_limit | acked_by_client | channel_disabled | digest
  sent_at     INTEGER,
  UNIQUE (channel_id, notif_id)              -- 幂等键：同一条通知同一个渠道只会有一条投递
);
```

状态机：`pending → sending → sent`；失败且 `transient` → `failed`（带 `next_attempt_at`，退避 1 分钟、5 分钟、30 分钟，最多 3 次）→ 回到 `pending`；`permanent` 或次数用尽 → `dead`，并产出一条 `task.failed` 提示该渠道不可用。`suppressed` 是终态，必须写明原因。

## 8. 离线转发（服务端，价格提醒专用）

**目的**：客户端关机时，价格提醒仍然能发出去。**边界**：服务端只存评估所需的最少信息，不存持仓、成本、理由、分析结果。

### 8.1 数据

```sql
CREATE TABLE relay_device (
  device_id TEXT PRIMARY KEY, token_hash TEXT NOT NULL,
  created_at INTEGER NOT NULL, last_seen_at INTEGER NOT NULL, last_stream_at INTEGER
);
CREATE TABLE relay_alert (
  device_id TEXT NOT NULL, alert_id TEXT NOT NULL,           -- alert_id = 客户端 rule_id
  ts_code TEXT NOT NULL, side TEXT NOT NULL, price REAL NOT NULL, confirm TEXT NOT NULL,
  rearm TEXT NOT NULL, state TEXT NOT NULL, fire_seq INTEGER NOT NULL DEFAULT 0,
  fired_at INTEGER, rearm_at INTEGER, last_value TEXT,
  label TEXT NOT NULL DEFAULT '',                            -- 消息里显示的名称，如「拓普集团」，不含用途说明
  channels TEXT NOT NULL DEFAULT '[]',                       -- relay_channel 的 id
  PRIMARY KEY (device_id, alert_id)
);
CREATE TABLE relay_channel (
  device_id TEXT NOT NULL, channel_id TEXT NOT NULL, type TEXT NOT NULL,
  secret_enc BLOB NOT NULL,                                  -- 用服务端主密钥加密的 webhook / key
  only_offline INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (device_id, channel_id)
);
CREATE TABLE relay_event (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT NOT NULL,
  event TEXT NOT NULL,                                       -- 第 5 节的事件信封
  created_at INTEGER NOT NULL, acked_at INTEGER, pushed_at INTEGER
);
```

### 8.2 协议

```
PUT  /v1/relay/alerts          { "v":1, "rev": 12, "alerts":[…全量…], "channels":[…] }   → { "rev": 12, "accepted": n }
GET  /v1/relay/events?after=<seq>                                                       → 事件列表
GET  /v1/relay/stream          (SSE)                                                    → 实时事件；连接即视为在线
POST /v1/relay/ack             { "seqs":[…] }
```

- 提醒定义**以全量加 `rev` 同步**，服务端用乐观版本校验，冲突返回当前 `rev` 让客户端合并重推。
- 触发状态（`fired` / `fire_seq`）**以服务端为准**。客户端同步定义后不再评估这些提醒，只有服务端不可达时才降级为本机评估。
- 触发后产出 `price.alert.fired` 事件写入 `relay_event`。
- **外发规则**：设备在线（SSE 已连接）则只通过流推给客户端，由客户端写收件箱、弹桌面通知；超过 `ack 超时`（默认 3 分钟）仍未确认，或设备离线，再用该设备的 `relay_channel` 外发。`only_offline=0` 的渠道总是外发。
- 外发消息同样带 `event_id`，客户端之后拉到同一事件时按 `dedupe_key` 去重，不会重复入箱。

### 8.3 安全

- 渠道密钥（webhook、Bark key 等）**只有用户在渠道设置里明确勾选「用于离线转发」时才上传**，对应本地 `channel.relay=1`；不勾选就只有客户端在线时才能外发。服务端用主密钥加密存储，主密钥来自环境变量或密钥管理服务；日志里一律脱敏。
- 设备令牌只存哈希，可随时撤销；撤销即删除该设备的全部 `relay_*` 数据。
- 服务端不向任何其他设备泄露某设备的提醒内容。

## 9. 客户端宿主

**目标**：窗口关了调度还在跑；电脑睡眠和唤醒不丢任务。

| 能力 | 做法 |
|---|---|
| 托盘常驻 | 系统托盘图标；关闭窗口只隐藏。菜单：打开、暂停通知 1 小时、今天的任务状态、退出 |
| 登录自启 | `app.setLoginItemSettings({ openAtLogin: true, openAsHidden: true })`，首次启动询问并可在设置里关闭 |
| 单实例 | `requestSingleInstanceLock`，再次打开只唤起已有窗口 |
| 进程监督 | Electron 主进程拉起并监督本机后端（Go + Python）。后端写心跳，主进程发现 90 秒无心跳就重启 |
| 休眠与唤醒 | `powerMonitor` 的 `resume`、`unlock-screen` 事件触发一次「立即 tick」，调度器按 `misfire` 和 `catchup_ttl_min` 补跑 |
| 长任务不被打断 | 执行 LLM 批处理时临时 `powerSaveBlocker.start('prevent-app-suspension')`，结束即释放；不阻止系统整体睡眠 |
| 退出语义 | 只有托盘菜单的「退出」才结束后端；系统关机和注销时优雅停止，未完成的任务靠租约回收 |
| 桌面通知 | 第一次产生通知时请求系统权限；点击通知按 `link` 打开对应页面和对象 |

调度器和执行器在 Python 后端里，Electron 只负责宿主能力；二者通过本机 HTTP 通信（例如 `POST /sched/tick?reason=resume`）。

## 10. 结果存储与运维接口

### 10.1 结果存在哪

| 内容 | 存放位置（客户端） | 保留 | 说明 |
|---|---|---|---|
| 调度记录（`task_run`、`job`、`job_step`、`job_attempt`、`budget_ledger`、`result`、`llm_call`） | `data/scheduler.db` | 60 天；`result` 的大对象文件 30 天 | 独立一个库，不和综合判断、持仓的库抢锁 |
| **分析结果**（综合判断、正反方、各维度体检） | 沿用现有综合判断库 `data/synthesis.db` 的 `runs` | **长期**（战绩要回头对） | 不复制一份，任务里只放引用 `run:<id>` |
| 事件、规则、收件箱、渠道、投递 | `data/notify.db`（沿用现有文件名，迁移后取代原来的发送记录表） | 事件 180 天；收件箱 90 天 | |
| 共享产物（事实表、市场快照等） | 服务端产物库；客户端本地也缓存一份在 `data/artifacts/` | 30 天，被订阅的最新一份一直保留 | |
| 持仓、想买、理由、体检快照 | `data/holdings.db`（不变） | 长期 | 私有，不出本机 |

服务端同理：调度记录在服务端自己的库里，和客户端互不相通。

**`job.output` 的统一格式：只放摘要和引用，不放大对象。**

```json
{
  "v": 1,
  "summary": { "judgment": { "lean": "unclear", "level": 0 }, "changed": true, "skipped_steps": ["review.news"] },
  "refs": [
    { "role": "judgment",       "ref": "run:48213" },
    { "role": "facts",          "ref": "artifact:stock.facts/002050.SZ/2026-11-05/close" },
    { "role": "review.sector",  "ref": "res:01JC7Z…" },
    { "role": "event",          "ref": "event:01JC7K…" }
  ],
  "metrics": { "duration_ms": 41200, "tokens": 38120, "calls": 7 }
}
```

`ref` 有四种前缀，统一用 `GET /sched/results/{ref}` 解析：

| 前缀 | 指向 |
|---|---|
| `run:<id>` | 综合判断库里的一次运行 |
| `artifact:<kind>/<subject>/<as_of>/<session>` | 共享产物 |
| `event:<event_id>` | 事件 |
| `res:<ulid>` | 本库的 `result`（大对象、体检原文等） |

**输入同样只存摘要和引用。**`job.input` 记参数；大的输入（例如事实表）不复制，只存引用加哈希（`facts_hash`）。只要产物还在保留期内，就可以用同样的输入**重放**一个单元；过了保留期，只能看到哈希，不能重放。

**脱敏。**输入、输出、错误里不得出现密钥、令牌、渠道地址。LLM 的提示词和回复默认只存哈希（`llm_call`），打开调试开关才存原文，并且调试开关只对单次运行生效，到期自动清除原文。

### 10.2 查询接口

客户端的本机后端暴露 `/sched/...`；服务端同样的接口在 `/v1/admin/sched/...`，需要管理员令牌。

**通用约定：**
- 分页用游标：`?limit=50&cursor=…`，返回 `{ "items": [], "next_cursor": null }`；
- 时间字段都是毫秒时间戳，交易日是 `YYYY-MM-DD`；
- 错误统一 `{ "error": { "code": "not_found", "message": "…", "details": {} } }`；
- 支持 `ETag` / `If-None-Match`，列表接口适合轮询；
- 带 `?fields=` 可以只取需要的字段。

| 接口 | 说明 |
|---|---|
| `GET /sched/tasks` | 全部任务：定义、**生效中的触发配置**、是否被覆盖、**下次触发时间**、最近一次运行 |
| `GET /sched/tasks/{task_id}` | 单个任务的完整定义（依赖、就绪门、队列、预算、重试）加最近 10 次运行 |
| `GET /sched/runs?task_id=&state=&since=&until=&limit=` | 运行列表 |
| `GET /sched/runs/{run_id}` | 一次运行：状态、当时生效的配置快照、`code_version`、计数、时间线（规划、等待就绪、开始、结束） |
| `GET /sched/runs/{run_id}/jobs?state=&unit_key=` | 这次运行下的各个单元 |
| `GET /sched/jobs/{job_id}` | **单元详情**：输入、输出（摘要和引用）、错误、租约、下面的步骤和尝试 |
| `GET /sched/jobs/{job_id}/attempts` | 每次尝试：开始、结束、失败类别、耗时、token |
| `GET /sched/jobs/{job_id}/steps/{step}` | 某一步的状态和 `output_ref` |
| `GET /sched/results/{ref}` | 解析任意引用，返回内容和 `content_type`；大对象支持 `Range` |
| `GET /sched/logs?run_id=&job_id=&level=&after=` | 日志（JSON 行），按时间游标 |
| `GET /sched/budget?resource=&window=` | 预算使用 |
| `GET /sched/health` | 调度器是否活着：最近一次 tick、领导者、各队列积压、有没有 overdue |
| `GET /sched/stream` | SSE：run、job 的状态变化，页面做实时视图用 |

**`GET /sched/tasks` 的一项：**

```json
{
  "v": 1,
  "task_id": "stock.nightly", "title": "每晚个股分析", "placement": "client",
  "enabled": true, "overridden": true,
  "trigger": { "kind": "daily", "at": "20:30", "window_min": 210, "days": "trading" },
  "trigger_default": { "kind": "daily", "at": "21:00", "window_min": 210, "days": "trading" },
  "next_fire_at": 1793878200000, "next_fire_trade_date": "2026-11-06",
  "queue": "llm", "gates": ["daily_bars", "moneyflow"], "after": [{ "task": "market.close", "mode": "soft" }],
  "budget": { "per_run": { "calls": 400, "tokens": 2000000 } },
  "last_run": { "run_id": "01JC7A…", "period_key": "trading_day:2026-11-05", "state": "partial",
                "counts": { "jobs": 40, "succeeded": 37, "failed": 1, "skipped": 2 },
                "started_at": 1793871000000, "finished_at": 1793874500000 }
}
```

**`GET /sched/jobs/{job_id}` 的一项：**

```json
{
  "v": 1, "job_id": "01JC7B…", "run_id": "01JC7A…", "task_id": "stock.nightly",
  "unit_key": "002050.SZ", "period_key": "trading_day:2026-11-05",
  "state": "succeeded", "attempt": 2, "max_attempts": 3,
  "input": { "ts_code": "002050.SZ", "tier": "hold", "facts_hash": "sha256:9f2c…" },
  "output": { "…见 10.1…" },
  "error": null,
  "steps": [
    { "step": "collect",       "state": "succeeded", "tokens": 0,     "output_ref": "artifact:stock.facts/002050.SZ/2026-11-05/close" },
    { "step": "review.sector", "state": "succeeded", "tokens": 6120,  "output_ref": "res:01JC7Z…" },
    { "step": "chief",         "state": "succeeded", "tokens": 18400, "output_ref": "run:48213" }
  ],
  "attempts": [
    { "attempt": 1, "state": "failed",    "error": { "class": "transient", "code": "RATE_LIMIT" }, "duration_ms": 8100 },
    { "attempt": 2, "state": "succeeded", "duration_ms": 33100, "tokens": 38120 }
  ],
  "events": ["01JC7K…"]
}
```

### 10.3 配置与控制接口

| 接口 | 说明 |
|---|---|
| `PUT /sched/tasks/{task_id}/config` | 覆盖启停、触发配置、参数。请求体 `{ "enabled": true, "trigger": {…}, "params": {…} }`，按 3.2 的触发器格式校验；返回生效后的配置和 `next_fire_at` |
| `DELETE /sched/tasks/{task_id}/config` | 清除覆盖，恢复代码里的默认配置 |
| `POST /sched/tasks/{task_id}/preview` | 干跑：给定（或当前的）触发配置，返回接下来 N 次触发时刻，按交易日历和时段计算，不产生任何任务 |
| `POST /sched/tasks/{task_id}/run` | 手动运行。`{ "period_key"?, "units"?: ["600036.SH"], "input"?: {}, "force": false, "dry_run": false }`；`dry_run` 只展开单元、检查就绪门和预算，不执行 |
| `POST /sched/tasks/{task_id}/pause` / `resume` | 暂停和恢复，暂停期间到点的周期按 `misfire` 处理 |
| `POST /sched/jobs/{job_id}/retry` | `{ "from_step"?: "chief" }`，从指定步骤重试；`dead` 的单元也可以手动重试 |
| `POST /sched/jobs/{job_id}/cancel` | 取消尚未开始的单元 |

**权限与保护：**
- 覆盖配置要经过校验：触发间隔不得小于该任务声明的下限（例如价格提醒不低于 1 分钟），`window_min` 不得超过一天。
- 手动运行默认受预算限制，`force` 才能忽略 `skip_if_fresh`，但**不能绕过预算上限**。
- 所有控制类接口写一条审计事件 `system.config_changed`（谁、改了什么、旧值新值），不含敏感内容。

### 10.4 日志

JSON 行，必带 `run_id`、`job_id`、`task_id`、`unit_key`、`attempt`、`step`，级别 `debug/info/warn/error`，敏感字段脱敏。日志文件按天滚动保留 14 天，`/sched/logs` 读的就是这些文件，所以它们和 `job` 记录用同一组 ID 可以互相跳转。

### 10.5 看门狗与指标

- **看门狗**（`watchdog`，每 10 分钟）：
  - `sched_state.last_tick` 超过 3 个 tick 没更新 → 调度器假死，重启；
  - 关键任务在「应完成时刻」之后仍未 `succeeded` → 产出 `task.overdue`；
  - 同一任务的 `dead` 数超过阈值 → 产出聚合的 `task.failed`，**不是每个单元一条**。
- **指标**（先落表，后续可接图表）：每次 run 的耗时、单元数、跳过数、重试数、预算使用、各渠道送达率。数据来自 `task_run`、`job_attempt`、`llm_call`、`delivery`，不另存一份。

## 11. 安全与隐私

- **密钥不入库**：LLM key、tushare token、各渠道密钥只存系统钥匙串（客户端）或加密字段（服务端离线转发）。仓库里只提供 `config.example.yaml`。
- **最小化**：服务端只持有订阅的股票代码、提醒定义和渠道密钥。持仓数量、成本、买入理由、分析结果不出本机。
- **产物无用户信息**：`stock.facts` 等产物不含任何设备或用户标识，个性化只发生在客户端读取之后。
- **LLM 提示词**：只放该任务必需的事实；持仓、成本只在用户本机的任务里使用。
- **认证**：设备令牌；接口限流；管理接口单独的管理员令牌。
- **数据授权与合规**：见第 14 节，是上线前必须确认的事项。

## 12. 与现有代码的映射与迁移

| 现有 | 新规范里的位置 | 迁移方式 |
|---|---|---|
| `synthesis/worker.py` 的 `jobs` 表（按股票合并、30 秒去抖） | `job`（`trigger.kind=event`，`coalesce_key=ts_code`） | 保留接口 `worker.request(code, reason)`，内部改为入 `job`；旧表读完即弃 |
| `market_job.py` 的 `SLOTS`、内存 `done` 集合 | `market.pre` / `market.close` 两个任务定义；`task_run` 唯一键代替 `done` | 先让两者并存，对比一周后切换 |
| `reviews/attach.py` 的按需后台线程 | `stock.nightly` 的 `review.*` 步骤；页面按需请求也走 `event` 触发 | 保持页面行为不变，只换调度层 |
| 综合判断存储的 `alerts` 表 | `rule(type=price_alert)` + 同步到 `relay_alert` | 一次性脚本把现有提醒转成规则，`base_name` 为「理由不成立」的标 `origin=reason_invalid` |
| 持仓的 `rules` 表（`flow_out`） | `rule(type=flow_streak)` | 同上 |
| 持仓的想买条件、`reasons.invalid` | `rule(origin=wish / reason_invalid)`，随来源对象增删 | 写入时自动派生，不再单独存 |
| `notify/outbox.py` 的 `sent(key)` | `delivery` 的 `(channel_id, notif_id)` 唯一键 | 旧键不迁移，当天起生效 |
| `notify/emit.py`、`notify/wecom.py` | `wecom_webhook` 渠道适配器 | 保留 `emit()` 作为兼容入口，内部改为「写事件」 |
| `market_store` 的 `event` / 已读 | `event` + `notification` | 导入近 60 天，已读状态一并迁 |
| 持仓的 `kv.notif_seen` | `notification.read_at` | 一次性转换 |
| `market_notify.settings()` 开关 | 规则的 `market_change` 订阅 + 渠道设置 | 开关映射成订阅项 |

兼容原则：**旧接口先不删**，新旧并行一个版本，页面切到新接口后再清理。

## 13. 分期与验收

| 期 | 内容 | 验收标准 |
|---|---|---|
| 0 | 密钥出库、钥匙串、`profile` 配置、`config.example.yaml` | 仓库里搜不到明文密钥；不配钥匙串时应用能给出明确提示 |
| 1 | 客户端调度内核、交易日历、补跑与唤醒、托盘常驻与自启；市场任务迁入；本机价格提醒、收件箱、桌面通知 | 合盖 3 小时后打开，错过的盘前简报在 1 分钟内补上；同一周期重复 tick 不产生重复任务；非交易日不触发 |
| 2 | 服务端骨架：设备认证、就绪标记、离线转发 | 客户端关机，价格触达后 3 分钟内外部渠道收到一条，且只有一条；客户端重新上线后收件箱里有同一事件，不重复 |
| 3 | 订阅与预热、`stock.facts` 产物、客户端每晚分析、判断变化事件 | 一晚上 40 只持仓和自选在预算内完成；事实没变的跳过；LLM 失败只重试失败的那一步 |
| 4 | 配额、限流、订阅集合运维、看门狗、摘要、更多渠道 | 故意让一个任务连续失败，只收到一条聚合告警；静默时段内的普通事件在早上进摘要 |

## 14. 待定问题

1. **数据授权**：服务端缓存行情和事实表，本质上是向其他用户再分发数据源的数据。上线前要核对 tushare 等数据商的条款。备选：服务端只缓存不含原始行情的衍生结果，原始行情由各客户端用自己的 token 取。
2. **合规**：向他人提供个股方向判断，可能涉及证券投资咨询监管。当前设计里 AI 判断都在客户端、用用户自己的密钥生成，平台不分发判断结果，但这点要请专业人士确认，并据此决定产品措辞。
3. **服务端部署位置与备份**：影响离线提醒的可用性目标（例如允许的停机时间）。
4. **价格提醒的行情源**：免费源的稳定性和限流，是否需要付费的实时源。
5. **预热的触发时间**：取决于各就绪门的实际发布时间，需要先探测一周，再定默认窗口。


## 15. 实现状态（2026-11-05）

代码位置：调度内核 `app/scheduler/`，任务 `app/scheduler/tasks/`，通知 `app/notify/`，服务端 `app/server/`（`python -m app.server.main`，端口 8100），Electron 宿主 `go-harness/native/host.js`。测试：`tests/test_scheduler_*`、`test_notify_*`、`test_server.py`、`test_relay_integration.py`、`test_nightly.py`、`test_ops.py`、`test_announce*.py`。

**已实现**

| 期 | 内容 |
|---|---|
| P0 | `web/config.yaml` 出库（`config.example.yaml` 入库，首次启动自动复制）；密钥只存引用（`env:`、`keychain:`），库里不存明文 |
| P1 | 调度内核（触发器、交易日历、补跑/过期、依赖和就绪门、扇出、重试退避、`not_ready`、检查点步骤、租约回收、事件去抖合并、预算和限速、领导者租约）；`/sched/...` 查询和控制接口；市场盘前/收盘迁成任务；事件注册表、规则边沿触发、策略（严重度、静默时段、折叠、摘要）、收件箱、渠道（桌面、企业微信、Bark、Server酱）、投递（退避、限频、过期）；价格提醒（盘中 touch、收盘 close、日内涨跌）；`/notify/...` 接口；托盘、自启、单实例、休眠恢复、桌面通知 |
| P2 | 服务端：设备注册和令牌（只存哈希，撤销即删数据）、订阅并集、就绪标记、共享产物（ETag/304、按需生成）、离线转发（评估、SSE、确认、在线先走流、超时才外发、渠道密钥加密、逐渠道只发一次）、管理接口 `/v1/admin/sched`（需要 `ADMIN_TOKEN`）；客户端同步（定义、订阅、拉事件、服务端权威/降级、同一把去重键） |
| P3 | 订阅并集预热 `stock.facts.warm`、市场快照 `market.publish`、公告采集和规则分类 `news.collect`；客户端每晚个股分析 `stock.nightly`（步骤检查点、判断变化事件、预算）；公告监控 `news.watch`（stream 水位、分层默认规则、折叠、每日上限） |
| P4 | 看门狗（`task.overdue`）、失败聚合（`task.failed`）、摘要、清理、默认规则（新持仓自动带 ±7%）、限频和预算 |

**和设计的差异 / 还没做**

- 调度 tick 15 秒（设计 30 秒）；`/sched/stream` 是轮询式 SSE；`/sched/logs`（JSON 行日志文件）和指标表没做；配置变更只写日志，没产出 `system.config_changed` 事件。
- `stock.nightly` 仍在本机重新取数；服务端的 `stock.facts` 已能生产和按需生成，但客户端综合判断还没改成从产物起步（需要把 `load_table` 拆成「取数」和「装表」两段）。`llm_call` 表和统一的 LLM 网关没做，预算按「分析次数」计，不是 token。
- 规则评估器只实现了 `price_alert`、`price_move(day_pct)`、`announcement`、`judgment_change`；`price_move(speed)`、`limit_hit`、`price_extreme`、`reason_status`、`wish_condition`、`flow_streak`、`calendar_upcoming`、`market_change`、`outcome_settled` 的事件类型已注册、接口可建规则，评估器还没写。`news.item`、`news.impact_assessed` 同理。
- 市场页、持仓页的通知抽屉已经并入收件箱（`app/notify/pageview.py`）：页面自己算的条目照旧，收件箱的新通知并进来、已读同步；持仓页里和收件箱重复的「到价」「改判」以收件箱为准。还没有调度和通知设置的页面。
- Electron：没有进程监督心跳、`powerSaveBlocker`；安装包里还没有打进股票后端（开发模式从相邻目录拉起）。
- 服务端：渠道密钥的主密钥来自 `RELAY_MASTER_KEY`（没配时开发环境落盘 `data/relay.key`）；设备注册没有限流和滥用防护；服务端用 SQLite，上线前换库。
- 第 14 节的待定问题（数据授权、合规、账号体系）仍然是上线前必须确认的事项。
