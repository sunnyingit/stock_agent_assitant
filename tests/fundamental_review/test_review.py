import json
import unittest
from unittest.mock import patch

from flask import Flask

from synthesis.fixtures import NOW, make_stock
from app.api.v1 import bp as v1_bp
from app.fundamental_review import facts as fact_sheet, prompt, review, template, validate
from app.fundamental_review.api import bp as review_bp
from app.reviews import attach
from app.synthesis import worker
from app.synthesis.gateway import Gateway
from app.synthesis.store import Store

TS = "600001.SH"
SECTOR = {"stage": "main"}
NEWS = [{"title": "行业新规出台", "published_at": "2026-10-29T10:00:00+08:00", "category": "industry", "source_grade": "B"}]


def quality_items(good=True):
    pct = {"profitability": 0.95, "growth": 0.7, "solvency": 0.8, "cash": 0.9, "payout": 0.75} if good else \
        {"profitability": 0.1, "growth": 0.05, "solvency": 0.3, "cash": 0.2, "payout": 0.15}
    return [{"key": key, "metric_text": key, "value": 0.18, "value_text": "18.0%", "peer_pctile": value, "counts": True} for key, value in pct.items()]


def payload(good=True, events=None, business=True, distorted=False):
    cheap = good
    return {
        "ts_code": TS, "stock_name": "测试公司",
        "meta": {"state": "ok", "as_of": "2026-10-30", "price_date": "2026-10-30", "report_period": "2026H1",
                 "industry": {"name": "白酒Ⅲ", "peer_count": 11}},
        "valuation": {
            "metric": "pe_ttm_deducted", "metric_text": "市盈率（TTM，扣非）", "metric_reason": "利润处在低位，市盈率失真" if distorted else None,
            "value": 21.4 if cheap else 48.0, "pctile_5y": 0.29 if cheap else 0.85, "peer_median": 27.8 if cheap else 30.0,
            "history": {"dates": ["2021-11-05"] * 260, "p20": 20.3, "p50": 27.5, "p80": 38.3, "values": []},
        },
        "quality": {"items": quality_items(good), "weakest": None if good else {"key": "growth", "text": "成长性处于行业后 5%"}},
        "cells": [{"key": "valuation", "level": "cheap" if cheap else "expensive"}, {"key": "quality", "level": "strong" if good else "weak"},
                  {"key": "holders", "level": "quiet" if not events else "negative"}],
        "holder_events": events or [],
        "inputs": {
            "ocf_negative_years": 0, "red_line": False, "distorted": distorted, "periods_behind": 0,
            "business": {"main_business": "白酒的生产与销售", "period": "2026-06-30", "items": [
                {"item": "茅台酒", "sales_yi": 777.2, "share": 0.8423, "yoy": 0.0282, "gross_margin": 0.9228},
                {"item": "其他系列酒", "sales_yi": 129.3, "share": 0.1402, "yoy": None, "gross_margin": 0.7359},
            ]} if business else None,
        },
    }


def context(sector=SECTOR, news=NEWS):
    return {"sector": sector, "news": news, "missing": []}


def unlock_event():
    return {"type": "unlock", "impact": "negative", "title": "限售股解禁", "detail_text": "解禁占流通股 3%", "days_left": 8}


class FactsTest(unittest.TestCase):
    def test_every_group_is_built_with_readable_text(self):
        facts, d = fact_sheet.build(payload(), context())
        text = {fact.id: fact.text for fact in facts}
        self.assertEqual(text["fd.pctile_5y"], "29% 分位")
        self.assertEqual(text["fd.value"], "21.4 倍")
        self.assertIn("低于同行中位数 23%", text["fd.vs_peer"])
        self.assertIn("茅台酒 777.2 亿元，占 84.2%", text["fd.biz_lines"])
        self.assertEqual(text["fd.biz_top"], "茅台酒，占营收 84.2%")
        self.assertEqual(text["fd.sector_stage"], "主升")
        self.assertIn("行业新规出台", text["fd.industry_news_1"])
        self.assertEqual(text["fd.peer_count"], "11 家")
        groups = {row["group"] for row in fact_sheet.public(facts)}
        self.assertEqual(groups, set(fact_sheet.GROUP_ORDER))
        self.assertEqual(d["peer_count"], 11)

    def test_missing_inputs_leave_facts_out_instead_of_inventing_them(self):
        facts, d = fact_sheet.build(payload(business=False), context(sector=None, news=None))
        ids = {fact.id for fact in facts}
        self.assertNotIn("fd.biz_lines", ids)
        self.assertNotIn("fd.sector_stage", ids)
        self.assertNotIn("fd.industry_news_count", ids)


