import unittest

from .fixtures import NOW, loaders, make_aligned, make_stock, volume_payload
from app.synthesis import policy
from app.synthesis.levels import Evaluator, build_candidates, reference_row, resolve_levels
from app.synthesis.levels import candidates as cand
from app.synthesis.levels.defaults import default_selection
from app.synthesis.levels.evaluate import describe, simulate_aligned
from app.synthesis.levels.render import condition_line, consequence_line, price_line
from app.synthesis.levels.resolve import resolve_side, tick
from app.synthesis.producer import adapters, health
from app.synthesis.producer.collect import collect


def _bars(closes, volumes=None):
    volumes = volumes or [1.0] * len(closes)
    return [{"date": f"2026-09-{i + 1:02d}", "close": c, "volume": v} for i, (c, v) in enumerate(zip(closes, volumes))]


def setup():
    aligned = make_aligned()
    stock = make_stock()
    bundle = collect(stock, NOW, loaders(aligned), aligned)
    snaps = [adapters.adapt(key, bundle.payloads[key]) for key in policy.DIMENSIONS]
    return stock, aligned, snaps


DEEP = [30.0] * 100 + [20.0] * 5 + [32.0] * 135 + [35.0 - 10.0 * i / 59 for i in range(60)]   # 300 根：现价 25


def bearish_below(threshold):
    """最后一天收盘低于 threshold 就让量价维度偏空；其余沿用真实规则。"""
    def build(aligned):
        payload = volume_payload(aligned)
        if aligned["bars"][-1]["close"] < threshold:
            payload["verdict"] = {**payload["verdict"], "lean": "bearish", "level": 2}
        return payload
    return build


class CandidateTest(unittest.TestCase):
    def test_ma20_needs_twenty_closes(self):
        self.assertIsNone(cand.ma20(_bars([1.0] * 19)))
        self.assertAlmostEqual(cand.ma20(_bars(list(range(1, 21)))), 10.5)

    def test_swing_low_requires_higher_closes_on_both_sides(self):
        closes = [10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 11, 12, 13, 9, 13, 12, 11, 10.5, 10.6, 10.7]
        # 9 的前后各 3 天都更高
        self.assertEqual(cand.swing_low(_bars(closes)), 9)
        self.assertIsNone(cand.swing_low(_bars([10.0] * 30)))

    def test_cost_zone_weights_by_big_money_inflow(self):
        bars = _bars([10.0, 20.0, 30.0])
        flow = [
            {"date": bars[0]["date"], "super_large": 3.0, "large": 0.0},
            {"date": bars[1]["date"], "super_large": 1.0, "large": 0.0},
            {"date": bars[2]["date"], "super_large": -9.0, "large": 0.0},   # 净流出的日子不算
        ]
        self.assertAlmostEqual(cand.cost_zone(bars, flow), (3 * 10 + 1 * 20) / 4)
        self.assertIsNone(cand.cost_zone(bars, []))

    def test_volume_cluster_picks_heaviest_price_bin(self):
        closes = [10.0] * 10 + [12.0] * 40 + [14.0] * 10
        volumes = [1.0] * 10 + [5.0] * 40 + [1.0] * 10
        value = cand.volume_cluster(_bars(closes, volumes))
        self.assertAlmostEqual(value, 12.0, delta=0.25)

    def test_pre_news_close_depends_on_session(self):
        bars = _bars([10.0, 11.0, 12.0, 13.0])
        self.assertEqual(cand.pre_news(bars, "2026-09-03T08:00:00+08:00", "pre"), 11.0)
        self.assertEqual(cand.pre_news(bars, "2026-09-03T18:30:00+08:00", "post"), 12.0)
        self.assertIsNone(cand.pre_news(bars, None, "post"))

    def test_pattern_invalid_is_the_close_before_the_window(self):
        bars = _bars([float(i) for i in range(1, 31)])
        self.assertEqual(cand.pattern_invalid(bars, "up_vol_up", 20), 10.0)
        self.assertIsNone(cand.pattern_invalid(bars, "down_vol_up", 20))
        self.assertIsNone(cand.pattern_invalid(bars, "none", 20))

    def test_screen_filters_distance_and_merges_close_levels(self):
        rows = [
            {"id": "L1", "name": "20 日均线", "dir": "down", "priority": 2, "price": 90.0, "dist": -0.10},
            {"id": "L2", "name": "大资金成本区", "dir": "down", "priority": 1, "price": 90.5, "dist": -0.095},
            {"id": "L4", "name": "近期波段低点", "dir": "down", "priority": 4, "price": 98.0, "dist": -0.02},
            {"id": "L5", "name": "量价形态失效价", "dir": "down", "priority": 5, "price": 80.0, "dist": -0.20},
            {"id": "U1", "name": "前高", "dir": "up", "priority": 1, "price": 110.0, "dist": 0.10},
        ]
        kept = cand.screen(rows)
        self.assertEqual([item["id"] for item in kept], ["L2", "U1"])
        self.assertEqual(kept[0]["name"], "大资金成本区 + 20 日均线")
        self.assertEqual(kept[0]["merged_ids"], ["L2", "L1"])

    def test_fixture_candidates_are_computed_by_definition(self):
        aligned = make_aligned()
        rows = {item["id"]: item for item in cand.raw_candidates(aligned, "up_vol_up", {
            "impact": "positive", "published": "2026-10-27T18:30:00+08:00", "session": "post"})}
        closes = [row["close"] for row in aligned["bars"]]
        self.assertAlmostEqual(rows["L1"]["price"], sum(closes[-20:]) / 20, places=3)
        self.assertEqual(rows["U2"]["price"], round(max(closes[-250:]), 4))
        self.assertAlmostEqual(rows["U3"]["price"], max(closes[-250:]) * 0.9, places=3)
        self.assertEqual(rows["L3"]["price"], [r["close"] for r in aligned["bars"] if r["date"] == "2026-10-27"][0])


