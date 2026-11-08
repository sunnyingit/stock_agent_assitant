"""产业链位置：这家公司在链条的哪一环，卖什么、卖给谁、原料从哪来、同行是谁。

资料来自东方财富 F10 的「核心题材」「主营构成」「行业对比」。上游、下游只取公司自己披露的原文：
挑出写到客户、应用、原材料、供应商的句子，原样给出；没写的就是没披露，不替公司补。
「同业」是行业对比里的可比公司，不是上下游。数字在这里算好，模型只引用 text。
"""

from __future__ import annotations

import re

from app import config
from app.tools import cache
from app.tools.http import get_json
from app.tools.stock import Stock, is_hk

BASE = "https://emweb.securities.eastmoney.com/PC_HSF10"
_SKIP_ITEMS = ("合计", "总计", "调整", "合并抵消", "抵消")
# 只认真正在说「卖给谁、原料从哪来」的句子；「快速响应客户需求」「供应商管理」「公司是关键供应商」这类不算
_DOWNSTREAM = re.compile(r"应用于|应用领域|应用场景|下游|终端(市场|客户|应用|产品)|(主要|重要|核心|战略|前五大)客户|客户(包括|主要|有|为)|与[^，。；]{2,40}等?(主要|知名)?客户")
_UPSTREAM = re.compile(r"原材料|原料|上游|(主要|重要|前五大)供应商|向[^，。；]{0,15}采购|供应商(包括|有)")
_FROM = {"主营业务", "核心竞争力", "对外扩张", "对内扩张"}  # 公司自己写的；「行业背景」是行业数据，另外给
_CAVEAT = "上下游只取公司自己披露的原文，没写的就是没披露；同业只是同行，不是上下游。"


def secucode(stock: Stock) -> str:
    """002050.SZ → SZ002050。"""
    code, _, market = stock.ts_code.partition(".")
    return f"{market.upper()}{code}"


def _fetch(page: str, stock: Stock) -> dict:
    key = f"chain:{page}:{stock.ts_code}"
    cached = cache.get(key)
    if isinstance(cached, dict):
        return cached
    body = get_json(f"{BASE}/{page}/PageAjax", {"code": secucode(stock)})
    body = body if isinstance(body, dict) else {}
    cache.set(key, body, config.CACHE_TTL["fundamental_detail"])
    return body


def build(stock: Stock) -> dict:
    if is_hk(stock):
        return _doc(stock, [], "港股暂无产业链资料", {})
    parts = {}
    for page in ("CoreConception", "BusinessAnalysis", "IndustryAnalysis"):
        try:
            parts[page] = _fetch(page, stock)
        except Exception:  # noqa: BLE001 - 某一页取不到，其余照常给
            parts[page] = {}
    body = format_chain(parts["CoreConception"], parts["BusinessAnalysis"], parts["IndustryAnalysis"], stock.name)
    return _doc(stock, body["facts"], body["text"], body["chain"], body["unavailable"])


def format_chain(core: dict, biz: dict, ind: dict, name: str) -> dict:
    facts: list[dict] = []
    missing: list[str] = []
    chain: dict = {"industry": [], "concepts": [], "products": [], "regions": [], "upstream": [], "downstream": [], "demand": [], "peers": []}

    levels, concepts = _boards(core.get("ssbk") or [])
    chain["industry"], chain["concepts"] = levels, concepts
    if levels:
        text = "所处行业：" + " › ".join(levels)
        if concepts:
            text += "；精确关联的概念：" + "、".join(concepts[:8])
        facts.append({"id": "fd.chain.position", "text": text})
    else:
        missing.append("没有取到所属行业")

    period, products, regions = _mix(biz.get("zygcfx") or [])
    chain["products"], chain["regions"] = products, regions
    if products:
        facts.append({"id": "fd.chain.products", "text": f"主营按产品（{period}）：" + "；".join(_share_text(item) for item in products[:6])})
    else:
        missing.append("没有取到主营按产品的构成")
    if regions:
        facts.append({"id": "fd.chain.regions", "text": f"销售区域（{period}）：" + "；".join(_share_text(item, margin=False) for item in regions[:5])})

    rows = [row for row in (core.get("hxtc") or []) if isinstance(row, dict)]
    segments = [row for row in rows if row.get("KEY_CLASSIF") == "主营业务"]
    for index, row in enumerate(segments[:4], start=1):
        facts.append({"id": f"fd.chain.segment.{index}", "text": f"业务环节「{_clean(row.get('KEYWORD'))}」：{_clip(row.get('MAINPOINT_CONTENT'), 110)}"})

    pool = [row for row in rows if row.get("KEY_CLASSIF") in _FROM]
    up = _pick(pool, _UPSTREAM, 3)
    down = _pick(pool, _DOWNSTREAM, 4, skip=set(up))
    chain["downstream"], chain["upstream"] = down, up
    for index, sentence in enumerate(down, start=1):
        facts.append({"id": f"fd.chain.downstream.{index}", "text": "公司披露的下游/客户：" + sentence})
    if not down:
        missing.append("公司披露里没有写明下游客户或应用")
    for index, sentence in enumerate(up, start=1):
        facts.append({"id": f"fd.chain.upstream.{index}", "text": "公司披露的上游/原料：" + sentence})
    if not up:
        missing.append("公司披露里没有写明上游原材料或供应商")

    demand = [row for row in rows if row.get("KEY_CLASSIF") == "行业背景"]
    for index, row in enumerate(demand[:3], start=1):
        sentence = _clip(row.get("MAINPOINT_CONTENT"), 120)
        chain["demand"].append({"industry": _clean(row.get("KEYWORD")), "text": sentence})
        facts.append({"id": f"fd.chain.demand.{index}", "text": f"下游景气「{_clean(row.get('KEYWORD'))}」：{sentence}"})

    peers = _peers(ind.get("czxbj") or [], name)
    chain["peers"] = peers
    if peers:
        facts.append({"id": "fd.chain.peers", "text": "同业可比公司：" + "、".join(peers[:8])})

    lines = [item["text"] for item in facts if item["id"] in ("fd.chain.position", "fd.chain.products")]
    lines += [item["text"] for item in facts if item["id"].startswith(("fd.chain.downstream", "fd.chain.upstream"))][:4]
    text = " ".join(lines) + (" " if lines else "") + _CAVEAT
    return {"facts": facts, "text": text, "chain": chain, "unavailable": missing}