class TemplateTest(unittest.TestCase):
    def answers(self, **kwargs):
        facts, d = fact_sheet.build(payload(**kwargs), context())
        return {row["key"]: row for row in template.answers(d, {fact.id: fact.text for fact in facts})}

    def test_healthy_company(self):
        rows = self.answers()
        self.assertEqual(len(rows), 11)
        self.assertEqual([rows[k]["status"] for k in ("q1", "q2", "q3", "q4", "q5", "q6", "q8", "q9", "q10")],
                         ["neutral", "support", "support", "support", "support", "support", "support", "support", "support"])
        self.assertEqual(rows["q7"]["status"], "neutral")
        self.assertEqual(rows["q11"]["status"], "support")
        for row in rows.values():
            self.assertTrue(row["answer"])

    def test_expensive_weak_company_with_pressure(self):
        rows = self.answers(good=False, events=[unlock_event()])
        self.assertEqual([rows[k]["status"] for k in ("q2", "q3", "q5", "q6", "q7", "q8")], ["warn"] * 6)
        self.assertEqual(rows["q11"]["status"], "warn")
        self.assertIn("成长性处于行业后 5%", rows["q6"]["answer"])

    def test_distorted_earnings_make_the_metric_untrustworthy(self):
        self.assertEqual(self.answers(distorted=True)["q4"]["status"], "warn")

    def test_no_business_data_is_missing_not_guessed(self):
        self.assertEqual(self.answers(business=False)["q1"]["status"], "missing")


class ScriptedModel:
    def __init__(self, mutate=None, fail=False):
        self.mutate, self.fail, self.calls, self.users = mutate, fail, 0, []

    def complete(self, system, user):
        self.calls += 1
        self.users.append(user)
        if self.fail:
            raise RuntimeError("模型不可用")
        view = json.loads(user)
        missing = set(view["must_be_missing"])
        answers = {"q1": "主营白酒，茅台酒占 84.2%", "q2": "估值在自己历史里是 29% 分位", "q3": "比同行中位数低 23%", "q4": "指标没有失真",
                   "q5": "盈利和现金在同行里靠前", "q6": "没有明显弱项", "q7": "近期没有股东动作", "q8": "近期没有集中到来的压力",
                   "q9": "板块处在主升阶段", "q10": "财报是 2026H1，同行 11 家", "q11": "整体偏正面"}
        cites = {"q1": ["fd.biz_top"], "q2": ["fd.pctile_5y"], "q3": ["fd.vs_peer"], "q4": ["fd.distorted"], "q5": ["fd.qp_profitability"],
                 "q6": ["fd.q_weakest"], "q7": ["fd.holders_level"], "q8": ["fd.holders_level"], "q9": ["fd.sector_stage"],
                 "q10": ["fd.report_period", "fd.peer_count"], "q11": ["fd.pctile_5y"]}
        out = {
            "items": [{"key": k, "status": "support", "answer": text, "cites": cites[k]} for k, text in answers.items() if k not in missing],
            "profile": {
                "partners": [{"name": "某经销商", "relation": "客户", "note": "渠道销售"}],
                "drivers_up": ["高端消费需求回升"], "drivers_down": ["政策收紧"], "link": "最大业务线受高端消费需求影响",
            },
        }
        return self.mutate(out, view) or out if self.mutate else out