class BelowEverythingTest(unittest.TestCase):
    """现价已经跌到近期最低：均线、成本区全在现价上方，要当作压力，而不是丢掉。"""

    def setUp(self):
        closes = DEEP   # 一路下跌，现价是近 60 天最低，但一年内更早有过更低的价
        self.aligned = make_aligned(closes)
        self.rows = {item["id"]: item for item in cand.raw_candidates(self.aligned, "down_vol_up")}
        self.current = closes[-1]

    def test_moving_average_and_cost_zone_turn_into_resistance(self):
        self.assertIn("U5", self.rows)
        self.assertNotIn("L1", self.rows)
        self.assertEqual(self.rows["U5"]["dir"], "up")
        self.assertGreater(self.rows["U5"]["price"], self.current)
        self.assertEqual(self.rows["U5"]["name"], "20 日均线")

    def test_one_year_low_is_the_support_when_the_recent_lows_are_gone(self):
        closes = [row["close"] for row in self.aligned["bars"]]
        self.assertEqual(self.current, min(closes[-60:]))
        self.assertNotIn("L7", self.rows)   # 现价就是 20 日最低，不在现价下方
        self.assertEqual(self.rows["L8"]["price"], 20.0)
        self.assertEqual(self.rows["L8"]["dir"], "down")
        flat = make_aligned([30.0 - 0.01 * i for i in range(300)])   # 一路创新低：下面确实没有任何价
        self.assertEqual({item["dir"] for item in cand.raw_candidates(flat)} & {"down"}, set())

    def test_both_sides_get_a_level_even_far_from_the_range(self):
        aligned = make_aligned(DEEP)
        stock = make_stock()
        bundle = collect(stock, NOW, loaders(aligned), aligned)
        snaps = [adapters.adapt(key, bundle.payloads[key]) for key in policy.DIMENSIONS]
        evaluator = Evaluator(stock, aligned, snaps, NOW, True)
        kept = build_candidates(aligned, snaps, evaluator)
        result = resolve_levels(None, kept, DEEP[-1], evaluator, snaps)
        self.assertIsNotNone(result["up"])
        self.assertIsNotNone(result["down"])
        self.assertEqual(result["down"]["base_name"], "一年最低收盘")
        self.assertFalse(result["down"]["verified"])
        self.assertEqual(result["up"]["base"], "U5")


