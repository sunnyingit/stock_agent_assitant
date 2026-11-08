import random
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import create_app
from app.services.volume_flow.align import lots_to_shares, qfq, qian_to_yuan, wan_to_yuan
from app.services.volume_flow.detail import build_detail, public
from app.services.volume_flow.follow import follow, refine
from app.services.volume_flow.metrics import compute
from app.services.volume_flow.rules import flow_state, judge, price_pattern
from app.tools.stock import Stock


def _days(count, start=date(2026, 6, 1)):
    rows = []
    cursor = start
    while len(rows) < count:
        if cursor.weekday() < 5:
            rows.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return rows


SEED_NORMAL = 1     # 这个种子下，最近 20 日和自己平时差不多


def _wobble(level, count):
    """围绕 level 小幅波动的大单净额：有波动，才有「和自己平时比」。"""
    return [(level + (i % 4) * 0.5, level + (i % 3) * 0.3, -1.0, -1.0) for i in range(count)]


def _noisy(level, count, seed=SEED_NORMAL):
    rng = random.Random(seed)
    return [(level + rng.uniform(-1.5, 1.5), level + rng.uniform(-1, 1), -1.0, -1.0) for _ in range(count)]


def _aligned(changes, volumes, flows, *, amount=100000.0, turnover=1.0):
    dates = _days(len(changes))
    bars = []
    flow = []
    sector = []
    index = []
    for index_no, trade_date in enumerate(dates):
        bars.append({
            "date": trade_date,
            "close": 10 + index_no,
            "volume": volumes[index_no],
            "amount": amount,
            "chg": changes[index_no],
            "turnover": turnover,
        })
        elg, large, medium, small = flows[index_no]
        flow.append({
            "date": trade_date,
            "super_large": elg,
            "large": large,
            "medium": medium,
            "small": small,
        })
        sector.append({"date": trade_date, "chg": 0.2, "net": 100.0, "rate": 2.0})
        index.append({"date": trade_date, "chg": 0.1})
    return {
        "name": "贵州茅台",
        "as_of": dates[-1],
        "bars": bars,
        "flow": flow,
        "sector": {"name": "白酒Ⅱ", "rows": sector},
        "index": {"name": "沪深300", "rows": index},
        "margin_percentile": None,
    }


