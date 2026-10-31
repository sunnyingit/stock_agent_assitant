import json
import unittest
from datetime import date
from unittest.mock import patch

from flask import Flask

from synthesis.fixtures import NOW
from test_sector_detail import _days, _dual, _sector, _snapshot
from app.api.v1 import bp as v1_bp
from app.reviews import attach
from app.sector_review import checks, outcomes, prompt, review, template
from app.sector_review import facts as fact_sheet
from app.services.sector_detail import build_detail
from app.synthesis import worker
from app.synthesis.gateway import Gateway
from app.synthesis.store import Store
from app.tools.stock import stock_from_code


def stock():
    return stock_from_code("603738", "SH", name="泰晶科技")


def detail(snapshot):
    return build_detail(stock(), snapshot=snapshot, today=date.fromisoformat(snapshot["as_of"]))


def single():
    return detail(_snapshot([_sector("850823.SI", "被动元件", 0.82)], primary="850823.SI"))


def two(secondary_days=None):
    primary = _sector("850823.SI", "被动元件", 0.82)
    secondary = _sector("885843.TI", "华为海思概念股", 0.8, days=secondary_days, type="concept")
    return detail(_snapshot([primary, secondary], primary="850823.SI", secondary="885843.TI", driver=_dual()))


def weak_days():
    days = _days(count=25, late=25, rank=29, breadth=0.05, quiet_rank=29, quiet_breadth=0.05, limit_up=0)
    for row in days:
        row["stock"], row["sector"], row["index"] = -0.015, -0.008, -0.004
    return days


def with_hanging(payload):
    payload["sectors"]["candidates"].append({
        "code": "886001.TI", "name": "先进封装", "type": "concept", "belongs": False, "basis": "联动强但不是核心成员", "score": 0.4,
        "business": {"share": 0.0, "items": []}, "linkage": {"corr60": 0.87}, "position": {"rank": 33, "total": 211, "top_pct": 0.16}, "role": None,
    })
    return payload


def sheet(payload):
    facts, d = fact_sheet.build(payload)
    return facts, d, {fact.id: fact.text for fact in facts}


def by(out, key):
    return next(row for row in out["items"] if row["key"] == key)


class ScriptedModel:
    def __init__(self, mutate=None, fail=False):
        self.mutate, self.fail, self.calls, self.users = mutate, fail, 0, []

    def complete(self, system, user):
        self.calls += 1
        self.users.append(user)
        if self.fail:
            raise RuntimeError("模型不可用")
        view = json.loads(user)
        rows = []
        for question in view["questions"]:
            fact = question["facts"][0] if question["facts"] else None
            cites = [fact["id"]] if fact else []
            need = checks.MUST_CITE.get(question["key"])
            if need and any(f["id"] == need for f in question["facts"]):
                cites = [need]
                fact = next(f for f in question["facts"] if f["id"] == need)
            rows.append({"key": question["key"], "status": question["reference"] or "neutral", "answer": f"依据是 {fact['value']}" if fact else "无", "cites": cites})
        out = {"items": rows}
        return self.mutate(out, view) or out if self.mutate else out


class FactsTest(unittest.TestCase):
    def test_primary_only(self):
        facts, d, text = sheet(single())
        self.assertTrue(all(fact.id.startswith("sp.") for fact in facts))
        self.assertEqual(text["sp.p.name"], "被动元件（申万行业）")
        self.assertIn("业务占比 50%", text["sp.p.basis"])
        self.assertEqual(text["sp.p.rank"], "3/40")
        self.assertEqual(text["sp.driver.type"], "单驱动")
        self.assertFalse(any(fact.id.startswith("sp.s.") for fact in facts))
        self.assertNotIn("sp.consistency", text)

    def test_two_sectors_and_driver(self):
        facts, d, text = sheet(two())
        self.assertEqual(text["sp.s.name"], "华为海思概念股（同花顺概念）")
        self.assertEqual(text["sp.driver.type"], "双驱动")
        self.assertEqual(text["sp.driver.p_share"], "42%")
        self.assertEqual(text["sp.driver.weights"], "主板块 60%，次板块 40%")
        self.assertIn("方向同向", text["sp.consistency"])
        self.assertEqual({row["group"] for row in fact_sheet.public(facts)} - set(fact_sheet.GROUP_ORDER), set())

    def test_hanging_sectors_are_listed_by_linkage(self):
        facts, d, text = sheet(with_hanging(single()))
        self.assertIn("先进封装（相关 0.87", text["sp.hang"])
        self.assertEqual(d["hang"][0]["name"], "先进封装")

    def test_every_question_subset_points_at_real_facts(self):
        facts, d, text = sheet(two())
        subsets = template.relevant(set(text))
        for key in ("q1", "q2", "q4", "q5", "q6", "q7"):
            self.assertTrue(subsets[key], key)
        self.assertNotIn("sp.s.stage", subsets["q5"])


