import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import create_app
from app.services.news_detail import _priced, _accepted_facts, _checked_sentence, _claim, _copy, _judge, _release, build_detail, public
from app.tools.stock import Stock


def _stock():
    return Stock("600519.SH", "600519", "贵州茅台", "1.600519", "SH600519", industry="白酒")


def _bars():
    dates = ["20261019", "20261020", "20261021", "20261022", "20261023", "20261026"]
    return [{"trade_date": day, "close": 100 + index, "pct_chg": 1.0, "vol": 100} for index, day in enumerate(dates)]


def _sector():
    return [{"trade_date": row["trade_date"], "pct_change": 0.0} for row in _bars()]


def _articles():
    return [
        {
            "title": "贵州茅台回购30亿，上年营收100亿",
            "summary": "公司公告回购",
            "source_name": "上交所公告",
            "time": "2026-10-26 10:00",
            "url": "https://example.com/a",
        },
        {
            "title": "贵州茅台股东减持",
            "summary": "财联社报道减持",
            "source_name": "财联社",
            "time": "2026-10-26 11:00",
            "url": "https://example.com/b",
        },
        {
            "title": "白酒行业提价讨论",
            "source_name": "财联社",
            "time": "2026-10-26 12:00",
            "url": "https://example.com/c",
        },
    ]