def _boards(rows: list[dict]) -> tuple[list[str], list[str]]:
    """前几条是行业（一级、二级、三级），到地区板块为止；IS_PRECISE 为 1 的是精确关联的概念。"""
    ordered = sorted((row for row in rows if isinstance(row, dict)), key=lambda row: _int(row.get("BOARD_RANK"), 999))
    levels: list[str] = []
    for row in ordered[:3]:
        name = _clean(row.get("BOARD_NAME"))
        if not name or name.endswith("板块"):  # 行业三级之后是地区板块
            break
        levels.append(name)
    concepts = [_clean(row.get("BOARD_NAME")) for row in ordered if str(row.get("IS_PRECISE")) == "1" and _clean(row.get("BOARD_NAME"))]
    return levels, concepts


def _mix(rows: list[dict]) -> tuple[str, list[dict], list[dict]]:
    """最新一期的主营构成：2 是按产品，3 是按地区；没有产品就退到 1（按行业）。"""
    dated = [row for row in rows if isinstance(row, dict) and row.get("REPORT_DATE")]
    if not dated:
        return "", [], []
    latest = max(str(row["REPORT_DATE"])[:10] for row in dated)
    now = [row for row in dated if str(row["REPORT_DATE"])[:10] == latest]

    def items(kind: str) -> list[dict]:
        out = []
        for row in now:
            if str(row.get("MAINOP_TYPE")) != kind:
                continue
            item = _clean(row.get("ITEM_NAME"))
            if not item or any(skip in item for skip in _SKIP_ITEMS):
                continue
            out.append({"name": item, "share": _pct(row.get("MBI_RATIO")), "gross": _pct(row.get("GROSS_RPOFIT_RATIO"))})
        return sorted(out, key=lambda item: -(item["share"] or 0))

    return latest, items("2") or items("1"), items("3")


def _share_text(item: dict, margin: bool = True) -> str:
    text = f"{item['name']} 占 {item['share']:.1f}%" if item["share"] is not None else item["name"]
    if margin and item["gross"] is not None:
        text += f"、毛利率 {item['gross']:.1f}%"
    return text


def _pick(rows: list[dict], pattern: re.Pattern, limit: int, skip: set[str] | None = None) -> list[str]:
    """从公司自己写的话里挑出说到这件事的句子，原样给出；一句话只归一边。"""
    seen: set[str] = set(skip or ())
    out: list[str] = []
    for row in rows:
        for sentence in re.split(r"[。；;\n]", str(row.get("MAINPOINT_CONTENT") or "")):
            sentence = sentence.strip()
            if len(sentence) < 8 or not pattern.search(sentence):
                continue
            short = _clip(sentence, 90)
            if short in seen:
                continue
            seen.add(short)
            out.append(short)
            if len(out) >= limit:
                return out
    return out


def _peers(rows: list[dict], name: str) -> list[str]:
    out = []
    for row in rows:
        peer = _clean(row.get("CORRE_SECURITY_NAME"))
        if peer and peer != name and not peer.startswith("行业") and not peer.endswith("B股"):
            out.append(peer)
    return out


def _doc(stock: Stock, facts: list[dict], text: str, chain: dict, unavailable: list[str] | None = None) -> dict:
    return {
        "ts_code": stock.ts_code, "stock_name": stock.name, "part": "chain", "facts": facts, "text": text,
        "chain": chain, "unavailable": unavailable if unavailable is not None else ([] if facts else ["没有取到"]),
    }


def _clean(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _clip(value, limit: int) -> str:
    text = _clean(value)
    return text if len(text) <= limit else text[:limit].rstrip("，、；, ") + "…"


def _pct(value) -> float | None:
    try:
        return float(value) * 100
    except (TypeError, ValueError):
        return None


def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