class TemplateTest(unittest.TestCase):
    def answers(self, payload):
        facts, d, text = sheet(payload)
        return {row["key"]: row for row in template.answers(d, text)}

    def test_single_sector(self):
        rows = self.answers(single())
        self.assertEqual(rows["q1"]["status"], "support")
        self.assertEqual(rows["q2"]["status"], "neutral")
        self.assertIn("单板块", rows["q2"]["answer"])
        self.assertEqual(rows["q6"]["status"], "na")
        self.assertEqual(rows["q7"]["status"], "na")
        self.assertEqual(rows["q5"]["status"], "support")        # 主升
        self.assertTrue(all(row["source"] == "template" for row in rows.values()))
        self.assertIn("被动元件", rows["q8"]["answer"])

    def test_divergence_is_a_warning(self):
        rows = self.answers(two(weak_days()))
        self.assertEqual(rows["q5"]["status"], "support")
        self.assertEqual(rows["q6"]["status"], "warn")
        self.assertEqual(rows["q7"]["status"], "warn")
        self.assertIn("不取平均", rows["q7"]["answer"])
        self.assertEqual(rows["q8"]["status"], "neutral")         # 一多一空：总结不偏任何一边

    def test_consistently_weak_is_a_warning_and_consistently_strong_is_support(self):
        weak = self.answers(two(weak_days()))
        self.assertEqual(weak["q7"]["status"], "warn")
        strong = self.answers(two())
        self.assertEqual(strong["q7"]["status"], "support")
        payload = detail(_snapshot(
            [_sector("850823.SI", "被动元件", 0.82, days=weak_days()), _sector("885843.TI", "华为海思概念股", 0.8, days=weak_days(), type="concept")],
            primary="850823.SI", secondary="885843.TI", driver=_dual(),
        ))
        rows = self.answers(payload)
        self.assertEqual(rows["q7"]["status"], "warn")
        self.assertNotIn("不取平均", rows["q7"]["answer"])

    def test_independent_driver_warns(self):
        payload = detail(_snapshot([_sector("850823.SI", "被动元件", 0.82)], primary="850823.SI", driver={"type": "independent", "weights": {"primary": 1.0, "secondary": 0.0}, "r2": 0.1, "primary_share": 0.1, "days": 60}))
        rows = self.answers(payload)
        self.assertEqual(rows["q4"]["status"], "warn")
        self.assertIn("参考价值低", rows["q4"]["answer"])

    def test_no_primary_is_stated_not_invented(self):
        snap = _snapshot([_sector("850823.SI", "被动元件", 0.82)])
        snap["sectors"] = []
        snap["affiliation"]["candidates"][0].update({"belongs": False, "basis": "仅挂名", "role": None})
        rows = self.answers(detail(snap))
        self.assertEqual(rows["q1"]["status"], "warn")
        self.assertIn("没有找到本股明确属于的板块", rows["q1"]["answer"])
        self.assertEqual(rows["q5"]["status"], "na")

    def test_every_template_cite_exists(self):
        facts, d, text = sheet(with_hanging(two()))
        for row in template.answers(d, text):
            self.assertTrue(set(row["cites"]) <= set(text), row)


class ReviewTest(unittest.TestCase):
    def build(self, model=None, payload=None, store=None, **kwargs):
        store = store if store is not None else Store(":memory:")
        gateway = Gateway(model) if model is not None else None
        return review.build(stock(), store=store, gateway=gateway, now=NOW, payload=payload or two(), model_tag="x", **kwargs)

    def test_without_a_model_the_template_answers_everything(self):
        result = self.build()
        self.assertEqual(result["source"], "template")
        self.assertEqual([row["key"] for row in result["items"]], [f"q{i}" for i in range(1, 9)])
        self.assertGreater(len(result["facts"]), 20)

    def test_model_gets_only_each_questions_own_facts(self):
        model = ScriptedModel()
        self.build(model)
        view = json.loads(model.users[0])
        by_key = {q["key"]: {f["id"] for f in q["facts"]} for q in view["questions"]}
        self.assertIn("sp.p.basis", by_key["q1"])
        self.assertNotIn("sp.s.basis", by_key["q1"])
        self.assertNotIn("sp.driver.r2", by_key["q1"])
        self.assertEqual(model.calls, 1)

    def test_a_well_formed_answer_is_taken_from_the_model(self):
        result = self.build(ScriptedModel())
        self.assertTrue(any(row["source"] == "model" for row in result["items"]), result["debug"])

    def test_q1_must_cite_the_basis(self):
        def drop(out, view):
            by(out, "q1")["cites"] = ["sp.p.name"]
            by(out, "q1")["answer"] = "主板块是被动元件（申万行业）"
            return out

        result = self.build(ScriptedModel(mutate=drop))
        self.assertEqual({row["key"]: row["source"] for row in result["items"]}["q1"], "template")
        self.assertTrue(any("sp.p.basis" in item for item in result["debug"]["errors"]))

    def test_a_hanging_sector_cannot_be_called_a_member(self):
        def overreach(out, view):
            by(out, "q1")["answer"] = "本股也属于先进封装"
            return out

        result = self.build(ScriptedModel(mutate=overreach), payload=with_hanging(two()))
        self.assertEqual({row["key"]: row["source"] for row in result["items"]}["q1"], "template")
        self.assertTrue(any("只挂名" in item for item in result["debug"]["errors"]))

    def test_cites_outside_the_question_are_rejected(self):
        def wander(out, view):
            by(out, "q1")["cites"] = ["sp.p.basis", "sp.driver.r2"]
            return out

        result = self.build(ScriptedModel(mutate=wander))
        self.assertTrue(any("不属于本题" in item for item in result["debug"]["errors"]))

    def test_made_up_numbers_and_banned_words_fall_back(self):
        def lie(out, view):
            by(out, "q4")["answer"] = "板块解释了 93%，可以关注"
            return out

        result = self.build(ScriptedModel(mutate=lie))
        self.assertEqual({row["key"]: row["source"] for row in result["items"]}["q4"], "template")
        self.assertTrue(any("禁用词" in item for item in result["debug"]["errors"]))

    def test_model_cannot_flip_a_guarded_status(self):
        def flip(out, view):
            by(out, "q7")["status"] = "support"
            return out

        result = self.build(ScriptedModel(mutate=flip), payload=two(weak_days()))
        row = next(r for r in result["items"] if r["key"] == "q7")
        self.assertEqual(row["status"], "warn")
        self.assertEqual(result["debug"]["status_overridden"], ["q7"])

    def test_a_failing_model_keeps_the_template(self):
        result = self.build(ScriptedModel(fail=True))
        self.assertEqual(result["source"], "template")
        self.assertTrue(result["debug"]["call_error"])

    def test_same_facts_hit_the_cache(self):
        store, model = Store(":memory:"), ScriptedModel()
        self.build(model, store=store)
        again = self.build(model, store=store)
        self.assertTrue(again["cached"])
        self.assertEqual(model.calls, 1)

    def test_summary_headline_uses_the_model_answer_only_when_it_wrote_it(self):
        from app.services.verdict_view import summary_headline

        self.assertIsNone(summary_headline(self.build(), "q8"))
        result = self.build(ScriptedModel())
        headline = summary_headline(result, "q8")
        self.assertEqual(headline["source"], "model")
        self.assertEqual(sorted(headline), ["source", "status", "status_text", "text"])


