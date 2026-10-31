"""超时、重试和缓存有效期。只在这里改。"""

import os
from pathlib import Path


def _load_env_file() -> None:
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env_file()
TUSHARE_TOKEN = os.environ.get("TUSHARE_TOKEN", "")

# 估值锚点价格（自身历史倍数 × 当前扣非每股收益）会让页面出现价格，是否涉及证券投资咨询资质还没有确认，默认关闭。
# 确认后在环境变量或 .env 里写 ENABLE_PRICE_ANCHORS=1 打开。
ENABLE_PRICE_ANCHORS = os.environ.get("ENABLE_PRICE_ANCHORS", "") == "1"

LLM_API_KEY = os.environ.get("LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY") or ""
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-flash")

# (连接超时, 读取超时)，单位秒
HTTP_TIMEOUT = (3.05, 12)

HTTP_RETRIES = 3
HTTP_BACKOFF = 0.4
HTTP_STATUS_FORCELIST = (429, 500, 502, 503, 504)

# 各接口结果缓存秒数
CACHE_TTL = {
    "stock": 300,
    "moneyflow": 120,
    "basic": 120,
    "news": 300,
    "news_analysis": 300,
    "analysis": 300,
    "fundamental": 24 * 3600,
    "fundamental_detail": 6 * 3600,
    "industry": 3600,
    "financial": 6 * 3600,
    "finance_detail": 6 * 3600,
    "sector_detail": 6 * 3600,
    "peers": 3600,
    "bars": 120,
    "flow_daily": 120,
    "sector": 300,
    "market": 300,
    "margin": 3600,
    "toplist": 3600,
    "volume_flow": 120,
    "news_detail": 120,
}

KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
SURVEY_URL = "https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/CompanySurveyAjax"