class AtNewExtremesTest(unittest.TestCase):
    """创了新低或新高：那一侧在历史收盘价里没有任何价，只能用波动带兜底。两侧都必须有价。"""

    def levels(self, closes):
        aligned = make_aligned(closes)
        stock = make_stock()
        bundle = collect(stock, NOW, loaders(aligned), aligned)
        snaps = [adapters.adapt(key, bundle.payloads[key]) for key in policy.DIMENSIONS]
        evaluator = Evaluator(stock, aligned, snaps, NOW, True)
        kept = build_candidates(aligned, snaps, evaluator)
        return resolve_levels(None, kept, closes[-1], evaluator, snaps), evaluator

    def test_new_low_gets_a_volatility_band_below(self):
        closes = [40.0 - 0.05 * i + (0.3 if i % 2 else -0.3) for i in range(300)]
        closes[-1] = min(closes) - 0.5
        result, evaluator = self.levels(closes)
        self.assertEqual(result["down"]["base"], "L9")
        self.assertEqual(result["down"]["base_name"], "波动带下沿")
        self.assertFalse(result["down"]["verified"])
        self.assertTrue(policy.DIST_MIN <= abs(result["down"]["dist"]) <= policy.DIST_MAX)
        self.assertLess(result["down"]["price"], closes[-1])
        self.assertIsNotNone(result["up"])
        status = {item["id"]: item["status"] for item in evaluator.considered}
        self.assertEqual(status["L9"], "reference")

    def test_new_high_gets_a_volatility_band_above(self):
        closes = [10.0 + 0.05 * i + (0.3 if i % 2 else -0.3) for i in range(300)]
        closes[-1] = max(closes) + 0.5
        result, _ = self.levels(closes)
        self.assertEqual(result["up"]["base"], "U9")
        self.assertGreater(result["up"]["price"], closes[-1])
        self.assertTrue(policy.DIST_MIN <= result["up"]["dist"] <= policy.DIST_MAX)
        self.assertIsNotNone(result["down"])

    def test_band_width_follows_recent_volatility_and_is_clamped(self):
        calm = make_aligned([20.0 + 0.001 * i for i in range(300)])
        wild = make_aligned([20.0 + (3.0 if i % 2 else -3.0) for i in range(300)])
        self.assertAlmostEqual(abs(cand.volatility_row(calm, "down")["dist"]), policy.DIST_MIN, places=3)
        self.assertAlmostEqual(abs(cand.volatility_row(wild, "up")["dist"]), policy.DIST_MAX, places=3)
        self.assertIsNone(cand.volatility_row({"bars": [{"close": 1.0}] * 3}, "down"))
        self.assertIsNone(cand.volatility_row(None, "up"))


class SimulateTest(unittest.TestCase):
    def test_two_day_confirmation_rewrites_two_closes_without_touching_the_original(self):
        aligned = make_aligned()
        before = [dict(row) for row in aligned["bars"]]
        sim = simulate_aligned(aligned, 20.0, "down", "close_2d")
        self.assertEqual(aligned["bars"], before)
        self.assertAlmostEqual(sim["bars"][-1]["close"], 19.8)
        self.assertAlmostEqual(sim["bars"][-2]["close"], 19.8)
        self.assertEqual(sim["bars"][-1]["chg"], 0.0)
        self.assertLess(sim["bars"][-2]["chg"], 0)

    def test_volume_and_outflow_confirmations(self):
        aligned = make_aligned()
        loud = simulate_aligned(aligned, 20.0, "down", "close_volume")
        self.assertGreater(loud["bars"][-1]["volume"], 1_000_000.0)
        out = simulate_aligned(aligned, 20.0, "down", "close_outflow")
        self.assertLess(out["flow"][-1]["super_large"], 0)
        self.assertLess(out["flow"][-1]["large"], 0)
        self.assertGreater(aligned["flow"][-1]["super_large"], 0)

    def test_describe_names_the_consequence(self):
        base = {"direction": "bullish", "level": 2, "position": "mid"}
        self.assertEqual(describe(base, {**base, "direction": "bearish"}, "down")["text"], "这轮上涨可能结束了")
        self.assertEqual(describe(base, {**base, "direction": "unclear", "level": 0}, "down")["text"], "偏多的判断可能失效")
        self.assertFalse(describe(base, dict(base), "down")["changed"])
        self.assertEqual(describe(base, {**base, "position": "high"}, "up")["text"], "位置变成偏高")


