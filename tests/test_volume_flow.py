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
from app.services.volume_flow.rules import judge
from app.tools.stock import Stock


def _days(count, start=date(2026, 6, 1)):
    rows = []
    cursor = start
    while len(rows) < count:
        if cursor.weekday() < 5:
            rows.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return rows


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
        changes = [1.0] * 60 + [1.0] * 16 + [-0.2] * 4
        volumes = [100.0] * 60 + [200.0] * 16 + [50.0] * 4
        flows = [(1.0, 1.0, -1.0, -1.0)] * 70 + [(20.0, 10.0, -5.0, -1.0)] * 10
        metrics = compute(_aligned(changes, volumes, flows), 20)
        verdict = judge(metrics, stale=False)
        self.assertEqual(verdict["pattern"], "up_vol_up")
        self.assertEqual(verdict["lean"], "bullish")
        self.assertEqual(verdict["level"], 3)
        self.assertEqual(verdict["certainty"], "high")

    def test_opposite_flow_does_not_flip_direction(self):
        changes = [1.0] * 60 + [1.0] * 20
        volumes = [100.0] * 60 + [200.0] * 20
        flows = [(-5.0, -5.0, -1.0, -1.0)] * 80
        metrics = compute(_aligned(changes, volumes, flows), 20)
        verdict = judge(metrics, stale=False)
        self.assertEqual(verdict["lean"], "bullish")
        self.assertEqual(verdict["level"], 1)
        self.assertEqual(verdict["certainty"], "low")   # 大资金方向相反，把握降一档

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
        self.assertEqual(payload["meta"]["rule_version"], "volume_price-2")
        self.assertIn("+08:00", payload["meta"]["updated_at"])
        self.assertTrue(payload["meta"]["status_text"].startswith("昨收"))
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
        self.assertLessEqual(len(payload["verdict"]["text"]), 120)
        self.assertNotIn("None", payload["verdict"]["text"])
        self.assertNotIn("还要看后面", payload["verdict"]["text"])
        self.assertIn("大资金", payload["verdict"]["text"])

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
        # 对外响应里 verdict 和 headline 同一套字段；lean/level/certainty 只给综合判断
        self.assertEqual(set(public(payload)["verdict"]), {"source", "status", "status_text", "text"})
        self.assertIn("有人跟", verdict["text"])
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

    @patch("app.services.volume_flow.build_detail", return_value={"status": "ok", "meta": {"window": 20}, "verdict": {"source": "rule", "status": "neutral", "status_text": "不明", "text": "x", "lean": "unclear", "level": 0, "certainty": None}})
    @patch("app.api.v1.resolve_stock")
    def test_detail_ok(self, resolve, build):
        resolve.return_value = Stock("600519.SH", "600519", "贵州茅台", "1.600519", "SH600519")
        response = self.client.get("/api/v1/stocks/600519.SH/dimensions/volume_flow/detail?window=20")
        body = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["meta"]["window"], 20)
        self.assertEqual(build.call_args.args[1], 20)
        self.assertEqual(set(body["verdict"]), {"source", "status", "status_text", "text"})


if __name__ == "__main__":
    unittest.main()
