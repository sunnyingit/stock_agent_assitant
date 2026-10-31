import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from .fixtures import (
    NOW,
    finance_payload,
    fundamental_payload,
    loaders,
    make_aligned,
    make_stock,
    news_payload,
    sector_payload,
    volume_payload,
)
from app.synthesis import policy, rules
from app.synthesis.calendar import last_close_date, session_of, workday_gap
from app.synthesis.producer import adapters, health
from app.synthesis.producer.collect import collect
from app.synthesis.producer.facts import build_fact_table
from app.synthesis.producer.market import market_facts
from app.synthesis.registry import REGISTRY, numbers, render
from app.synthesis.types import STATUS_INVALID, STATUS_OK, STATUS_PENDING, STATUS_STALE, STATUS_UNAVAILABLE

ZONE = ZoneInfo("Asia/Shanghai")


def snapshots(**overrides):
    aligned = make_aligned()
    bundle = collect(make_stock(), NOW, loaders(aligned, **overrides), aligned)
    return [adapters.adapt(key, bundle.payloads[key], bundle.errors.get(key, "")) for key in policy.DIMENSIONS], aligned


class CalendarTest(unittest.TestCase):
    def test_sessions_and_last_close(self):
        self.assertEqual(session_of(datetime(2026, 10, 29, 10, 0, tzinfo=ZONE)), "intraday")
        self.assertEqual(session_of(datetime(2026, 10, 29, 16, 0, tzinfo=ZONE)), "close")
        self.assertEqual(session_of(datetime(2026, 10, 31, 10, 0, tzinfo=ZONE)), "close")
        self.assertEqual(last_close_date(datetime(2026, 10, 29, 10, 0, tzinfo=ZONE)).isoformat(), "2026-10-28")
        self.assertEqual(last_close_date(datetime(2026, 10, 29, 16, 0, tzinfo=ZONE)).isoformat(), "2026-10-29")
        self.assertEqual(last_close_date(datetime(2026, 10, 31, 12, 0, tzinfo=ZONE)).isoformat(), "2026-10-30")
        self.assertEqual(last_close_date(datetime(2026, 11, 1, 12, 0, tzinfo=ZONE)).isoformat(), "2026-10-30")
        self.assertEqual(workday_gap(datetime(2026, 10, 23).date(), datetime(2026, 10, 29).date()), 4)


class AdapterTest(unittest.TestCase):
    def test_volume_flow_uses_structured_fields(self):
        snap = adapters.adapt("volume_flow", volume_payload())
        self.assertEqual(snap.status, STATUS_OK)
        self.assertEqual((snap.lean, snap.level), ("bullish", 2))
        self.assertEqual(snap.facts["flow.pattern"], "up_vol_up")
        self.assertAlmostEqual(snap.facts["flow.rel_vol"], 2.0)
        self.assertIn("flow.view", snap.facts)
        self.assertEqual(snap.flags["index_ret"], 0.0202)

    def test_other_dimensions(self):
        news = adapters.adapt("news", news_payload())
        self.assertEqual(news.facts["news.main_event"], "业绩类 · A 级 · 利好 · 量级大")
        self.assertEqual(news.facts["news.priced_in"], "not_yet")
        self.assertTrue(news.facts["news.company_notice"])
        sector = adapters.adapt("sector", sector_payload())
        self.assertEqual(sector.facts["sector.stage"], "main")
        self.assertAlmostEqual(sector.facts["sector.vs_index"], 0.06)
        self.assertAlmostEqual(sector.facts["sector.stock_vs_sector"], 0.04)
        self.assertEqual(sector.events[0]["kind"], "sector_stage")
        finance = adapters.adapt("finance", finance_payload(red=True))
        self.assertTrue(finance.facts["fin.red_line"])
        self.assertTrue(finance.flags["red_line"])
        self.assertEqual(finance.facts["fin.days_to_next"], 40)
        fundamental = adapters.adapt("fundamental", fundamental_payload())
        self.assertEqual(fundamental.facts["fund.holder_event"], "解禁（利空），12 个交易日内")
        self.assertEqual(len(fundamental.events), 1)

    def test_states_map_to_status(self):
        self.assertEqual(adapters.adapt("news", None, "取数失败").status, STATUS_UNAVAILABLE)
        self.assertEqual(adapters.adapt("news", news_payload(state="computing")).status, STATUS_PENDING)
        self.assertEqual(adapters.adapt("sector", sector_payload(state="stale")).status, STATUS_STALE)
        self.assertEqual(adapters.adapt("news", news_payload(state="no_news", lean="unclear", level=0)).status, STATUS_OK)

    def test_broken_payload_does_not_raise(self):
        snap = adapters.adapt("finance", {"meta": {"state": "ok"}, "verdict": "oops"})
        self.assertIn(snap.status, (STATUS_INVALID, STATUS_OK))

    def test_no_event_means_no_priced_in_fact(self):
        payload = news_payload()
        payload["key_events"] = []
        snap = adapters.adapt("news", payload)
        self.assertNotIn("news.priced_in", snap.facts)
        self.assertEqual(snap.facts["news.main_event"], "近期没有公司范围的有效事件")