class ReviewTest(unittest.TestCase):
    def build(self, model=None, **kwargs):
        store = kwargs.pop("store", Store(":memory:"))
        gateway = Gateway(model) if model is not None else None
        return review.build(make_stock(), store=store, gateway=gateway, now=NOW, payload=payload(), context=context(), model_tag="x", **kwargs)

    def test_without_a_model_the_template_answers_everything_and_there_is_no_profile(self):
        result = self.build()
        self.assertEqual(result["source"], "template")
        self.assertEqual([row["key"] for row in result["items"]], [f"q{i}" for i in range(1, 12)])
        self.assertIsNone(result["profile"])
        self.assertEqual(result["as_of"], "2026-10-30")
        self.assertGreater(len(result["facts"]), 25)
        for key in ("cites",):
            self.assertIn(key, result["items"][0])

    def test_model_answers_replace_templates_and_profile_is_labelled(self):
        result = self.build(ScriptedModel())
        self.assertEqual(result["source"], "model")
        by_key = {row["key"]: row for row in result["items"]}
        self.assertEqual(by_key["q2"]["source"], "model")
        self.assertEqual(by_key["q11"]["answer"], "整体偏正面")
        profile = result["profile"]
        self.assertEqual(profile["source"], "model")
        self.assertIn("未经核对", profile["note"])
        self.assertEqual(profile["partners"], [{"name": "某经销商", "relation": "客户", "note": "渠道销售"}])
        self.assertEqual(profile["drivers_down"], ["政策收紧"])

    def test_a_made_up_number_sends_only_that_item_back_to_the_template(self):
        def lie(out, view):
            out["items"][1]["answer"] = "估值在自己历史里是 90% 分位"
            return out

        result = self.build(ScriptedModel(mutate=lie))
        by_key = {row["key"]: row for row in result["items"]}
        self.assertEqual(by_key["q2"]["source"], "template")
        self.assertEqual(by_key["q3"]["source"], "model")
        self.assertEqual(result["source"], "mixed")
        self.assertTrue(any("数字" in item for item in result["debug"]["errors"]))

    def test_status_on_rule_questions_cannot_be_flipped_but_free_questions_can(self):
        def flip(out, view):
            for row in out["items"]:
                row["status"] = "warn"      # 估值明明偏便宜，模型却写需警惕
            return out

        result = self.build(ScriptedModel(mutate=flip))
        by_key = {row["key"]: row for row in result["items"]}
        self.assertEqual(by_key["q2"]["status"], "support")   # 以规则为准
        self.assertEqual(by_key["q11"]["status"], "warn")     # 总结由模型定
        self.assertIn("q2", result["debug"]["status_overridden"])

    def test_model_failure_falls_back_and_is_not_cached(self):
        store = Store(":memory:")
        result = self.build(ScriptedModel(fail=True), store=store)
        self.assertEqual(result["source"], "template")
        entry = store.get(attach.latest_key(TS, "fund"))
        self.assertGreater(entry["retry_after"], entry["ts"])
        self.assertEqual(self.build(ScriptedModel(), store=store)["source"], "model")   # 上次没缓存，这次重新调用

    def test_same_facts_hit_the_cache_and_refresh_forces_a_call(self):
        store, model = Store(":memory:"), ScriptedModel()
        self.build(model, store=store)
        again = self.build(model, store=store)
        self.assertTrue(again["cached"])
        self.assertEqual(model.calls, 1)
        self.build(model, store=store, refresh=True)
        self.assertEqual(model.calls, 2)

    def test_prompt_carries_facts_questions_and_profile_rules(self):
        model = ScriptedModel()
        self.build(model)
        view = json.loads(model.users[0])
        self.assertIn("fd.pctile_5y", {row["id"] for row in view["facts"]})
        self.assertEqual(view["stock"]["code"], make_stock().code)
        self.assertIn("q9", view["by_question"])
        self.assertIn("q11", prompt.SYSTEM)
        self.assertIn("profile", prompt.SYSTEM)
        self.assertIn("不要出现任何金额、比例、日期、排名等具体数字", prompt.PROFILE_RULES)


