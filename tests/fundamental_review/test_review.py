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

    def test_concepts_list_membership_and_chain_only_when_the_name_says_so(self):
        from app.fundamental_review import data

        sector_payload = {"sectors": {"candidates": [
            {"type": "concept", "belongs": True, "name": "锂电负极材料", "score": 0.9, "business": {"share": 0.41, "items": ["负极材料"]}},
            {"type": "concept", "belongs": True, "name": "存储芯片", "score": 0.4, "business": {"share": 0.2, "items": ["DRAM"]}},
            {"type": "concept", "belongs": False, "name": "不该出现", "score": 5, "business": {}},
            {"type": "industry", "belongs": True, "name": "半导体", "score": 1, "business": {}},
        ]}}
        concepts = data.concepts_of(sector_payload)
        self.assertEqual([row["name"] for row in concepts], ["锂电负极材料", "存储芯片"])
        self.assertEqual(data.chain_of(concepts), [{"name": "锂电负极材料", "position": "材料"}])
        ctx = context()
        ctx["sector"] = {"stage": "main", "concepts": concepts, "chain": data.chain_of(concepts)}
        facts, derived = fact_sheet.build(payload(), ctx)
        text = {fact.id: fact.text for fact in facts}
        self.assertIn("锂电负极材料，业务占比 41%", text["fd.concepts"])
        self.assertIn("存储芯片", text["fd.concepts"])
        self.assertNotIn("不该出现", text["fd.concepts"])
        self.assertNotIn("半导体", text["fd.concepts"])
        self.assertEqual(text["fd.chain"], "锂电负极材料属于材料环节")
        rows = {row["key"]: row for row in template.answers(derived, text)}
        self.assertIn("判定属于的概念", rows["q1"]["answer"])
        self.assertIn("fd.concepts", rows["q1"]["cites"])
        self.assertIn("fd.chain", rows["q1"]["cites"])

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
        answers = {
            "q1": ("最大业务线占营收 84.2%。", ["fd.biz_top"]),
            "q2": ("处在 29% 分位。", ["fd.pctile_5y"]),
            "q3": ("低于同行中位数 23%。", ["fd.vs_peer"]),
            "q4": ("利润没有让市盈率失真。", ["fd.distorted"]),
            "q5": ("质量档位较好。", ["fd.q_level"]),
            "q6": ("没有明显弱项。", ["fd.q_weakest"]),
            "q7": ("近期没有股东动作。", ["fd.hold_none"]),
            "q8": ("近期没有集中到来的压力。", ["fd.hold_none"]),
            "q9": ("板块处在主升阶段。", ["fd.sector_stage"]),
            "q10": ("同行 11 家。", ["fd.peer_count"]),
            "q11": ("处在 29% 分位，整体偏正面。", ["fd.pctile_5y"]),
        }
        missing = set(view.get("must_be_missing") or [])
        out = {
            "items": [
                {"key": key, "status": "support", "answer": text, "cites": cites}
                for key, (text, cites) in answers.items() if key not in missing
            ],
            "lean": "偏多",
            "ai_text": "估值处在近 5 年偏低的位置，底子在同行里靠前。",
            "cited_fact_ids": ["fd.pctile_5y", "fd.q_level"],
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
        self.assertIsNone(result["headline"])
        self.assertEqual([row["key"] for row in result["items"]], [f"q{i}" for i in range(1, 12)])
        self.assertIsNone(result["profile"])
        self.assertEqual(result["as_of"], "2026-10-30")
        self.assertGreater(len(result["facts"]), 25)
        self.assertIn("cites", result["items"][0])
        self.assertTrue(all(row["source"] == "template" for row in result["items"]))

    def test_gateway_is_ignored_and_template_summary_can_be_headline(self):
        model = ScriptedModel()
        result = self.build(model)
        self.assertEqual(model.calls, 0)
        self.assertEqual(result["source"], "template")
        self.assertIsNone(result.get("headline"))
        shown = review.headline_from_review(result, allow_template=True)
        self.assertEqual(shown["source"], "rule")
        self.assertTrue(shown["text"])

    def test_same_facts_hit_the_cache_and_refresh(self):
        store = Store(":memory:")
        first = self.build(store=store)
        second = self.build(store=store)
        self.assertEqual((first["cached"], second["cached"]), (False, True))
        third = self.build(store=store, refresh=True)
        self.assertFalse(third["cached"])

    def test_prompt_carries_facts_questions_and_profile_rules(self):
        self.assertIn("cited_fact_ids", prompt.SYSTEM)
        self.assertIn("profile", prompt.SYSTEM)
        self.assertIn("不要出现任何金额、比例、日期、排名等具体数字", prompt.SYSTEM)
        result = self.build()
        self.assertTrue(any(row["id"] == "fd.pctile_5y" for row in result["facts"]))


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
        self.skipTest("详情不再依赖 attach 轮询模型")

class PromptVersionTest(unittest.TestCase):
    def test_a_review_from_an_older_prompt_is_not_treated_as_fresh(self):
        self.skipTest("详情不再依赖 attach 轮询模型")

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
        self.assertEqual(body["review_state"], "ready")
        self.assertEqual(body["review"]["source"], "template")
        self.assertIsNone(body.get("headline"))
        self.assertFalse(body.get("analyst_failed"))

    def test_stream_saves_template_headline(self):
        from app.api import v1
        with patch("app.api.v1._notify"), patch("app.synthesis.worker.get_store", return_value=self.store):
            page = v1._fundamental_page(make_stock(), wait=True)
            detail = v1._fundamental_page(make_stock(), wait=False)
        self.assertEqual(page["headline"]["source"], "rule")
        self.assertTrue(page["headline"]["text"])
        self.assertFalse(page["analyst_failed"])
        self.assertEqual(detail["headline"]["text"], page["headline"]["text"])

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
