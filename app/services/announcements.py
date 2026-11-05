"""公告的规则分类：重要程度、类别、主题、方向提示。纯规则，不调模型。设计 6.6。

direction_hint 只是「标题提到了什么」，不是结论；页面上要写成「标题提到 X」，不写利好利空。
真正的方向和量级由客户端的新闻分析师在夜间复判。
"""

from __future__ import annotations

import hashlib
import re

# (关键词, topic, category, 方向提示)；按重要程度分层，先匹配 P0
P0 = (
    ("立案", "立案调查", "风险与治理", "利空"),
    ("行政处罚", "行政处罚", "风险与治理", "利空"),
    ("处罚", "处罚", "风险与治理", "利空"),
    ("监管函", "监管函", "风险与治理", "利空"),
    ("警示函", "警示函", "风险与治理", "利空"),
    ("问询函", "交易所问询", "风险与治理", "利空"),
    ("关注函", "交易所关注函", "风险与治理", "利空"),
    ("退市风险", "退市风险", "风险与治理", "利空"),
    ("终止上市", "终止上市", "风险与治理", "利空"),
    ("风险警示", "风险警示", "风险与治理", "利空"),
    ("重大诉讼", "重大诉讼", "风险与治理", "利空"),
    ("业绩预亏", "业绩预亏", "业绩", "利空"),
    ("预亏", "业绩预亏", "业绩", "利空"),
    ("首亏", "业绩预亏", "业绩", "利空"),
    ("控制权变更", "控制权变更", "资本", "中性"),
    ("实际控制人变更", "控制权变更", "资本", "中性"),
    ("控股股东变更", "控制权变更", "资本", "中性"),
    ("重大资产重组", "重大重组", "资本", "中性"),
    ("重大重组", "重大重组", "资本", "中性"),
    ("停牌", "停牌", "资本", "中性"),
    ("复牌", "复牌", "资本", "中性"),
)
P1 = (
    ("业绩预增", "业绩预增", "业绩", "利好"),
    ("预增", "业绩预增", "业绩", "利好"),
    ("扭亏", "扭亏为盈", "业绩", "利好"),
    ("预减", "业绩预减", "业绩", "利空"),
    ("业绩快报", "业绩快报", "业绩", "中性"),
    ("业绩预告", "业绩预告", "业绩", "中性"),
    ("年度报告", "定期报告", "业绩", "中性"),
    ("半年度报告", "定期报告", "业绩", "中性"),
    ("季度报告", "定期报告", "业绩", "中性"),
    ("年报", "定期报告", "业绩", "中性"),
    ("回购", "股份回购", "资本", "利好"),
    ("增持", "股东增持", "资本", "利好"),
    ("减持", "股东减持", "资本", "利空"),
    ("重大合同", "重大合同", "经营", "利好"),
    ("中标", "中标", "经营", "利好"),
    ("签订", "合同签订", "经营", "中性"),
    ("收购", "并购", "资本", "中性"),
    ("并购", "并购", "资本", "中性"),
    ("发行股份", "再融资", "资本", "中性"),
    ("定向增发", "再融资", "资本", "中性"),
    ("可转换公司债券", "再融资", "资本", "中性"),
    ("配股", "再融资", "资本", "中性"),
    ("利润分配", "分红送转", "资本", "利好"),
    ("分红", "分红送转", "资本", "利好"),
    ("权益分派", "分红送转", "资本", "利好"),
    ("股权激励", "股权激励", "资本", "利好"),
    ("员工持股", "员工持股", "资本", "利好"),
    ("诉讼", "诉讼", "风险与治理", "利空"),
    ("仲裁", "仲裁", "风险与治理", "利空"),
    ("解除限售", "限售解禁", "资本", "利空"),
)
P2 = (
    ("监事会", "监事会决议", "经营", "中性"),
    ("董事会", "董事会决议", "经营", "中性"),
    ("会议决议", "董事会决议", "经营", "中性"),
    ("股东大会", "股东大会", "经营", "中性"),
    ("独立董事", "独立董事意见", "经营", "中性"),
    ("法律意见", "律师意见", "经营", "中性"),
    ("关联交易", "日常关联交易", "经营", "中性"),
)
LEVELS = (("P0", P0), ("P1", P1), ("P2", P2))
RANK = {"P0": 0, "P1": 1, "P2": 2}


def classify(title: str) -> dict:
    """返回 {importance, category, topic, direction_hint, matched}。没有匹配的归为 P2 经营。"""
    text = re.sub(r"\s+", "", title or "")
    for level, rules in LEVELS:
        hits = [(word, topic, category, hint) for word, topic, category, hint in rules if word in text]
        if not hits:
            continue
        word, topic, category, hint = hits[0]
        if word == "诉讼" and "重大" in text:           # 「重大诉讼」已在 P0 里先匹配到，这里是普通诉讼
            pass
        if hint == "利好" and ("终止" in text or "取消" in text or "未能" in text or "失败" in text):
            hint = "中性"                                 # 「终止回购」「终止股权激励」不是利好
        if topic == "股份回购" and ("注销" in text and "回购" not in text):
            hint = "中性"
        return {"importance": level, "category": category, "topic": topic, "direction_hint": hint, "matched": [h[0] for h in hits][:4]}
    return {"importance": "P2", "category": "经营", "topic": "", "direction_hint": "中性", "matched": []}


def item_id(source: str, url: str, title: str, published_at: str) -> str:
    """同一条公告不论从哪个来源抓到，id 都一样：来源 + 链接 + 规范化标题 + 发布时间。"""
    norm = re.sub(r"\s+", "", title or "")
    return hashlib.sha1("|".join((source or "", url or "", norm, published_at or "")).encode("utf-8")).hexdigest()[:20]


def enrich(ts_code: str, raw: list[dict]) -> list[dict]:
    """原始条目（fetch_announcements 的输出）→ 带分类和 item_id 的条目，按发布时间升序，同 id 去重。"""
    seen, out = set(), []
    for row in raw:
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        published = str(row.get("time") or "")[:19]
        iid = item_id(row.get("source", "exchange"), row.get("url", ""), title, published)
        if iid in seen:
            continue
        seen.add(iid)
        out.append({"item_id": iid, "ts_code": ts_code, "title": title, "url": row.get("url", ""), "source": row.get("source_name") or row.get("source") or "交易所公告",
                    "source_grade": "A", "published_at": published, **classify(title)})
    return sorted(out, key=lambda item: (item["published_at"], item["item_id"]))