class ResolveTest(unittest.TestCase):
    def setUp(self):
        self.stock, self.aligned, self.snaps = setup()
        self.current = self.aligned["bars"][-1]["close"]
        self.fresh = health.fresh(self.snaps, NOW)

    def evaluator(self, build=None):
        return Evaluator(self.stock, self.aligned, self.snaps, NOW, self.fresh, build)

    def down(self, price, name="20 日均线", cid="L1"):
        return {"id": cid, "dir": "down", "name": name, "price": price, "dist": price / self.current - 1}

    def test_agent_choice_is_used_when_the_consequence_holds(self):
        candidates = [self.down(self.current * 0.90)]
        choice = {"base": "L1", "buffer": 0.02, "confirm": "close_2d", "why": "x", "cites": ["flow.pattern"]}
        result, steps = resolve_side("down", choice, candidates, self.current, self.evaluator(bearish_below(1e9)), "unclear")
        self.assertEqual(result["origin"], "agent")
        self.assertEqual(result["price"], tick(self.current * 0.90 * 0.98))
        self.assertEqual(result["confirm"], "close_2d")
        self.assertEqual(result["consequence"], "→ 判断可能减弱")   # 其余维度仍偏多：只是减弱，没有翻转
        self.assertTrue(result["line"].startswith("▼ "))
        self.assertEqual(steps, [])

    def test_buffer_that_leaves_the_range_steps_back_to_zero(self):
        candidates = [self.down(self.current * 0.86)]
        choice = {"base": "L1", "buffer": 0.03, "confirm": "close", "why": "", "cites": []}
        result, steps = resolve_side("down", choice, candidates, self.current, self.evaluator(bearish_below(1e9)), "unclear")
        self.assertEqual(result["buffer"], 0.0)
        self.assertEqual(len(steps), 2)
        self.assertTrue(all("不在范围内" in step for step in steps))

    def test_unchanged_conclusion_means_no_level(self):
        candidates = [self.down(self.current * 0.90)]
        result, steps = resolve_side("down", None, candidates, self.current, self.evaluator(), "unclear")
        self.assertIsNone(result)
        self.assertTrue(any("结论没变" in step for step in steps))

    def test_unknown_base_falls_back_to_the_reference_row(self):
        candidates = [self.down(self.current * 0.90, cid="L1")]
        choice = {"base": "L9", "buffer": 0.0, "confirm": "close"}
        result, _ = resolve_side("down", choice, candidates, self.current, self.evaluator(bearish_below(1e9)), "unclear")
        self.assertEqual(result["origin"], "default")
        self.assertEqual(result["base"], "L1")

    def test_stricter_confirmation_relaxes_when_it_changes_nothing(self):
        def build(aligned):
            payload = volume_payload(aligned)
            if aligned["bars"][-1]["close"] < 1e9 and aligned["bars"][-1]["volume"] == 2_000_000.0:
                payload["verdict"] = {**payload["verdict"], "lean": "bearish", "level": 2}
            return payload

        candidates = [self.down(self.current * 0.90)]
        choice = {"base": "L1", "buffer": 0.0, "confirm": "close_volume"}
        result, steps = resolve_side("down", choice, candidates, self.current, self.evaluator(build), "unclear")
        self.assertEqual(result["confirm"], "close")
        self.assertTrue(any("close_volume" in step for step in steps))

    def test_up_side_keeps_the_meaning_when_position_flips(self):
        candidates = [{"id": "U3", "dir": "up", "name": "位置变偏高价", "price": self.current * 1.05, "dist": 0.05}]
        choice = {"base": "U3", "confirm": "close", "meaning": "position_high"}
        result, _ = resolve_side("up", choice, candidates, self.current, self.evaluator(), "unclear")
        self.assertIsNotNone(result)
        self.assertEqual(result["consequence"], "→ 位置变成偏高")
        self.assertTrue(result["line"].startswith("▲ "))

    def test_resolve_levels_handles_missing_price(self):
        result = resolve_levels(None, [], None, self.evaluator(), self.snaps)
        self.assertEqual((result["down"], result["up"]), (None, None))