class VolumeFlowTest(unittest.TestCase):
    def test_units_and_forward_adjust(self):
        self.assertEqual(qfq(10, 2, 4), 5)
        self.assertEqual(lots_to_shares(3), 300)
        self.assertEqual(wan_to_yuan(1.5), 15000)
        self.assertEqual(qian_to_yuan(2), 2000)

    def test_window_rate_and_volume_base(self):
        changes = [1.0] * 80
        changes[-3:] = [2.0, 1.0, -1.0]
        volumes = [100.0] * 80
        volumes[-3:] = [200.0, 200.0, 100.0]
        flows = [(10.0, 0.0, 0.0, 0.0)] * 80
        metrics = compute(_aligned(changes, volumes, flows, amount=10000), 3)
        self.assertEqual(metrics["price"]["vol_base"], 100)
        self.assertEqual(metrics["price"]["up_down_vol_ratio"], 2)
        self.assertNotIn("is_up", metrics["price"]["bars"][0])
        self.assertAlmostEqual(metrics["windows"][-1]["net_rate"], 0.1)
        self.assertEqual(metrics["flow"]["rows"][0]["size"], "super_large")
        self.assertEqual(metrics["relative"]["sector"]["name"], "白酒Ⅱ")
        self.assertAlmostEqual(metrics["relative"]["net_rate_vs_sector"], -1.9)
        self.assertEqual([row["days"] for row in metrics["relative_windows"]], [3, 5, 20])
        self.assertEqual(metrics["relative_windows"][0]["sector"]["name"], "白酒Ⅱ")

    def test_quiet_volume_has_no_pattern(self):
        changes = [1.0] * 75 + [1.0, 1.0, 1.0, -0.2, 1.0]
        volumes = [100.0] * 80
        flows = [(20.0, 10.0, -5.0, -1.0)] * 80
        metrics = compute(_aligned(changes, volumes, flows), 20)
        verdict = judge(metrics, stale=False)
        self.assertGreaterEqual(metrics["price"]["rel_vol"], 0.8)
        self.assertLessEqual(metrics["price"]["rel_vol"], 1.2)
        self.assertEqual(verdict["pattern"], "none")
        self.assertEqual(verdict["lean"], "unclear")
        self.assertEqual(verdict["level"], 0)
        self.assertIsNone(verdict["certainty"])

    def test_expanded_volume_can_reach_level_three(self):
        changes = [1.0] * 240 + [1.0] * 16 + [-0.2] * 4
        volumes = [100.0] * 240 + [200.0] * 16 + [50.0] * 4
        flows = _wobble(1.0, 250) + [(20.0, 10.0, -5.0, -1.0)] * 10
        metrics = compute(_aligned(changes, volumes, flows), 20)
        verdict = judge(metrics, stale=False)
        self.assertEqual(verdict["pattern"], "up_vol_up")
        self.assertEqual(verdict["lean"], "bullish")
        self.assertGreaterEqual(metrics["flow"]["rate_z"], 1.5)       # 大资金比这只股票自己平时明显更流入
        self.assertEqual(verdict["flow_state"], "in_strong")
        self.assertEqual(verdict["level"], 3)
        # 中单反向只降把握，不翻方向
        self.assertEqual(verdict["certainty"], "medium")
        self.assertEqual(verdict["structure"]["medium"], "against")

    def test_opposite_flow_does_not_flip_direction(self):
        changes = [1.0] * 260
        volumes = [100.0] * 240 + [200.0] * 20
        flows = _wobble(1.0, 240) + [(-5.0, -5.0, -1.0, -1.0)] * 20     # 平时小幅净流入，这一段明显转为净流出
        metrics = compute(_aligned(changes, volumes, flows), 20)
        verdict = judge(metrics, stale=False)
        self.assertEqual(verdict["lean"], "bullish")
        self.assertEqual(verdict["level"], 1)
        self.assertEqual(verdict["flow_state"], "out_strong")
        self.assertEqual(verdict["certainty"], "low")   # 大资金比平时明显更流出，把握降一档

    def test_habitual_net_selling_is_not_treated_as_opposite(self):
        """大单净卖是常态（约六成窗口）：一只票一直小幅净流出，这一段和平时差不多，不能因为「大单净卖」就降把握度。"""
        changes = [1.0] * 260
        volumes = [100.0] * 240 + [200.0] * 20
        flows = _noisy(-3.0, 260)                                       # 一直小幅净流出，上下有波动
        metrics = compute(_aligned(changes, volumes, flows), 20)
        verdict = judge(metrics, stale=False)
        self.assertLess(metrics["flow"]["big"], 0)
        self.assertEqual(verdict["flow_state"], "normal")
        self.assertEqual(verdict["certainty"], "high")

    def test_follow_compares_big_money_with_the_stocks_own_past_not_with_day_counts(self):
        changes = [1.0] * 260
        volumes = [100.0] * 240 + [200.0] * 20
        out = compute(_aligned(changes, volumes, _wobble(1.0, 240) + [(-5.0, -5.0, -1.0, -1.0)] * 20), 20)
        item = next(i for i in follow(out)["items"] if i["key"] == "big_money")
        self.assertEqual(item["status"], "disagree")                    # 涨，但大资金比平时明显更流出
        self.assertIn("比平时明显更流出", item["brief"])
        flat = compute(_aligned(changes, volumes, _noisy(1.0, 260)), 20)
        calm = next(i for i in follow(flat)["items"] if i["key"] == "big_money")
        self.assertEqual(calm["status"], "neutral")

    def test_synthesis_gets_the_relative_strength_as_a_fact(self):
        from app.synthesis.producer.adapters import volume_flow as adapt
        changes = [1.0] * 260
        volumes = [100.0] * 240 + [200.0] * 20
        data = _aligned(changes, volumes, _wobble(1.0, 240) + [(20.0, 10.0, -5.0, -1.0)] * 20)
        payload = build_detail(Stock("600519.SH", "600519", "贵州茅台", "1.600519", "SH600519", industry="白酒"), 5, aligned=data, today=date.fromisoformat(data["as_of"]))
        snap = adapt(payload)
        self.assertEqual(snap.facts["flow.vs_self"], "in_strong")

    def test_a_move_under_one_percent_is_not_a_price_pattern(self):
        self.assertEqual(price_pattern(0.4, 1.8), "none")
        self.assertEqual(price_pattern(-0.9, 0.5), "none")
        self.assertEqual(price_pattern(1.0, 1.8), "up_vol_up")
        self.assertEqual(price_pattern(-1.2, 1.8), "down_vol_up")

    def test_flow_state_bands(self):
        self.assertEqual([flow_state(z) for z in (None, 2.0, 1.0, 0.0, -1.0, -2.0)], ["", "in_strong", "in", "normal", "out", "out_strong"])

    def test_short_history_gives_no_relative_strength_and_no_flow_bonus(self):
        changes = [1.0] * 60 + [1.0] * 20
        volumes = [100.0] * 60 + [200.0] * 20
        flows = [(1.0, 1.0, -1.0, -1.0)] * 70 + [(20.0, 10.0, -5.0, -1.0)] * 10   # 平时一成不变：没有波动可比
        metrics = compute(_aligned(changes, volumes, flows), 20)
        self.assertIsNone(metrics["flow"]["rate_z"])
        verdict = judge(metrics, stale=False)
        self.assertEqual(verdict["flow_state"], "")
        self.assertEqual(verdict["level"], 1)                           # 没有资金确认加分

    def test_reference_windows_do_not_overlap_the_current_one(self):
        changes = [1.0] * 100
        volumes = [100.0] * 100
        calm = _wobble(1.0, 100)
        spike = calm[:-5] + [(50.0, 50.0, -5.0, -1.0)] * 5          # 只改最后 5 天
        base = compute(_aligned(changes, volumes, calm), 5)["flow"]
        hot = compute(_aligned(changes, volumes, spike), 5)["flow"]
        self.assertEqual(base["rate_n"], hot["rate_n"])
        self.assertLess(abs(base["rate_z"]), 3)
        self.assertGreater(hot["rate_z"], 5)

    def test_negative_concentration_is_empty(self):
        changes = [1.0] * 5
        volumes = [100.0] * 5
        flows = [(-30.0, 50.0, -5.0, -1.0)] * 5
        metrics = compute(_aligned(changes, volumes, flows), 5)
        self.assertIsNone(metrics["flow"]["concentration"])
        self.assertEqual(
            metrics["signals"]["details"]["flow_concentration"],
            "超大单净流出，流入全部来自大单，不存在集中风险",
        )

    def test_big_outflow_concentration_is_empty(self):
        changes = [1.0] * 5
        volumes = [100.0] * 5
        flows = [(-20.0, -10.0, 5.0, 1.0)] * 5
        metrics = compute(_aligned(changes, volumes, flows), 5)
        self.assertIsNone(metrics["flow"]["concentration"])
        self.assertEqual(metrics["signals"]["details"]["flow_concentration"], "大资金净流出，不计算集中度")

    def test_latest_outflow_day_is_turning_out(self):
        from app.services.volume_flow.rules import flow_trend

        days = [{"date": f"2026-10-{index:02d}", "net": 100} for index in range(1, 10)]
        days.append({"date": "2026-10-10", "net": -50})
        self.assertEqual(flow_trend(days), "turning_out")

    def test_v_shape_is_turning_in(self):
        from app.services.volume_flow.rules import flow_structure, flow_trend

        # 前半流出、后半流入
        days = [{"net": -10}] * 5 + [{"net": 12}] * 5
        self.assertEqual(flow_trend(days), "turning_in")
        # 末日由负翻正
        flip = [{"net": -8}] * 8 + [{"net": -3}, {"net": 5}]
        self.assertEqual(flow_trend(flip), "turning_in")
        # 超大单强、中单反向：方向仍跟大额，把握降低
        structure = flow_structure(30.0, 10.0, -20.0)
        self.assertEqual(structure["direction"], "in")
        self.assertEqual(structure["medium"], "against")
        self.assertLess(structure["certainty_delta"], 0)

    def test_robust_z_agrees_in_direction_with_mean_on_clean_spike(self):
        from app.services.volume_flow.metrics import _robust_z

        ref = [1.0 + (i % 5) * 0.1 for i in range(80)]
        z = _robust_z(8.0, ref)
        self.assertIsNotNone(z)
        self.assertGreater(z, 3)

    def test_diagnosis_headline_caps_at_fifty(self):
        from app.flow_review.tools import diagnosis_headline
        from app.services.volume_flow.rules import DIAGNOSIS_MAX_CHARS

        text = diagnosis_headline(
            {"text": "资金回升"},
            {"state_text": "比自己平时明显更流入"},
            {"text": "加权后大额偏流入，中单反向略降把握"},
            {"text": "价涨资弱"},
            mode="diagnosis",
        )
        self.assertLessEqual(len(text), DIAGNOSIS_MAX_CHARS)
        reply = diagnosis_headline(
            {"text": "资金回升"},
            {"state_text": "比自己平时明显更流入"},
            {"text": "加权后大额偏流入，中单反向略降把握"},
            {"text": "价涨资弱"},
            mode="reply",
        )
        self.assertGreaterEqual(len(reply), len(text))

    def test_tools_short_horizon_uses_window_three(self):
        from app.flow_review.tools import compute_flow_level, resolve_horizon

        self.assertEqual(resolve_horizon("short")["window"], 3)
        changes = [1.0] * 120
        volumes = [100.0] * 120
        flows = _wobble(1.0, 100) + [(20.0, 10.0, -5.0, -1.0)] * 20
        level = compute_flow_level(_aligned(changes, volumes, flows), horizon="short")
        self.assertEqual(level["window"], 3)
        self.assertEqual(level["rule_version"], "volume_price-6")

    def test_prior_peak_skips_the_previous_session(self):
        closes = [10] * 10 + [12, 11, 10, 11, 13, 14]
        volumes = [100] * 15 + [50]
        changes = [0.1] * 16
        flows = [(1.0, 1.0, -1.0, -1.0)] * 16
        data = _aligned(changes, volumes, flows)
        for row, close, volume in zip(data["bars"], closes, volumes):
            row["close"] = close
            row["volume"] = volume
        metrics = compute(data, 16)
        hints = {item["type"]: item for item in judge(metrics, stale=False)["hints"]}
        self.assertIn("前高", hints["pv_top_divergence"]["detail_text"])
        self.assertNotIn(data["bars"][-2]["date"], hints["pv_top_divergence"]["detail_text"])
        self.assertIn("RSI", hints["indicator_top_divergence"]["detail_text"])

    def test_detail_blocks_and_stale_data(self):
        stock = Stock("600519.SH", "600519", "贵州茅台", "1.600519", "SH600519", industry="白酒")
        fresh = _aligned([1.0, -1.0, 2.0], [100, 80, 120], [(10, 5, -3, -1)] * 3)
        payload = build_detail(stock, 3, aligned=fresh, today=date(2026, 6, 4))
        self.assertNotIn("status", payload)
        self.assertEqual(payload["meta"]["window"], 3)
        self.assertEqual(payload["meta"]["state"], "ok")
        self.assertEqual(payload["meta"]["rule_version"], "volume_price-6")
        self.assertIn("+08:00", payload["meta"]["updated_at"])
        self.assertTrue(payload["meta"]["status_text"].startswith("昨收"))
        self.assertEqual(len(payload["price_volume"]["bars"]), 3)
        self.assertEqual(payload["price_volume"]["chart"]["days"], 3)  # 样本只有 3 根，图跟着缩
        self.assertEqual(payload["price_volume"]["bars"][0]["chg"], 0.01)
        self.assertEqual(payload["verdict"]["lean"], "unclear")
        self.assertEqual(payload["verdict"]["level"], 0)
        self.assertIsNone(payload["verdict"]["certainty"])
        self.assertIn("不构成投资建议", payload["disclaimer_text"])
        self.assertEqual(payload["price_volume"]["pattern"], "none")
        self.assertEqual(payload["meta"]["sources"]["flow"], "东方财富")
        self.assertEqual(len(payload["position_hints"]["items"]), 6)   # build_detail 的完整结果，给综合判断和体检读
        self.assertNotIn("position_hints", public(payload))
        self.assertIn("相对量能", payload["windows"]["formula_text"])
        for gone, parent in (("ai_status", payload["verdict"]), ("chart_alt_text", payload["price_volume"]),
                             ("in_days", payload["flow_daily"]), ("out_days", payload["flow_daily"]),
                             ("marked_dates", payload["flow_daily"]), ("hit_count", payload["position_hints"])):
            self.assertNotIn(gone, parent)
        self.assertNotIn("is_up", payload["price_volume"]["bars"][0])
        self.assertLessEqual(len(payload["verdict"]["text"]), 50)
        self.assertNotIn("None", payload["verdict"]["text"])
        self.assertNotIn("还要看后面", payload["verdict"]["text"])
        self.assertTrue(payload["verdict"]["text"])  # 诊断页头：路径/力度/结构，≤50 字

        stale = dict(fresh)
        stale["as_of"] = "2026-06-30"
        old = build_detail(stock, 3, aligned=stale, today=date(2026, 10, 27))
        self.assertEqual(old["meta"]["state"], "expired")
        self.assertEqual(old["verdict"]["lean"], "")
        self.assertEqual(old["verdict"]["status_text"], "数据过期")
        self.assertIn("已过期", old["meta"]["status_text"])

    def _metrics(self, changes, volumes, flows):
        return compute(_aligned(changes, volumes, flows), 20)

    def test_follow_up_move_with_quiet_volume_still_has_followers(self):
        # 量能平稳 → 形态规则判不出，但涨时量更大、大资金天天流入 → 有人跟，只缺放量
        changes = [0.0] * 60 + [1.0, 0.5, 1.0, -0.2, 0.8] * 4
        volumes = [100.0] * 60 + [110.0, 110.0, 110.0, 80.0, 110.0] * 4
        metrics = self._metrics(changes, volumes, [(20.0, 10.0, -5.0, -1.0)] * 80)
        block = follow(metrics)
        verdict = judge(metrics, stale=False)
        self.assertEqual(verdict["lean"], "unclear")
        refined = refine(verdict, block, metrics)
        self.assertEqual((refined["lean"], refined["level"], refined["certainty"]), ("bullish", 1, "low"))
        self.assertEqual(refined["pattern"], "none")
        self.assertEqual(block["move"], "up")
        self.assertEqual(block["level"], "yes")
        self.assertTrue(block["label"].startswith("有人跟"))
        status = {item["key"]: item["status"] for item in block["items"]}
        self.assertEqual(status["volume"], "neutral")
        self.assertEqual(status["up_down"], "agree")
        self.assertEqual(status["big_money"], "agree")
        self.assertNotIn("turnover", status)
        self.assertIn("但总量没放大", block["brief"])
        self.assertEqual(refine(verdict, {**block, "level": "weak"}, metrics)["lean"], "unclear")

    def test_follow_up_move_against_money_is_none(self):
        changes = [0.0] * 60 + [1.0, 0.5, 1.0, -0.2, 0.8] * 4
        volumes = [100.0] * 60 + [60.0, 60.0, 60.0, 120.0, 60.0] * 4
        metrics = self._metrics(changes, volumes, [(-20.0, -10.0, 5.0, 1.0)] * 80)
        block = follow(metrics)
        self.assertEqual(block["level"], "none")
        self.assertEqual(block["label"], "没人跟，涨势缺支撑")

    def test_follow_down_move_reads_as_selling_pressure(self):
        changes = [0.0] * 60 + [-1.0, -0.5, -1.0, 0.2, -0.8] * 4
        volumes = [100.0] * 60 + [160.0, 160.0, 160.0, 90.0, 160.0] * 4
        metrics = self._metrics(changes, volumes, [(-20.0, -10.0, 5.0, 1.0)] * 80)
        block = follow(metrics)
        self.assertEqual(block["move"], "down")
        self.assertEqual(block["level"], "strong")
        self.assertIn("抛压", block["label"])

    def test_quiet_volume_down_move_refines_to_bearish(self):
        changes = [0.0] * 60 + [-1.0, -0.5, -1.0, 0.2, -0.8] * 4
        volumes = [100.0] * 60 + [110.0, 110.0, 110.0, 70.0, 110.0] * 4
        metrics = self._metrics(changes, volumes, [(-20.0, -10.0, 5.0, 1.0)] * 80)
        refined = refine(judge(metrics, stale=False), follow(metrics), metrics)
        self.assertEqual((refined["lean"], refined["level"]), ("bearish", 1))

    def test_expanded_volume_is_left_to_pattern_rule(self):
        changes = [0.0] * 60 + [1.0] * 20
        volumes = [100.0] * 60 + [200.0] * 20
        metrics = self._metrics(changes, volumes, [(20.0, 10.0, -5.0, -1.0)] * 80)
        verdict = judge(metrics, stale=False)
        self.assertEqual(refine(verdict, follow(metrics), metrics), verdict)

    def test_follow_flat_and_missing_flow(self):
        flat = self._metrics([0.0] * 80, [100.0] * 80, [(1.0, 1.0, 1.0, 1.0)] * 80)
        self.assertEqual(follow(flat)["level"], "flat")
        changes = [0.0] * 60 + [1.0, 0.5, 1.0, -0.2, 0.8] * 4
        metrics = self._metrics(changes, [100.0] * 80, [(None, None, None, None)] * 80)
        metrics["daily"] = {"days": [], "in_days": 0, "out_days": 0}
        metrics["flow"]["big"] = None
        metrics["price"]["up_down_vol_ratio"] = None
        self.assertEqual(follow(metrics)["level"], "unknown")

    def test_detail_exposes_follow_and_stale_has_none(self):
        stock = Stock("600519.SH", "600519", "贵州茅台", "1.600519", "SH600519", industry="白酒")
        changes = [0.0] * 60 + [1.0, 0.5, 1.0, -0.2, 0.8] * 4
        volumes = [100.0] * 60 + [110.0, 110.0, 110.0, 80.0, 110.0] * 4
        data = _aligned(changes, volumes, [(20.0, 10.0, -5.0, -1.0)] * 80)
        payload = build_detail(stock, 20, aligned=data, today=date.fromisoformat(data["as_of"]))
        verdict = payload["verdict"]
        self.assertEqual(verdict["lean"], "bullish")
        self.assertEqual(verdict["level"], 1)
        self.assertEqual(verdict["certainty"], "low")
        self.assertEqual((verdict["source"], verdict["status"], verdict["status_text"]), ("rule", "support", "偏多"))
        self.assertNotIn("ai_text", verdict)
        self.assertEqual(payload["price_volume"]["pattern"], "none")
        self.assertEqual(payload["follow"]["level"], "yes")
        self.assertEqual(set(payload["follow"]), {"level", "label", "move", "items"})
        self.assertEqual(set(payload["follow"]["items"][0]), {"key", "name", "status", "text"})
        by_key = {item["key"]: item for item in payload["follow"]["items"]}
        self.assertEqual(by_key["short_windows"]["status"], "info")
        self.assertIn("近 3 日", by_key["short_windows"]["text"])
        self.assertIn("近 20 日", by_key["short_windows"]["text"])
        self.assertEqual([row["days"] for row in payload["relative_strength"]["windows"]], [3, 5, 20])
        self.assertEqual(len(payload["relative_strength"]["windows"][0]["bars"]), 3)
        self.assertEqual(payload["price_volume"]["chart"]["days"], 20)
        self.assertEqual(len(payload["price_volume"]["chart"]["bars"]), 20)
        self.assertEqual(len(payload["price_volume"]["bars"]), 20)  # 本用例主窗口也是 20
        # 对外响应里 verdict 和 headline 同一套字段；lean/level/certainty 只给综合判断
        self.assertEqual(set(public(payload)["verdict"]), {"source", "status", "status_text", "text"})
        self.assertLessEqual(len(verdict["text"]), 50)
        self.assertTrue(any(token in verdict["text"] for token in ("流入", "流出", "资金", "价资")))
        self.assertNotIn("缺少增量资金确认", verdict["text"])
        stale = dict(data)
        stale["as_of"] = "2026-01-01"
        old = build_detail(stock, 20, aligned=stale, today=date(2026, 10, 27))
        self.assertEqual(old["verdict"]["lean"], "")
        self.assertIsNone(old["follow"])

    def test_hk_is_refused(self):
        stock = Stock("00700.HK", "00700", "腾讯控股", "116.00700", "00700")
        with self.assertRaises(RuntimeError):
            build_detail(stock, aligned={"bars": [{"date": "2026-10-26"}]})


