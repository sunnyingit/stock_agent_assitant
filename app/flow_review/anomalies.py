"""异常检测：代码按固定规则查，模型不参与。查到的一定会提示，不依赖模型是否调用成功。

severity：medium（需要留意）、high（建议重点看）。overall：none / attention / warning。
"""

from __future__ import annotations

from app.synthesis.types import Fact


def _row(severity: str, title: str, detail: str, cites: list[str], ids: set[str]) -> dict:
    return {"source": "rule", "severity": severity, "title": title, "detail": detail, "cites": [item for item in cites if item in ids]}


def detect(d: dict, facts: list[Fact]) -> list[dict]:
    ids = {fact.id for fact in facts}
    text = {fact.id: fact.text for fact in facts}
    out: list[dict] = []

    lag = d.get("data_lag")
    if lag:
        out.append(_row("high" if lag > 3 else "medium", "数据不是最新的", f"资金流数据落后最近收盘日 {lag} 个工作日，下面的解读按旧数据做出。", ["fr.data_lag"], ids))
    if d.get("flow_coverage") is not None and d["flow_coverage"] < 0.8:
        out.append(_row("medium", "资金流数据缺了几天", f"近 20 日只有 {text.get('fr.coverage', '')}有资金流数据，统计可能偏差。", ["fr.coverage"], ids))
    rate = d.get("net_rate_20d")
    if rate is not None and abs(rate) > 1.0:
        out.append(_row("high", "资金流金额超过成交额", f"大资金净额占成交额达到 {text.get('fr.net_rate_20d', '')}，超出合理范围，数据口径可疑。", ["fr.net_rate_20d"], ids))

    ret, big, rate = d.get("ret_5d"), d.get("big_net_5d"), d.get("net_rate_5d")
    if ret is not None and big is not None and rate is not None and abs(rate) >= 0.01:
        if ret > 0.03 and big < 0:
            out.append(_row("high", "上涨，但大资金在净流出", f"近 5 日股价 {text['fr.ret_5d']}，大资金同期净额 {text['fr.big_net_5d']}，价格和资金方向相反。", ["fr.ret_5d", "fr.big_net_5d", "fr.net_rate_5d"], ids))
        elif ret < -0.03 and big > 0:
            out.append(_row("medium", "下跌，但大资金在净流入", f"近 5 日股价 {text['fr.ret_5d']}，大资金同期净额 {text['fr.big_net_5d']}，可能有资金承接，也可能只是下跌途中的反复。", ["fr.ret_5d", "fr.big_net_5d", "fr.net_rate_5d"], ids))

    z = d.get("last_day_z")
    if z is not None and abs(z) >= 2.5:
        word = "流入" if z > 0 else "流出"
        out.append(_row("high" if abs(z) >= 3.5 else "medium", f"最近一天大资金{word}异常大", f"最近一个交易日大资金净额 {text.get('fr.last_day_net', '')}，偏离近 60 日均值 {text['fr.last_day_z']}。", ["fr.last_day_net", "fr.last_day_z"], ids))
    trend = d.get("trend")
    if trend == "turning_out":
        out.append(_row("medium", "大资金由流入转为流出", "近期大资金方向出现反转，前几天还在流入，最近转为流出。", ["fr.trend"], ids))
    elif trend == "accelerating_out":
        out.append(_row("high", "大资金流出在加快", "大资金连续净流出，并且力度越来越大。", ["fr.trend", "fr.streak"], ids))

    hints = d.get("hints") or []
    if hints:
        out.append(_row("high" if len(hints) >= 2 else "medium", "出现位置提示：" + "、".join(hints), "这些是高位常见的风险迹象，需要结合位置一起看。", ["fr.hints", "fr.from_year_high"], ids))
    rel = d.get("rel_vol_5d") if d.get("rel_vol_5d") is not None else d.get("rel_vol")
    rel_id = "fr.rel_vol_5d" if d.get("rel_vol_5d") is not None else "fr.rel_vol"
    if rel is not None and rel >= 2.5:
        out.append(_row("medium", "成交量异常放大", f"相对量能达到此前 60 日均量的 {text.get(rel_id, '')}。", [rel_id], ids))

    if d.get("limit_days"):
        out.append(_row("medium", "窗口内有疑似涨跌停", f"近 20 日有 {text['fr.limit_days']}涨幅或跌幅接近涨跌停，那几天的成交和资金流不能直接当常规数据看。", ["fr.limit_days", "fr.limit_last"], ids))
    if d.get("zero_volume_days"):
        out.append(_row("medium", "窗口内疑似停牌", f"近 20 日有 {text['fr.zero_volume_days']}没有成交。", ["fr.zero_volume_days"], ids))

    margin = d.get("margin_buy_pct")
    if margin is not None and margin >= 0.2:
        out.append(_row("medium", "融资买入占比偏高", f"近 5 日融资买入额占成交额 {text['fr.margin_buy_pct']}，杠杆资金参与度高，涨跌可能被放大。", ["fr.margin_buy_pct"], ids))
    if d.get("toplist_days", 0) >= 3:
        out.append(_row("medium", "近期频繁上龙虎榜", f"近 20 个交易日上榜 {text['fr.toplist_days']}，短线资金博弈明显。", ["fr.toplist_days", "fr.toplist_net"], ids))
    inst = d.get("toplist_inst_net")
    if inst is not None and abs(inst) >= 0.5:
        word = "净买入" if inst > 0 else "净卖出"
        out.append(_row("medium", f"龙虎榜上机构席位{word}", f"上榜日机构席位合计 {text['fr.toplist_inst_net']}。", ["fr.toplist_inst_net", "fr.toplist_days"], ids))

    rank = {"high": 0, "medium": 1}
    out.sort(key=lambda row: rank[row["severity"]])
    return out