class NewsDetailTest(unittest.TestCase):
    def test_blocks_follow_code_score(self):
        payload = build_detail(
            _stock(),
            3,
            model=False,
            articles=_articles(),
            bars=_bars(),
            sector=_sector(),
            today=date(2026, 10, 27),
        )
        self.assertEqual(payload["meta"]["state"], "ok")
        self.assertEqual(payload["meta"]["as_of"], "2026-10-27")
        self.assertEqual(payload["meta"]["window_days"], 3)
        self.assertEqual(payload["meta"]["rule_version"], "news-1")
        self.assertEqual(payload["meta"]["counts"]["kept"], 3)
        self.assertIn("+08:00", payload["meta"]["updated_at"])
        verdict = payload["verdict"]
        self.assertEqual(verdict["lean"], "unclear")
        self.assertEqual(verdict["level"], 0)
        self.assertIsNone(verdict["certainty"])
        self.assertLessEqual(len(verdict["text"]), 90)
        self.assertIn("不过", verdict["text"])
        self.assertEqual((verdict["source"], verdict["status"], verdict["status_text"]), ("rule", "neutral", "不明"))
        self.assertIsNone(payload["headline"])   # 没有模型写的结论
        self.assertEqual(set(public(payload)["verdict"]), {"source", "status", "status_text", "text"})
        roles = [item["role"] for item in payload["key_events"]]
        self.assertLessEqual(roles.count("counter"), 1)
        self.assertIn("counter", roles)
        self.assertIn("main", roles)
        lead = payload["key_events"][0]
        self.assertEqual(lead["basis"], "fact")
        self.assertEqual(lead["category"], "capital")
        self.assertEqual(lead["scope"], "company")
        self.assertEqual(lead["magnitude"], "large")
        self.assertEqual(lead["publish_session"], "intra")
        self.assertEqual(lead["persistence"], "one_off")
        self.assertEqual(lead["role"], "main")
        self.assertEqual(lead["surprise"], "na")
        self.assertIsNone(lead["summary"])
        self.assertEqual(lead["published_at"], "2026-10-26T10:00:00+08:00")
        self.assertEqual(lead["weight"], 1.2)
        self.assertEqual(payload["reaction"]["priced_in"], "mostly")
        self.assertEqual(payload["reaction"]["today"]["chg"], 0.01)
        self.assertEqual(payload["reaction"]["today"]["text"], "昨日 +1.00%，由事件 1 带动")
        industry = next(item for item in payload["key_events"] if "白酒行业" in item["title"])
        self.assertEqual(industry["scope"], "industry")
        self.assertEqual(industry["impact"], "positive")
        markers = payload["timeline"]["markers"]
        self.assertIn(f"标出{len(markers)}个事件", payload["timeline"]["chart_alt_text"])
        self.assertIn("不构成投资建议", payload["disclaimer_text"])
        self.assertEqual(payload["watch_next"][0]["condition_id"], "grade_a_opposite")
        self.assertIsNone(payload["heat"]["news_count_pctile_60d"])

    def test_after_close_marker_uses_next_session(self):
        articles = [{
            "title": "贵州茅台回购公告",
            "source_name": "上交所公告",
            "time": "2026-10-23 18:00",
            "url": "https://example.com/after",
        }]
        payload = build_detail(
            _stock(), 3, model=False, articles=articles, bars=_bars(), sector=_sector(), today=date(2026, 10, 27),
        )
        self.assertEqual(payload["timeline"]["markers"][0]["date"], "2026-10-26")

    def test_price_hike_is_low_certainty_company_event(self):
        articles = [
            {
                "title": "茅台提价，为何暖不到其他高端白酒？",
                "summary": "媒体讨论提价",
                "source": "eastmoney",
                "source_name": "东方财富",
                "time": "2026-10-26 18:04",
                "url": "https://example.com/hike",
            },
            {
                "title": "白酒三季报本周密集出炉",
                "source": "eastmoney",
                "source_name": "东方财富",
                "time": "2026-10-26 09:00",
                "url": "https://example.com/preview",
            },
            {
                "title": "红利资产走势强劲",
                "source": "eastmoney",
                "source_name": "东方财富",
                "time": "2026-10-26 10:00",
            },
            {
                "title": "贵州茅台",
                "source": "xueqiu",
                "url": "https://xueqiu.com/s/SH600519",
                "time": "",
            },
        ]
        bars = _bars() + [{
            "trade_date": "20261027", "close": 1356.87, "pct_chg": 0.51, "vol": 100, "intraday": True,
        }]
        sector = _sector() + [{"trade_date": "20261027", "pct_change": 0.0}]
        payload = build_detail(
            _stock(), 3, model=False, articles=articles, bars=bars, sector=sector, today=date(2026, 10, 27),
        )
        self.assertEqual(payload["meta"]["state"], "ok")
        self.assertEqual(payload["meta"]["as_of"], "2026-10-27")
        self.assertEqual(payload["meta"]["counts"]["found"], 4)
        self.assertEqual(payload["meta"]["counts"]["kept"], 2)
        verdict = payload["verdict"]
        self.assertEqual(verdict["lean"], "unclear")
        self.assertEqual(verdict["level"], 0)
        self.assertIsNone(verdict["certainty"])
        self.assertNotIn("丨", verdict["text"])
        self.assertIn("公告", verdict["text"])
        self.assertEqual(len(payload["key_events"]), 2)
        lead = next(item for item in payload["key_events"] if "提价" in item["title"])
        self.assertIn("提价", lead["title"])
        self.assertEqual(lead["basis"], "forecast")
        self.assertEqual(lead["category"], "operations")
        self.assertEqual(lead["scope"], "company")
        self.assertEqual(lead["magnitude"], "small")
        self.assertEqual(lead["publish_session"], "post")
        self.assertEqual(lead["persistence"], "one_off")
        self.assertEqual(lead["role"], "main")
        self.assertEqual(lead["surprise"], "na")
        self.assertIsNone(lead["summary"])
        self.assertEqual(lead["published_at"], "2026-10-26T18:04:00+08:00")
        self.assertEqual(lead["weight"], 0.24)
        self.assertEqual(payload["timeline"]["markers"][0]["date"], "2026-10-27")
        self.assertIn(f"标出{len(payload['timeline']['markers'])}个事件", payload["timeline"]["chart_alt_text"])
        preview = next(item for item in payload["key_events"] if "三季报" in item["title"])
        self.assertEqual(preview["scope"], "industry")
        self.assertIsNotNone(preview["impact"])
        self.assertEqual(payload["reaction"]["today"]["chg"], 0.0051)

    def test_no_company_event_is_unclear(self):
        articles = [{
            "title": "白酒三季报本周密集出炉",
            "source": "eastmoney",
            "source_name": "东方财富",
            "time": "2026-10-26 09:00",
        }]
        payload = build_detail(
            _stock(), 3, model=False, articles=articles, bars=_bars(), sector=_sector(), today=date(2026, 10, 27),
        )
        self.assertEqual(payload["meta"]["state"], "no_news")
        self.assertEqual(payload["verdict"]["lean"], "unclear")
        self.assertEqual(payload["verdict"]["level"], 0)
        self.assertIsNone(payload["verdict"]["certainty"])
        self.assertEqual(len(payload["key_events"]), 1)
        self.assertEqual(payload["key_events"][0]["scope"], "industry")
        self.assertEqual(payload["key_events"][0]["impact"], "neutral")
        self.assertIn("公告", payload["verdict"]["text"])
        self.assertIn("不过", payload["verdict"]["text"])

    def test_model_sentence_is_checked_against_the_cited_articles(self):
        articles = [{"id": "N1", "title": "贵州茅台前三季度净利644.67亿元", "summary": "同比减少0.25%"}]
        good = {"ai_text": "前三季度净利644.67亿元，同比减少0.25%。", "cited_article_ids": ["N1"]}
        self.assertEqual(_checked_sentence(good, articles), {"ai_text": good["ai_text"]})
        self.assertEqual(_checked_sentence(dict(good, ai_text="净利650亿元。"), articles), {"ai_text": "净利650亿元。"})
        self.assertEqual(_checked_sentence(dict(good, cited_article_ids=["N9"]), articles), {})                          # 引用的文章不存在
        self.assertEqual(_checked_sentence(dict(good, cited_article_ids=[]), articles), {})                              # 没有引用
        self.assertEqual(_checked_sentence(dict(good, ai_text="净利644.67亿元。建议买入。"), articles), {})               # 禁用词
        self.assertEqual(_checked_sentence(None, articles), {})

    def test_copy_returns_both_the_template_and_the_model_sentence(self):
        template, model_text = _copy([], {"ai_text": "模型的句子。不过有变数。"}, _stock(), 3)
        self.assertIn("不过", template)
        self.assertEqual(model_text, "模型的句子。不过有变数。")
        self.assertIsNone(_copy([], {}, _stock(), 3)[1])

    def test_public_keeps_only_the_shared_verdict_fields(self):
        payload = {"verdict": {"source": "rule", "status": "neutral", "status_text": "不明", "text": "x", "lean": "unclear", "level": 0, "certainty": None}, "headline": None}
        self.assertEqual(set(public(payload)["verdict"]), {"source", "status", "status_text", "text"})
        self.assertIsNone(public({"verdict": None, "headline": None})["verdict"])

    def test_model_labels_replace_the_keyword_rules(self):
        # 关键词规则会把"回购"判成利好；模型看清了这条是"回购金额减少"，判为利空
        import json

        class Model:
            def __init__(self):
                self.users = []

            def complete(self, system, user):
                self.users.append(user)
                buyback = next(row["id"] for row in json.loads(user)["articles"] if "回购" in row["title"])
                return {"events": [
                    {"article_id": buyback, "event": "公司回购金额同比减少", "category": "资本", "scope": "公司", "basis": "事实",
                     "direction": "利空", "persistence": "一次性", "magnitude": "大", "amount": "30亿", "base": "100亿", "numbers": ["30", "999"], "reason": "回购少于上年"},
                    {"article_id": "N99", "event": "不存在的新闻", "category": "资本", "direction": "利好"},
                ], "lean": "利空", "ai_text": "公司回购30亿元。不过金额低于上年。", "cited_article_ids": [buyback]}

        model = Model()
        payload = build_detail(_stock(), 3, model=model, articles=_articles(), bars=_bars(), sector=_sector(), today=date(2026, 10, 27))
        self.assertEqual(len(model.users), 1)
        self.assertNotEqual(payload["meta"]["model_version"], "template")
        self.assertEqual(payload["verdict"]["lean"], "bearish")
        self.assertEqual(payload["headline"]["text"], "公司回购30亿元。不过金额低于上年。")
        self.assertEqual(payload["headline"]["facts"][0]["label"], "贵州茅台回购30亿，上年营收100亿")
        buyback_id = next(row["id"] for row in json.loads(model.users[0])["articles"] if "回购" in row["title"])
        self.assertEqual(payload["headline"]["facts"][0]["id"], buyback_id)
        self.assertEqual(payload["headline"]["status_text"], "偏空")
        self.assertEqual(len(payload["key_events"]), 1)   # 只有模型抽取并通过核对的那一条

    def test_model_failure_or_empty_events_fall_back_to_the_keyword_rules(self):
        class Broken:
            def complete(self, system, user):
                raise RuntimeError("boom")

        class Empty:
            def complete(self, system, user):
                return {"events": [], "ai_text": "x。不过y。", "cited_article_ids": []}

        for model in (Broken(), Empty()):
            payload = build_detail(_stock(), 3, model=model, articles=_articles(), bars=_bars(), sector=_sector(), today=date(2026, 10, 27))
            self.assertEqual(payload["meta"]["model_version"], "template")
            self.assertIsNone(payload["headline"])
            self.assertEqual(payload["verdict"]["lean"], "unclear")

    def test_accepted_facts_checks_ids_duplicates_and_numbers(self):
        articles = [{"id": "N1", "title": "回购30亿元", "summary": "上年100亿元"}, {"id": "N2", "title": "别的", "summary": ""}]
        rows = [
            {"article_id": "N1", "event": "回购", "category": "资本", "direction": "利好", "amount": "30亿元", "base": "999亿元", "numbers": ["30", "7"]},
            {"article_id": "N1", "event": "重复", "category": "资本", "direction": "利空"},
            {"article_id": "N7", "event": "没有这条新闻"},
            "not a dict",
        ]
        facts = _accepted_facts(rows, articles)
        self.assertEqual(len(facts), 1)
        self.assertEqual((facts[0]["id"], facts[0]["direction"], facts[0]["amount"], facts[0]["base"], facts[0]["numbers"]), ("FN1", "利好", "30亿元", "", ["30"]))
        self.assertEqual(_accepted_facts(None, articles), [])

    def test_dates_and_non_money_numbers_are_not_amounts_or_bases(self):
        text = "持股金额由499.98万元增加至565.22万元，截至2026年6月30日股东户数28.4万户"
        articles = [{"id": "N1", "title": "股东户数", "summary": text}]
        row = {"article_id": "N1", "event": "股东户数减少", "amount": "499.98万元；565.22万元", "base": "2026年6月30日"}
        fact = _accepted_facts([row], articles)[0]
        self.assertEqual((fact["amount"], fact["base"]), ("499.98万元；565.22万元", ""))
        for bad in ("28.4万户", "28.4", "2026", ""):
            self.assertEqual(_accepted_facts([dict(row, base=bad)], articles)[0]["base"], "")

    def test_without_a_model_nothing_is_judged_and_rules_take_over(self):
        self.assertIsNone(_judge(self._prepared(), False, True, _stock()))

    def _prepared(self, articles=None):
        from datetime import datetime

        from app.agents.news.clean import annotate

        rows, _ = annotate(articles or _articles(), _stock(), today=datetime(2026, 10, 27, 16, 0), trade_dates=[row["trade_date"] for row in _bars()], window_days=3)
        for index, row in enumerate(rows, start=1):
            row.setdefault("id", f"N{index}")
        return rows

    def test_model_does_the_cleaning_merge_skip_and_leftovers(self):
        import json

        articles = _articles() + [
            {"title": "回购30亿，上年营收100亿（转载）", "summary": "同一份公告", "source_name": "财联社", "time": "2026-10-26 10:30", "url": "https://example.com/d"},
            {"title": "晚间新闻精选", "summary": "多家公司", "source_name": "财联社", "time": "2026-10-26 22:00", "url": "https://example.com/e"},
            {"title": "董事会核查意见", "summary": "", "source_name": "上交所公告", "time": "2026-10-26 09:00", "url": "https://example.com/f"},
            {"title": "没人处理的一条", "summary": "", "source_name": "财联社", "time": "2026-10-26 13:00", "url": "https://example.com/g"},
        ]
        seen = {}

        class Model:
            def complete(self, system, user):
                rows = json.loads(user)["articles"]
                seen["titles"] = [row["title"] for row in rows]
                by = {row["title"]: row["id"] for row in rows}
                return {
                    "events": [{"article_id": by["贵州茅台回购30亿，上年营收100亿"], "also_in": [by["回购30亿，上年营收100亿（转载）"], "N99", by["贵州茅台回购30亿，上年营收100亿"]],
                                "event": "回购30亿元", "category": "资本", "scope": "公司", "basis": "事实", "direction": "利好", "amount": "30亿元", "base": "100亿元", "magnitude": "大"}],
                    "lean": "利好",
                    "skipped": [{"article_id": by["晚间新闻精选"], "reason": "无关"}, {"article_id": by["董事会核查意见"], "reason": "例行披露"},
                                {"article_id": by["贵州茅台股东减持"], "reason": "乱写的原因"}],
                }

        payload = build_detail(_stock(), 3, model=Model(), articles=articles, bars=_bars(), sector=_sector(), today=date(2026, 10, 27))
        # 没有任何规则预先丢弃：晚间精选、核查意见这类也交给了模型
        self.assertIn("晚间新闻精选", seen["titles"])
        self.assertIn("董事会核查意见", seen["titles"])
        reasons = {row["title"]: row for row in payload["meta"]["dropped"]}
        self.assertEqual(reasons["晚间新闻精选"]["reason"], "irrelevant")
        self.assertNotIn("董事会核查意见", reasons)
        self.assertEqual(reasons["回购30亿，上年营收100亿（转载）"]["reason"], "duplicate")
        self.assertEqual(reasons["回购30亿，上年营收100亿（转载）"]["into"], "贵州茅台回购30亿，上年营收100亿")
        self.assertEqual(reasons["贵州茅台股东减持"]["reason"], "not_extracted")      # 原因不在约定里，当作模型没处理
        self.assertEqual(reasons["没人处理的一条"]["reason"], "not_extracted")
        self.assertEqual(payload["meta"]["counts"]["found"], 7)
        self.assertEqual(payload["meta"]["counts"]["kept"], 2)
        self.assertEqual(payload["verdict"]["lean"], "bullish")

    def test_model_may_legitimately_skip_everything(self):
        class Model:
            def complete(self, system, user):
                import json

                return {"events": [], "skipped": [{"article_id": row["id"], "reason": "无关"} for row in json.loads(user)["articles"]]}

        payload = build_detail(_stock(), 3, model=Model(), articles=_articles(), bars=_bars(), sector=_sector(), today=date(2026, 10, 27))
        self.assertEqual(payload["meta"]["state"], "no_news")
        self.assertEqual(payload["meta"]["counts"]["kept"], 0)
        self.assertEqual({row["reason"] for row in payload["meta"]["dropped"]}, {"irrelevant"})
        self.assertNotEqual(payload["meta"]["model_version"], "template")

    def test_articles_beyond_the_limit_are_recorded_not_silently_lost(self):
        import json

        from app.services import news_detail

        many = [{"title": f"贵州茅台公告{i}", "summary": "", "source_name": "上交所公告", "time": "2026-10-26 10:00", "url": f"https://example.com/{i}"} for i in range(5)]

        class Model:
            def complete(self, system, user):
                rows = json.loads(user)["articles"]
                return {"events": [{"article_id": rows[0]["id"], "event": "公告", "category": "经营", "scope": "公司", "basis": "事实", "direction": "利好"}], "skipped": []}

        with patch.object(news_detail, "MAX_ARTICLES", 2):
            payload = build_detail(_stock(), 3, model=Model(), articles=many, bars=_bars(), sector=_sector(), today=date(2026, 10, 27))
        reasons = [row["reason"] for row in payload["meta"]["dropped"]]
        self.assertEqual(reasons.count("over_limit"), 3)
        self.assertEqual(payload["meta"]["counts"]["found"], 5)

    def test_company_gossip_counts_as_company_even_if_model_says_industry(self):
        import json

        articles = [
            {"title": "贵州茅台饭局八卦曝光", "summary": "席间谈了提价", "source_name": "财联社", "time": "2026-10-26 10:00", "url": "https://example.com/gossip"},
            {"title": "白酒行业本周景气回升", "summary": "行业需求改善", "source_name": "财联社", "time": "2026-10-26 11:00", "url": "https://example.com/industry"},
        ]

        class Model:
            def complete(self, system, user):
                rows = json.loads(user)["articles"]
                by = {row["title"]: row["id"] for row in rows}
                return {"events": [
                    {"article_id": by["贵州茅台饭局八卦曝光"], "event": "饭局八卦", "category": "传闻与澄清", "scope": "行业", "basis": "传闻", "direction": "利好", "magnitude": "小"},
                    {"article_id": by["白酒行业本周景气回升"], "event": "行业景气", "category": "政策与行业", "scope": "行业", "basis": "事实", "direction": "利好", "magnitude": "小"},
                ], "lean": "利好", "ai_text": "饭局提到提价。", "cited_article_ids": [by["贵州茅台饭局八卦曝光"]]}

        payload = build_detail(_stock(), 3, model=Model(), articles=articles, bars=_bars(), sector=_sector(), today=date(2026, 10, 27))
        scopes = {item["title"]: item["scope"] for item in payload["key_events"]}
        self.assertEqual(scopes["贵州茅台饭局八卦曝光"], "company")
        self.assertEqual(scopes["白酒行业本周景气回升"], "industry")
        self.assertIn("贵州茅台饭局八卦曝光", [item["title"] for item in payload["key_events"]])

    def test_company_events_that_are_all_neutral_are_not_no_news(self):
        class Neutral:
            def complete(self, system, user):
                import json

                rows = json.loads(user)["articles"]
                return {"events": [
                    {"article_id": row["id"], "event": row["title"], "category": "业绩", "scope": "公司", "basis": "事实", "direction": "中性", "magnitude": "小"}
                    for row in rows
                ], "ai_text": "整体看不出方向。不过目前没有看到明确的反方报道。", "cited_article_ids": [rows[0]["id"]]}

        payload = build_detail(_stock(), 3, model=Neutral(), articles=_articles(), bars=_bars(), sector=_sector(), today=date(2026, 10, 27))
        self.assertEqual(payload["meta"]["state"], "ok")                       # 有公司事件，只是方向看不出
        self.assertEqual(payload["verdict"]["status_text"], "不明")
        self.assertEqual(len(payload["key_events"]), 3)
        self.assertTrue(all(item["scope"] == "company" for item in payload["key_events"]))
        notice = payload["watch_next"][0]
        self.assertEqual(notice["condition_id"], "grade_a_opposite")
        self.assertEqual(notice["text"], "出现新的 A 级公告")                  # 已经有 A 级公告（上交所公告那条），等的是新的

    def test_watch_text_depends_on_existing_notice(self):
        from app.services.news_detail import _watch

        self.assertEqual(_watch("unclear")[0]["text"], "出现 A 级公告")
        self.assertEqual(_watch("unclear", True)[0]["text"], "出现新的 A 级公告")
        self.assertEqual(_watch("bullish", True)[0]["text"], "出现 A 级利空公告")

    def test_priced_in_says_unknown_when_it_cannot_be_judged(self):
        good = {"direction": "利好", "reaction": {"pre_excess": 6.0, "day_excess": 1.0}}
        self.assertEqual(_priced(good), "mostly")
        self.assertEqual(_priced({"direction": "利好", "reaction": {"pre_excess": 1.0, "day_excess": -1.0}}), "not_yet")
        self.assertEqual(_priced({"direction": "利空", "reaction": {"pre_excess": -6.0, "day_excess": 1.0}}), "partly")
        # 算不出来：没有主事件、中性事件、前后行情都没有
        self.assertEqual(_priced(None), "unknown")
        self.assertEqual(_priced({"direction": "中性", "reaction": {"pre_excess": 6.0, "day_excess": 1.0}}), "unknown")
        self.assertEqual(_priced({"direction": "利好", "reaction": {"adjustments": []}}), "unknown")
        # 反应日还没有行情（收盘后发布）：市场确实还没反应
        self.assertEqual(_priced({"direction": "利好", "reaction": {"adjustments": [], "pending": True}}), "not_yet")

    def test_no_primary_event_means_unknown_in_the_reaction_block(self):
        payload = build_detail(_stock(), 3, model=False, articles=[], bars=_bars(), sector=_sector(), today=date(2026, 10, 27))
        self.assertEqual(payload["reaction"]["priced_in"], "unknown")

    def test_after_close_without_next_bar_stays_on_chart(self):
        articles = [{
            "title": "贵州茅台回购公告",
            "source_name": "上交所公告",
            "time": "2026-10-26 18:04",
            "url": "https://example.com/late",
        }]
        payload = build_detail(
            _stock(), 3, model=False, articles=articles, bars=_bars(), sector=_sector(), today=date(2026, 10, 27),
        )
        marker = payload["timeline"]["markers"][0]
        self.assertEqual(marker["date"], "2026-10-26")
        self.assertNotIn("note", marker)
        self.assertIn("标出1个事件", payload["timeline"]["chart_alt_text"])
        self.assertIn("100.00", payload["timeline"]["chart_alt_text"])
        self.assertTrue(payload["timeline"]["bars"][0]["is_intraday"] is False)
        self.assertEqual(payload["key_events"][0]["weight"], 0.4)

    def test_stale_company_filings_do_not_set_direction(self):
        articles = [
            {
                "title": "三花智控:关于回购公司A股股份的进展公告",
                "source": "exchange",
                "source_name": "交易所公告",
                "time": "2026-10-08 18:08:36",
                "url": "https://example.com/buyback-1008",
            },
            {
                "title": "三花智控:关于回购公司A股股份的进展公告",
                "source": "exchange",
                "source_name": "交易所公告",
                "time": "2026-10-15 18:44:36",
                "url": "https://example.com/buyback-1015",
            },
            {
                "title": "三花智控:关于变更公司财务总监的公告",
                "source": "exchange",
                "source_name": "交易所公告",
                "time": "2026-10-22 17:13:27",
                "url": "https://example.com/cfo",
            },
            {
                "title": "三花智控:H股公告-翌日披露报表",
                "source": "exchange",
                "source_name": "交易所公告",
                "time": "2026-10-13 19:26:06",
                "url": "https://example.com/routine",
            },
        ]
        dates = ["20261008", "20261009", "20261015", "20261016", "20261022", "20261023", "20261026", "20261027"]
        bars = [{"trade_date": day, "close": 36, "pct_chg": -1.44 if day == "20261009" else 0.2, "vol": 100} for day in dates]
        sector = [{"trade_date": day, "pct_change": 0} for day in dates]
        payload = build_detail(
            Stock("002050.SZ", "002050", "三花智控", "0.002050", "SZ002050", industry="制冷空调设备"),
            3, model=False, articles=articles, bars=bars, sector=sector, today=date(2026, 10, 27),
        )
        self.assertEqual(payload["meta"]["state"], "no_news")
        self.assertEqual(payload["meta"]["counts"]["in_window"], 0)
        self.assertIn("0 条有效", payload["meta"]["status_text"])
        verdict = payload["verdict"]
        self.assertEqual(verdict["lean"], "unclear")
        self.assertEqual(verdict["level"], 0)
        self.assertIsNone(verdict["certainty"])
        self.assertIn("财务总监变更", verdict["text"])
        self.assertIn("10-22", verdict["text"])
        self.assertNotIn("回购公司A", verdict["text"])
        urls = [item["url"] for item in payload["key_events"]]
        self.assertIn("https://example.com/buyback-1008", urls)
        self.assertIn("https://example.com/buyback-1015", urls)
        buyback = next(item for item in payload["key_events"] if item["url"].endswith("1008"))
        self.assertEqual(buyback["in_window"], False)
        self.assertIn("利好当日下跌", buyback["reaction"]["text"])
        self.assertEqual(buyback["evidence"][0]["field"], "counter")
        reasons = {item["reason"] for item in payload["meta"]["dropped"]}
        self.assertNotIn("routine", reasons)

    def test_computing_returns_only_meta(self):
        stock = _stock()
        key = f"news_detail:{stock.ts_code}:3:2026-10-27"
        self.assertTrue(_claim(key))
        try:
            payload = build_detail(stock, 3, today=date(2026, 10, 27))
        finally:
            _release(key)
        self.assertEqual(payload["meta"]["state"], "computing")
        self.assertIsNone(payload["verdict"])
        self.assertIsNone(payload["key_events"])
        self.assertIsNone(payload["disclaimer_text"])


class NewsDetailRouteTest(unittest.TestCase):
    def setUp(self):
        self.client = create_app().test_client()

    def test_days_must_be_positive(self):
        response = self.client.get("/api/v1/stocks/600519.SH/dimensions/news/detail?days=0")
        self.assertEqual(response.status_code, 400)

    @patch("app.api.v1.resolve_stock", return_value=None)
    def test_unknown_stock(self, _resolve):
        response = self.client.get("/api/v1/stocks/000000.SH/dimensions/news/detail")
        self.assertEqual(response.status_code, 404)