class IntegrationTest(unittest.TestCase):
    def test_verified_candidates_keep_their_consequence(self):
        stock, aligned, snaps = setup()
        evaluator = Evaluator(stock, aligned, snaps, NOW, True)
        kept = build_candidates(aligned, snaps, evaluator)
        verified = [item for item in kept if item["verified"]]
        self.assertTrue(verified)
        for item in verified:
            self.assertTrue(item["consequence"])
            self.assertTrue(policy.DIST_MIN <= abs(item["dist"]) <= policy.DIST_MAX)
        up = next(item for item in verified if item["dir"] == "up")
        self.assertEqual(up["consequence"], "位置变成偏高")

    def test_a_side_with_no_verified_candidate_still_gets_the_nearest_one(self):
        stock, aligned, snaps = setup()
        evaluator = Evaluator(stock, aligned, snaps, NOW, True)
        kept = build_candidates(aligned, snaps, evaluator)
        down = [item for item in kept if item["dir"] == "down"]
        self.assertTrue(down)
        self.assertTrue(all(item["verified"] is False and item["consequence"] == "" for item in down))
        result = resolve_levels(None, kept, aligned["bars"][-1]["close"], evaluator, snaps)
        self.assertIsNotNone(result["down"])
        self.assertIsNotNone(result["up"])
        self.assertFalse(result["down"]["verified"])
        self.assertTrue(result["up"]["verified"])
        self.assertTrue(result["down"]["line"].startswith("▼ "))
        self.assertEqual(result["down"]["consequence"], "→ 跌破后，这轮走势需要重新评估")
        self.assertTrue(any("没有通过后果验证" in step for step in result["steps"]))

    def test_fallback_prefers_the_candidate_nearest_to_the_allowed_range(self):
        rows = [
            {"id": "L1", "name": "a", "dir": "down", "priority": 2, "price": 99.0, "dist": -0.01},
            {"id": "L2", "name": "b", "dir": "down", "priority": 1, "price": 80.0, "dist": -0.20},
            {"id": "L4", "name": "c", "dir": "down", "priority": 4, "price": 95.0, "dist": -0.05},
        ]
        picked = cand.fallback_rows(rows, "down", limit=2)
        self.assertEqual([item["id"] for item in picked], ["L4", "L1"])
        self.assertEqual(cand.fallback_rows(rows, "up"), [])

    def test_twenty_day_low_is_the_last_resort_support(self):
        aligned = make_aligned()
        rows = {item["id"]: item for item in cand.raw_candidates(aligned)}
        self.assertEqual(rows["L7"]["price"], min(row["close"] for row in aligned["bars"][-20:]))
        falling = make_aligned([30.0 - 0.1 * i for i in range(300)])   # 一路下跌，收盘就是最低
        self.assertNotIn("L7", {item["id"] for item in cand.raw_candidates(falling)})


class TemplateTest(unittest.TestCase):
    def test_lines(self):
        self.assertEqual(price_line("down", 20.6, -0.0813), "▼ 20.60 再跌 8.1%")
        self.assertEqual(price_line("up", 24.6, 0.098), "▲ 24.60 再涨 9.8%")
        self.assertEqual(condition_line("down", "close_2d", "大资金成本区", 0.02), "连续 2 日收盘跌破大资金成本区下方 2%")
        self.assertEqual(condition_line("down", "close_outflow", "20 日均线", 0.0), "收盘跌破20 日均线，且大资金当日净流出")
        self.assertEqual(condition_line("up", "close_volume", "前高", 0.0), "放量收盘突破前高")
        self.assertEqual(consequence_line(""), "")
        self.assertEqual(tick(21.0196), 21.02)

    def test_reference_rows(self):
        stock, aligned, snaps = setup()
        self.assertEqual(reference_row(snaps), "good_news_unpriced")
        selection = default_selection("good_news_unpriced", [
            {"id": "L2", "dir": "down"}, {"id": "U1", "dir": "up"}])
        self.assertEqual(selection["down"]["base"], "L2")
        self.assertEqual(selection["down"]["buffer"], 0.02)
        self.assertEqual(selection["up"]["meaning"], "stronger")


if __name__ == "__main__":
    unittest.main()