class ContextTimeoutTest(unittest.TestCase):
    def test_slow_parts_are_skipped_not_waited_for(self):
        import time

        from app.fundamental_review import data

        def slow(stock):
            time.sleep(0.4)
            return {"stage": "main"}

        with patch.object(data, "_sector", slow), patch.object(data, "_news", lambda stock: NEWS):
            started = time.monotonic()
            out = data.load_context(make_stock(), timeout=0.05)
        self.assertLess(time.monotonic() - started, 0.3)
        self.assertEqual(out["pending"], ["sector"])
        self.assertIsNone(out["sector"])
        self.assertEqual(out["news"], NEWS)
        self.assertIn("板块阶段：数据还在整理", out["missing"][0])

    def test_failures_are_recorded_and_the_rest_still_used(self):
        from app.fundamental_review import data

        def boom(stock):
            raise RuntimeError("取不到")

        with patch.object(data, "_sector", boom), patch.object(data, "_news", lambda stock: []):
            out = data.load_context(make_stock(), timeout=5)
        self.assertEqual((out["pending"], out["news"], out["sector"]), ([], [], None))
        self.assertEqual(out["missing"], ["板块阶段：取不到"])

    def test_a_partial_review_is_regenerated_after_a_short_while(self):
        store = Store(":memory:")
        result = review.build(make_stock(), store=store, payload=payload(), context={**context(sector=None), "missing": ["板块阶段：数据还在整理，这次没用上"], "pending": ["sector"]}, now=NOW)
        self.assertTrue(result["partial"])
        ts = store.get(attach.latest_key(TS, "fund"))["ts"]
        calls = []
        spawn = lambda stock, where: calls.append(1)
        meta = {"meta": {"as_of": "2026-10-30"}}
        self.assertEqual(attach.attach(meta, make_stock(), store, now=ts + 10, spawn=spawn, kind="fund")["review_state"], "ready")
        self.assertEqual(attach.attach(meta, make_stock(), store, now=ts + attach.PARTIAL_SECONDS + 1, spawn=spawn, kind="fund")["review_state"], "refreshing")
        self.assertEqual(calls, [1])
        self.assertFalse(review.build(make_stock(), store=Store(":memory:"), payload=payload(), context=context(), now=NOW)["partial"])


class PromptVersionTest(unittest.TestCase):
    def test_a_review_from_an_older_prompt_is_not_treated_as_fresh(self):
        store = Store(":memory:")
        result = review.build(make_stock(), store=store, payload=payload(), context=context(), now=NOW)
        meta = {"meta": {"as_of": "2026-10-30"}}
        ts = store.get(attach.latest_key(TS, "fund"))["ts"]
        calls = []
        spawn = lambda stock, where: calls.append(1)
        self.assertEqual(attach.attach(meta, make_stock(), store, now=ts + 1, spawn=spawn, kind="fund")["review_state"], "ready")
        with patch.object(prompt, "PROMPT_VERSION", "fund-review-next"):
            out = attach.attach(meta, make_stock(), store, now=ts + 1, spawn=spawn, kind="fund")
        self.assertEqual((out["review_state"], calls), ("refreshing", [1]))
        self.assertEqual(result["debug"]["prompt_version"], "fund-review-2")


class ProfileValidationTest(unittest.TestCase):
    def test_bad_entries_are_dropped_one_by_one(self):
        raw = {"profile": {
            "partners": [
                {"name": "甲公司", "relation": "客户", "note": "销售渠道"},
                {"name": "甲公司", "relation": "供应商", "note": "重复"},
                {"name": "乙公司", "relation": "朋友", "note": "关系不在约定里"},
                {"name": "丙公司", "relation": "供应商", "note": "供货 30 亿元"},
                {"name": "测试公司", "relation": "客户", "note": "就是自己"},
                {"name": "", "relation": "客户", "note": ""},
            ],
            "drivers_up": ["政策支持", "需求增长 20%", "技术突破", "政策支持"],
            "drivers_down": ["目标价下调"], "link": "最大业务线受需求影响",
        }}
        profile, errors = validate.profile(raw, "测试公司")
        self.assertEqual([row["name"] for row in profile["partners"]], ["甲公司"])
        self.assertEqual(profile["drivers_up"], ["政策支持", "技术突破"])
        self.assertEqual(profile["drivers_down"], [])
        self.assertTrue(errors)

    def test_caps_and_empty(self):
        many = {"profile": {"partners": [{"name": f"公司{chr(65 + i)}", "relation": "客户", "note": ""} for i in range(9)], "drivers_up": [], "drivers_down": [], "link": ""}}
        self.assertEqual(len(validate.profile(many)[0]["partners"]), 5)
        self.assertEqual(validate.profile({"profile": {"partners": [], "drivers_up": [], "drivers_down": [], "link": ""}}), (None, []))
        self.assertEqual(validate.profile({}), (None, []))
        self.assertEqual(validate.profile({"profile": "乱写"}), (None, []))


