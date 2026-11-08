"""综合判断的阈值、菜单和词表。调参数改这里，不改逻辑。

存档和战绩都带 POLICY_VERSION：改了任何一项，都要升版本号。
"""

POLICY_VERSION = "policy-9"   # 2：上下价位不再为空（补底候选）；3：页面带走势与判断；4：均线、成本区按相对现价的位置归到上方或下方；5：新高新低时用波动带兜底；6：走势带判断分段和区间涨幅；7：走势带今天的判断和高亮起点；8：量价主窗口改为 5 日；9：资金流判断按和自己过去比，形态要涨跌够 1%

# 参与综合的维度。海外映射上线时在这里加一项，可信度分母和文案都会跟着变。
DIMENSIONS = ("volume_flow", "news", "sector", "finance", "fundamental")
DIMENSION_LABEL = {
    "volume_flow": "量价资金",
    "news": "新闻",
    "sector": "板块",
    "finance": "财报",
    "fundamental": "基本面",
}

# ---- 取数与新鲜度 ----
STALE_TRADING_DAYS = 5          # 数据日期落后超过这么多个工作日，视为过期
COLLECT_TIMEOUT = 45            # 页面请求里并发取 5 个维度最多等多少秒；冷启动时板块可能要几分钟
COLLECT_TIMEOUT_BACKGROUND = 300   # 后台重算的等待秒数；超时的维度按"还在整理"处理，下一次再取
FLOW_WINDOW = 5                 # 量价详情主窗口（A 股短线）；3 / 20 日作对照
NEAR_EVENT_DAYS = 20            # 新闻里「临近事件」回看天数（与量价主窗口无关）

# ---- 输入体检 ----
RET_TOLERANCE = 0.02            # 跨模块同一口径的涨跌允许相差 2 个百分点

# ---- 规则版综合 ----
CERTAINTY_WEIGHT = {"high": 1.0, "medium": 0.75, "low": 0.5, None: 0.5}
DIMENSION_WEIGHT = {key: 1.0 for key in DIMENSIONS}
# 模块分歧：财报和这些维度方向相反时，不平均，标出来并列出双方各自最关键的事实
DIVERGENCE_PAIRS = (("finance", "volume_flow"), ("finance", "news"))
KEY_FACTS = {
    "finance": ("fin.growth", "fin.quality", "fin.warn_hits", "fin.days_since"),
    "volume_flow": ("flow.pattern", "flow.vs_self", "flow.big_trend", "flow.net_rate"),
    "news": ("news.main_event", "news.priced_in"),
}
RULE_EDGE = 1.0                 # 加权得分绝对值低于它 → 不明
RULE_STRENGTH_STEPS = (1.0, 2.5, 4.5)   # 达到第 n 个阈值 → 强度 n
LOW_COVERAGE_CAP = 1            # 完成的维度不到一半时，强度最高这么多
POSITION_HIGH_FROM_HIGH = 0.10  # 收盘在一年高点下方 10% 以内 → 偏高
POSITION_LOW_FROM_HIGH = 0.30   # 收盘在一年高点下方 30% 以外 → 偏低

# ---- 可信度档位 ----
CREDIBILITY_HIGH_RATIO = 0.8
CREDIBILITY_MID_RATIO = 0.5

# ---- Agent 论据与强度（阈值是示例，用战绩校准）----
SIDE_MAX_POINTS = 3
SPREAD_STEPS = ((2, 1), (4, 2), (6, 3))     # (分差下限, 强度)；分差 < 2 → 不明
FLIP_MIN_SPREAD = 4             # 方向要改变时，不满足"连续两次"就至少要这么大的分差
CHIEF_RUNS = {"close": 3, "intraday": 1}
CONSENSUS_MIN = 2               # 3 次里至少这么多次"方向 + 位置"相同
SIDE_CACHE_SECONDS = 24 * 3600
MODEL_TIMEOUT = 90              # 一轮并行调用的最长等待秒数

ADOPT_REASONS = {
    "priced_in": "利好已反应",
    "stale": "数据过期",
    "low_grade": "来源等级低",
    "duplicate": "和另一条论据重复",
    "weak": "证据偏弱",
    "contradicted": "被其他事实反驳",
}

# ---- 校验 ----
HEADLINE_MAX = 20
BANNED_WORDS = ("买入", "卖出", "建仓", "加仓", "主力", "吸筹", "洗盘", "出货", "目标价")
# 文字里不必在事实中出现的数字：窗口天数，以及很小的计数
CONTEXT_NUMBERS = (3, 5, 10, 20, 30, 60, 250)
SMALL_COUNT_MAX = 6
BULLISH_HEADLINE_WORDS = ("都在推", "走强", "向好", "上行", "突破", "共振向上", "继续推")
BEARISH_HEADLINE_WORDS = ("承压", "走弱", "转弱", "回落", "下行", "都在拖", "风险加大")

# ---- 价位 ----
DIST_MIN = 0.03
DIST_MAX = 0.15
MERGE_GAP = 0.01
SIMULATE_OUTSIDE = 0.01         # 代入重算时，把收盘价设在价位外侧这么多
PRICE_TICK = 0.01
DOWN_BUFFERS = (0.0, 0.02, 0.03)
DOWN_CONFIRMS = ("close", "close_2d", "close_volume", "close_outflow")
UP_CONFIRMS = ("close", "close_volume")
UP_MEANINGS = ("stronger", "position_high", "priced_in")
SIMULATED_VOLUME_MULTIPLE = 1.6   # close_volume 代入时，当天量是此前均量的倍数

# ---- 防来回翻 / 档位文字 ----
DIRECTION_TEXT = {"bullish": "偏多", "bearish": "偏空", "unclear": "不明"}
TEXT_DIRECTION = {value: key for key, value in DIRECTION_TEXT.items()}
LEVEL_TEXT = {0: "", 1: "稍微", 2: "比较明显", 3: "明显"}
POSITION_TEXT = {"low": "偏低", "mid": "适中", "high": "偏高"}
TEXT_POSITION = {value: key for key, value in POSITION_TEXT.items()}

DISCLAIMER = "以上为基于公开数据的研究分析，不构成投资建议"

# ---- 战绩 ----
OUTCOME_HORIZONS = (5, 10, 20)