class VolumeFlowRouteTest(unittest.TestCase):
    def setUp(self):
        self.client = create_app().test_client()

    def test_window_must_be_positive(self):
        response = self.client.get("/api/v1/stocks/600519.SH/dimensions/volume_flow/detail?window=0")
        self.assertEqual(response.status_code, 400)

    @patch("app.api.v1.resolve_stock", return_value=None)
    def test_unknown_stock(self, _resolve):
        response = self.client.get("/api/v1/stocks/000000.SH/dimensions/volume_flow/detail")
        self.assertEqual(response.status_code, 404)

    @patch("app.services.volume_flow.build_detail", return_value={"status": "ok", "meta": {"window": 5}, "verdict": {"source": "rule", "status": "neutral", "status_text": "不明", "text": "x", "lean": "unclear", "level": 0, "certainty": None}})
    @patch("app.api.v1.resolve_stock")
    def test_detail_ok(self, resolve, build):
        resolve.return_value = Stock("600519.SH", "600519", "贵州茅台", "1.600519", "SH600519")
        response = self.client.get("/api/v1/stocks/600519.SH/dimensions/volume_flow/detail?window=5")
        body = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["meta"]["window"], 5)
        self.assertEqual(build.call_args.args[1], 5)
        self.assertEqual(set(body["verdict"]), {"source", "status", "status_text", "text"})


if __name__ == "__main__":
    unittest.main()