class MarketAndHealthTest(unittest.TestCase):
    def test_market_facts_share_one_basis(self):
        facts = market_facts(make_aligned())
        self.assertAlmostEqual(facts["market.stock_ret_20d"], 0.2202)
        self.assertAlmostEqual(facts["market.index_ret_20d"], 0.0202)
        self.assertLess(facts["market.from_year_high"], 0)

    def test_stale_dimension_is_marked(self):
        snaps, aligned = snapshots()
        snaps[2].as_of = "2026-10-01"
        health.inspect(snaps, market_facts(aligned), NOW)
        self.assertEqual(snaps[2].status, STATUS_STALE)
        self.assertEqual(rules.synthesize(snaps, market_facts(aligned), True)["done"], 4)   # 过期的不算完成

    def test_unit_error_is_caught_before_the_model(self):
        snaps, aligned = snapshots()
        snaps[2].facts["sector.vs_index"] = 12.5   # 把百分数当成了小数
        notes = health.inspect(snaps, market_facts(aligned), NOW)["notes"]
        self.assertEqual(snaps[2].status, STATUS_INVALID)
        self.assertEqual(notes[0]["check"], "unit")

    def test_cross_module_index_mismatch(self):
        snaps, aligned = snapshots()
        snaps[2].flags["index_ret"] = 0.20
        health.inspect(snaps, market_facts(aligned), NOW)
        self.assertEqual(snaps[2].status, STATUS_INVALID)
        self.assertEqual(snaps[0].status, STATUS_OK)

    def test_fresh_requires_latest_session(self):
        snaps, aligned = snapshots()
        self.assertTrue(health.fresh(snaps, NOW))
        snaps[0].as_of = "2026-10-28"
        self.assertFalse(health.fresh(snaps, NOW))