class AttachKindTest(unittest.TestCase):
    def test_fund_and_flow_reviews_are_stored_separately(self):
        store = Store(":memory:")
        attach.remember(store, TS, {"ts_code": TS, "as_of": "2026-10-30"}, kind="fund")
        self.assertIsNone(store.get(attach.latest_key(TS, "flow")))
        self.assertIsNotNone(store.get(attach.latest_key(TS, "fund")))
        out = attach.attach({"meta": {"as_of": "2026-10-30"}}, make_stock(), store, now=store.get(attach.latest_key(TS, "fund"))["ts"] + 1, spawn=lambda *a: None, kind="fund")
        self.assertEqual(out["review_state"], "ready")
        flow = attach.attach({"meta": {"as_of": "2026-10-30"}}, make_stock(), store, spawn=lambda *a: None, kind="flow")
        self.assertEqual(flow["review_state"], "computing")


class RouteTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        worker.set_store(self.store)
        self.addCleanup(worker.set_store, None)
        app = Flask(__name__)
        app.register_blueprint(v1_bp)
        app.register_blueprint(review_bp)
        self.client = app.test_client()
        self.payload = {**payload(), "verdict": {"source": "rule", "status": "support", "status_text": "偏多", "text": "估值偏便宜",
                                                  "lean": "bullish", "level": 1, "certainty": "medium"}}
        stocks = {TS: make_stock()}
        for item in (patch("app.api.v1.resolve_stock", lambda code: stocks.get(code)),
                     patch("app.reviews.api.resolve_stock", lambda code: stocks.get(code)),
                     patch("app.services.fundamental_detail.build_detail", lambda stock, *a, **kw: self.payload),
                     patch("app.fundamental_review.review.load_payload", lambda stock: self.payload),
                     patch("app.fundamental_review.data.load_context", lambda stock: context()),
                     patch.object(Gateway, "from_config", classmethod(lambda cls: Gateway(None)))):   # 不能真的去调模型
            item.start()
            self.addCleanup(item.stop)

    def get(self):
        return self.client.get(f"/api/v1/stocks/{TS}/dimensions/fundamental/detail").get_json()

    def test_detail_shows_the_same_verdict_fields_as_headline_and_hides_internals(self):
        body = self.get()
        self.assertEqual(set(body["verdict"]), {"source", "status", "status_text", "text"})
        self.assertNotIn("inputs", body)
        self.assertIn("cells", body)
        self.assertEqual((body["review"], body["review_state"], body["headline"]), (None, "disabled", None))
        self.assertIn("inputs", self.payload)   # 缓存里的完整结果没被改

    def test_a_generated_review_gives_the_headline(self):
        result = review.build(make_stock(), store=self.store, gateway=Gateway(ScriptedModel()), now=NOW, payload=self.payload, context=context(), model_tag="x")
        body = self.get()
        self.assertEqual(body["review_state"], "ready")
        self.assertEqual(body["headline"], {"source": "model", "status": "support", "status_text": "偏多", "text": "整体偏正面"})
        self.assertNotIn("debug", body["review"])
        self.assertEqual(body["review"]["profile"]["source"], "model")
        self.assertEqual(result["source"], "model")

    def test_review_endpoint_returns_items_facts_and_profile(self):
        body = self.client.get(f"/api/v1/stocks/{TS}/dimensions/fundamental/review").get_json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(len(body["items"]), 11)
        self.assertGreater(len(body["facts"]), 25)
        self.assertNotIn("debug", body)

    def test_insufficient_data_means_no_review(self):
        self.payload = {**self.payload, "meta": {**self.payload["meta"], "state": "insufficient", "status_text": "上市不足一年"}}
        body = self.get()
        self.assertEqual((body["review_state"], body["review_error"]), ("unavailable", "上市不足一年"))


if __name__ == "__main__":
    unittest.main()