class OutcomesTest(unittest.TestCase):
    def test_records_once_per_day_and_counts_changes(self):
        store = Store(":memory:")
        payload = two()
        self.assertTrue(outcomes.record(store, "603738.SH", payload))
        self.assertFalse(outcomes.record(store, "603738.SH", payload))          # 同一天同一版本只记第一次
        later = json.loads(json.dumps(payload))
        later["meta"]["as_of"] = "2026-09-05"
        later["sectors"]["secondary"] = None
        outcomes.record(store, "603738.SH", later)
        rows = store.sector_affiliations("603738.SH")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["primary_code"], "850823.SI")
        self.assertEqual(rows[0]["driver"], "dual")
        stats = outcomes.stability(rows)
        self.assertEqual((stats["pairs"], stats["primary_changes"], stats["secondary_changes"]), (1, 0, 1))
        self.assertEqual(stats["secondary_change_rate"], 1.0)

    def test_review_build_records_the_affiliation(self):
        store = Store(":memory:")
        review.build(stock(), store=store, gateway=None, now=NOW, payload=two(), model_tag="x")
        self.assertEqual(len(store.sector_affiliations()), 1)


class AttachTest(unittest.TestCase):
    def test_sector_kind_is_registered_and_starts_computing(self):
        store, spawned = Store(":memory:"), []
        out = attach.attach(single(), stock(), store, spawn=lambda s, w: spawned.append(s.ts_code), kind="sector")
        self.assertEqual(out["review_state"], "computing")
        self.assertEqual(spawned, ["603738.SH"])


class RouteTest(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.register_blueprint(v1_bp)
        self.client = app.test_client()
        self.payload = two()
        self.store = Store(":memory:")
        self.patches = [
            patch("app.api.v1.resolve_stock", lambda code: stock()),
            patch("app.services.sector_detail.build_detail", lambda s, *a, **kw: self.payload),
            patch("app.synthesis.worker.get_store", lambda: self.store),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in self.patches:
            item.stop()

    def test_detail_response_shape(self):
        body = self.client.get("/api/v1/stocks/603738.SH/dimensions/sector/detail").get_json()
        self.assertEqual(sorted(body["verdict"]), ["source", "status", "status_text", "text"])
        self.assertIsNone(body["headline"])
        self.assertIn(body["review_state"], ("computing", "disabled"))
        self.assertNotIn("inputs", body)
        self.assertEqual(body["sectors"]["primary"]["code"], "850823.SI")

    def test_ready_review_gives_a_model_headline(self):
        result = review.build(stock(), store=self.store, gateway=Gateway(ScriptedModel()), now=NOW, payload=self.payload, model_tag="x")
        self.assertTrue(result["source"] in ("model", "mixed"))
        body = self.client.get("/api/v1/stocks/603738.SH/dimensions/sector/detail").get_json()
        self.assertEqual(body["review_state"], "ready")
        self.assertEqual(body["headline"]["source"], "model")


if __name__ == "__main__":
    unittest.main()