class FactTableTest(unittest.TestCase):
    def test_registry_render_and_numbers(self):
        self.assertEqual(render("market.from_year_high", -0.1215), "-12.2%")
        self.assertEqual(render("flow.rel_vol", 2.0), "2.00 倍")
        self.assertEqual(render("flow.pattern", "up_vol_up"), "价涨量增")
        self.assertEqual(render("fin.red_line", False), "否")
        allowed = numbers("market.from_year_high", -0.1215)
        self.assertTrue({12.15, 12.2, 12.0, 0.1215} <= {round(x, 2) for x in allowed} | allowed)

    def test_every_fact_has_a_known_dimension(self):
        for defn in REGISTRY.values():
            self.assertTrue(defn.dim in policy.DIMENSIONS or defn.dim == "market", defn.id)

    def test_table_has_no_identity_and_numbers_events(self):
        snaps, aligned = snapshots()
        market = market_facts(aligned)
        rule = rules.synthesize(snaps, market, True)
        table = build_fact_table(snaps, market, rule)
        text = str(table.sides_view())
        self.assertNotIn("测试股份", text)
        self.assertNotIn("600001", text)
        self.assertNotIn("2026-", text)
        self.assertEqual([(item["id"], item["kind"]) for item in table.events], [("E1", "sector_stage"), ("E2", "holder")])
        self.assertIsNotNone(table.fact("flow.pattern"))
        self.assertNotIn("rule_reference", table.sides_view())
        self.assertIn("rule_reference", table.chief_view())

    def test_unavailable_dimension_gives_no_direction(self):
        snaps, aligned = snapshots(sector=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        market = market_facts(aligned)
        table = build_fact_table(snaps, market, rules.synthesize(snaps, market, True))
        self.assertIsNone(table.fact("sector.stage"))
        self.assertIsNone(table.fact("sector.view"))
        self.assertTrue(any("板块" in item["text"] and "暂不可用" in item["text"] for item in table.unavailable))

    def test_digest_is_stable_and_sensitive(self):
        snaps, aligned = snapshots()
        market = market_facts(aligned)
        rule = rules.synthesize(snaps, market, True)
        first = build_fact_table(snaps, market, rule)
        second = build_fact_table(snaps, market, rule)
        self.assertEqual(first.digest(), second.digest())
        self.assertEqual(first.sides_digest(), second.sides_digest())
        second.candidates = [{"id": "L1", "dir": "down", "name": "x", "dist": -0.05, "sources": [], "consequence": ""}]
        self.assertNotEqual(first.digest(), second.digest())
        self.assertEqual(first.sides_digest(), second.sides_digest())   # 价位候选不影响正反方缓存


class CollectTest(unittest.TestCase):
    def test_failures_and_timeouts_are_recorded_not_raised(self):
        def boom():
            raise RuntimeError("接口挂了")

        bundle = collect(make_stock(), NOW, {"news": boom, "sector": lambda: sector_payload()}, None)
        self.assertIsNone(bundle.payloads["news"])
        self.assertIn("接口挂了", bundle.errors["news"])
        self.assertIsNotNone(bundle.payloads["sector"])
        self.assertEqual(bundle.errors["finance"], "没有取数入口")


class SlowDimensionTest(unittest.TestCase):
    def test_timeout_marks_the_dimension_pending_instead_of_failed(self):
        import time as _time
        from app.synthesis import service
        from app.synthesis.store import Store

        def slow():
            _time.sleep(0.6)
            return sector_payload()

        aligned = make_aligned()
        reply = service.compute(
            make_stock(), Store(":memory:"), mode="rule", now=NOW, aligned=aligned, timeout=0.2,
            loaders=loaders(aligned, sector=slow),
        )
        sector = next(item for item in reply["display"]["dimensions"] if item["key"] == "sector")
        self.assertEqual(sector["status"], "pending")
        self.assertEqual(reply["pending"], ["sector"])


class RulesTest(unittest.TestCase):
    def test_bullish_consensus(self):
        snaps, aligned = snapshots()
        rule = rules.synthesize(snaps, market_facts(aligned), True)
        self.assertEqual((rule["direction"], rule["done"], rule["total"]), ("bullish", 5, 5))
        self.assertEqual(rule["counts"], {"bullish": 4, "bearish": 0, "neutral": 1})
        self.assertEqual(rule["majority"], "bullish")
        self.assertEqual(rule["position"], "mid")

    def test_red_line_blocks_bullish(self):
        snaps, aligned = snapshots(finance=finance_payload(red=True))
        rule = rules.synthesize(snaps, market_facts(aligned), True)
        self.assertEqual((rule["direction"], rule["level"]), ("unclear", 0))
        self.assertTrue(rule["red_capped"])

    def test_low_coverage_caps_strength(self):
        boom = lambda: (_ for _ in ()).throw(RuntimeError("x"))
        snaps, aligned = snapshots(news=boom, sector=boom, finance=boom)
        rule = rules.synthesize(snaps, market_facts(aligned), True)
        self.assertEqual(rule["done"], 2)
        self.assertLessEqual(rule["level"], policy.LOW_COVERAGE_CAP)

    def test_conflict_and_edge(self):
        snaps, aligned = snapshots(
            news=news_payload(impact="negative", lean="bearish", level=2),
            sector=sector_payload(lean="bearish", level=2),
        )
        rule = rules.synthesize(snaps, market_facts(aligned), True)
        self.assertEqual(rule["counts"]["bearish"], 2)
        self.assertEqual(rule["direction"], "unclear")

    def test_position_follows_year_high(self):
        self.assertEqual(rules.position_of(-0.05), "high")
        self.assertEqual(rules.position_of(-0.2), "mid")
        self.assertEqual(rules.position_of(-0.4), "low")
        self.assertEqual(rules.position_of(None), "mid")

    def test_credibility_steps_down(self):
        snaps, aligned = snapshots()
        rule = rules.synthesize(snaps, market_facts(aligned), True)
        self.assertEqual(rules.credibility(rule)["level"], "high")
        self.assertEqual(rules.credibility(rule, inconsistent=True)["level"], "medium")
        stale = dict(rule, fresh=False, red_line=True)
        self.assertEqual(rules.credibility(stale, inconsistent=True)["level"], "low")
        self.assertEqual(rules.credibility(rule)["total"], len(policy.DIMENSIONS))


if __name__ == "__main__":
    unittest.main()
