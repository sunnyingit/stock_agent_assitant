import copy
import json
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.finance_detail import build_detail, metrics as M, rules
from app.services.finance_detail.detail import public
from app.tools.stock import Stock

FIXTURE = Path(__file__).parent / "fixtures" / "finance_000021.json"
Q1, H1, Q3 = date(2026, 5, 10), date(2026, 9, 5), date(2026, 11, 5)   # 各自披露后十天左右，只看得到当时已披露的数据


def snapshot() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def stock() -> Stock:
    return Stock("000021.SZ", "000021", "深科技", "0.000021", "SZ000021", industry="元器件")


def metrics(day: date, snap: dict | None = None) -> dict:
    return M.compute(snap or snapshot(), day)


class RegressionFixtureTest(unittest.TestCase):
    """深科技 2026 年三份财报：设计稿里写明的预期。"""

    def test_q1_deducted_profit_is_about_59_percent_of_net_profit(self):
        m = metrics(Q1)
        self.assertAlmostEqual(m["quality"]["ded_to_np"], 0.59, delta=0.01)
        hard = rules.bounds(m)
        self.assertTrue(hard["q4"]["forbid_support"])          # 第 4 题不能判偏正面
        self.assertFalse(hard["q4"]["force_warn"])

    def test_h1_financial_expense_flips_and_cash_flow_forces_a_warning(self):
        m = metrics(H1)
        self.assertAlmostEqual(m["quality"]["fin_exp_before"] / 1e8, -3.35, delta=0.02)
        self.assertAlmostEqual(m["quality"]["fin_exp"] / 1e8, 2.78, delta=0.02)
        self.assertGreater(m["quality"]["fin_exp_shift_to_np"], 0.3)        # 第 5 题要能识别汇兑和衍生品的大幅变动
        hard = rules.bounds(m)
        self.assertTrue(hard["q7"]["forbid_support"])
        self.assertTrue(hard["q7"]["force_warn"])                          # 第 7 题必须需警惕

    def test_q3_single_quarter_shows_the_scissors_that_cumulative_numbers_hide(self):
        m = metrics(Q3)
        g = m["growth"]
        self.assertAlmostEqual(g["q_rev_yoy"], 0.31, delta=0.01)           # 单季营收约 +31%
        self.assertAlmostEqual(g["q_np_yoy"], 0.0, delta=0.01)             # 归母基本持平
        self.assertAlmostEqual(g["cum_rev_yoy"], 0.14, delta=0.01)         # 累计口径看起来平稳
        self.assertGreater(g["scissors_pp"], 0.20)
        self.assertEqual(m["data"]["single_source"], "tushare")
        self.assertTrue(rules.bounds(m)["q8"]["force_warn"])               # 应收和存货增速都跑在营收、成本前面

    def test_seasonal_adjusted_qoq_needs_three_years_and_flags_only_big_deviations(self):
        m = metrics(Q3)
        self.assertTrue(m["growth"]["adj_available"])
        self.assertEqual(m["growth"]["adj_state"], "accelerating")          # 本季环比比往年同季高出 10 个百分点以上
        short = snapshot()
        short["income_q"] = [r for r in short["income_q"] if r["end_date"] >= "20250101"]
        short["income_c"] = [r for r in short["income_c"] if r["end_date"] >= "20250101"]
        m = metrics(Q3, short)
        self.assertFalse(m["growth"]["adj_available"])
        self.assertIsNone(m["growth"]["qoq_rev_adj"])                       # 历史不足只报原始环比
        self.assertIsNotNone(m["growth"]["qoq_rev"])


class DetailShapeTest(unittest.TestCase):
    def test_full_payload_keeps_internals_and_public_hides_them(self):
        payload = build_detail(stock(), snapshot(), Q3)
        self.assertEqual(payload["meta"]["state"], "ok")
        self.assertEqual(payload["meta"]["report"]["days_since"], 6)
        self.assertIn("距最近财报 6 天", payload["verdict"]["text"])
        self.assertIn(payload["verdict"]["lean"], {"bullish", "bearish", "neutral"})
        self.assertEqual([c["key"] for c in payload["cells"]], ["growth", "quality", "expectation"])
        self.assertEqual(len(payload["quarters"]), 8)
        shown = public(payload)
        self.assertEqual(set(shown["verdict"]), {"source", "status", "status_text", "text"})
        self.assertNotIn("inputs", shown)
        self.assertIn("inputs", payload)
        for gone in ("surprise", "chain", "risk_checks", "red_line", "growth", "table", "next_disclosure", "preview_basis"):
            self.assertNotIn(gone, shown)                                  # 旧区块已下线

    def test_rule_verdict_follows_growth_and_quality(self):
        for day, quality in ((Q1, "poor"), (H1, "poor"), (Q3, "poor")):
            payload = build_detail(stock(), snapshot(), day)
            cells = {c["key"]: c["level"] for c in payload["cells"]}
            self.assertEqual((cells["growth"], cells["quality"]), ("up", quality))
            self.assertEqual(payload["verdict"]["status_text"], "不明")      # 增长向上、含金量偏弱：互相抵消

    def test_old_reports_become_background_after_sixty_days_without_a_new_forecast(self):
        payload = build_detail(stock(), snapshot(), date(2027, 1, 20))
        self.assertGreater(payload["meta"]["report"]["days_since"], 60)
        self.assertEqual((payload["verdict"]["lean"], payload["verdict"]["level"], payload["verdict"]["certainty"]), ("neutral", 0, None))

    def test_not_applicable_and_missing_data_return_insufficient(self):
        bank = snapshot()
        bank["industry"] = "银行"
        self.assertEqual(build_detail(stock(), bank, Q3)["meta"]["state"], "insufficient")
        empty = {**snapshot(), "income_c": [], "income_q": []}
        payload = build_detail(stock(), empty, Q3)
        self.assertEqual((payload["meta"]["state"], payload["verdict"], payload["cells"]), ("insufficient", None, None))
        hk = Stock("00700.HK", "00700", "腾讯控股", "116.00700", "00700")
        self.assertEqual(build_detail(hk, None, Q3)["meta"]["state"], "insufficient")


class TimeWeightTest(unittest.TestCase):
    """设计稿第六节：距最近披露 0～30 天正常，31～60 天减半，60 天以上且没有新预告只作背景；刚发了新预告就优先读预告。"""

    def weight(self, day, snap=None):
        return rules.time_weight(M.compute(snap or snapshot(), day))

    def test_weight_by_days_since_disclosure(self):
        self.assertEqual(self.weight(date(2026, 11, 5)), 1.0)        # 三季报 10-30 披露，6 天
        self.assertEqual(self.weight(date(2026, 11, 29)), 1.0)       # 30 天
        self.assertEqual(self.weight(date(2026, 12, 15)), 0.5)       # 46 天
        self.assertEqual(self.weight(date(2026, 12, 29)), 0.5)       # 60 天
        self.assertEqual(self.weight(date(2027, 1, 5)), 0.0)         # 67 天，且没有新预告

    def with_forecast(self, kind: str, ann_date: str) -> dict:
        snap = snapshot()
        snap["forecast"] = [{"ts_code": "000021.SZ", "ann_date": ann_date, "end_date": "20261231", "type": kind, "net_profit_min": 90000, "net_profit_max": 100000,
                             "p_change_min": None, "p_change_max": None, "summary": "", "change_reason": ""}]
        return snap

    def test_a_fresh_forecast_restores_full_weight_and_decides_the_direction(self):
        day = date(2027, 1, 20)                                      # 三季报已过 82 天，本来只作背景
        self.assertEqual(self.weight(day), 0.0)
        up = build_detail(stock(), self.with_forecast("预增", "20270115"), day)
        down = build_detail(stock(), self.with_forecast("预减", "20270115"), day)
        self.assertEqual((up["verdict"]["lean"], up["verdict"]["level"], up["verdict"]["certainty"]), ("bullish", 1, "low"))
        self.assertEqual(down["verdict"]["lean"], "bearish")
        self.assertEqual(up["inputs"]["time_weight"], 1.0)
        self.assertEqual(build_detail(stock(), self.with_forecast("不确定", "20270115"), day)["verdict"]["lean"], "neutral")

    def test_an_old_forecast_does_not_count_as_fresh(self):
        old = self.with_forecast("预增", "20261201")                  # 31 天前发的预告
        self.assertEqual(self.weight(date(2027, 1, 20), old), 0.0)
        self.assertEqual(build_detail(stock(), old, date(2027, 1, 20))["verdict"]["lean"], "neutral")

    def test_the_detail_hands_the_weight_to_the_synthesis(self):
        self.assertEqual(build_detail(stock(), snapshot(), date(2026, 12, 15))["inputs"]["time_weight"], 0.5)


class MetricsEdgeCaseTest(unittest.TestCase):
    def test_single_quarter_falls_back_to_cumulative_difference(self):
        snap = snapshot()
        with_tushare = metrics(Q3, snap)
        snap["income_q"] = []
        diffed = metrics(Q3, snap)
        self.assertEqual(diffed["data"]["single_source"], "diff")
        self.assertAlmostEqual(diffed["growth"]["q_rev"], with_tushare["growth"]["q_rev"], delta=1)

    def test_losses_report_the_change_in_amount_instead_of_a_percentage(self):
        snap = snapshot()
        for row in snap["income_q"] + snap["income_c"]:
            if row["end_date"] == "20250930":
                row["n_income_attr_p"] = -50_000_000.0     # 去年同期亏损
        m = metrics(Q3, snap)
        self.assertIsNone(m["growth"]["q_np_yoy"])
        self.assertTrue(m["growth"]["loss_prior"])
        self.assertFalse(m["growth"]["loss_now"])
        self.assertGreater(m["growth"]["loss_change"], 0)
        self.assertEqual(rules.growth_level(m), "up")                   # 去年同期亏损、本期已盈利，是扭亏不是仍在亏损
        self.assertIn("本期盈利", rules._growth_note(m))

    def test_restatement_needs_two_versions_that_disagree(self):
        snap = snapshot()
        self.assertFalse(metrics(Q3, snap)["data"]["restated"])             # 两版一样是正常的重复记录
        changed = copy.deepcopy(snap)
        for row in changed["income_c"]:
            if row["end_date"] == "20260930" and str(row["update_flag"]) == "0":
                row["revenue"] = row["revenue"] * 0.9
        self.assertTrue(metrics(Q3, changed)["data"]["restated"])

    def test_future_disclosures_are_invisible(self):
        m = metrics(date(2026, 4, 15))                                      # 年报和一季报都在 4 月 29 日才披露：这时最新只有去年三季报
        self.assertEqual(m["report_end"], "20250930")
        self.assertEqual(metrics(date(2026, 5, 1))["report_end"], "20260331")

    def test_thresholds_can_be_overridden_per_industry(self):
        from app.services.finance_detail import config

        self.assertEqual(config.thresholds("general")["ded_to_np_support_min"], 0.70)
        original = dict(config.OVERRIDES["realestate"])
        config.OVERRIDES["realestate"] = {"ded_to_np_support_min": 0.5}
        try:
            self.assertEqual(config.thresholds("realestate")["ded_to_np_support_min"], 0.5)
            self.assertEqual(config.thresholds("general")["ded_to_np_support_min"], 0.70)
        finally:
            config.OVERRIDES["realestate"] = original

    def test_a_later_restatement_wins_over_an_earlier_correction(self):
        rows = [
            {"end_date": "20241231", "ann_date": "20250430", "update_flag": "1", "revenue": 100.0},
            {"end_date": "20241231", "ann_date": "20250301", "update_flag": "1", "revenue": 80.0},
            {"end_date": "20241231", "ann_date": "20250430", "update_flag": "0", "revenue": 70.0},
        ]
        self.assertEqual(M._by_end(rows)["20241231"]["revenue"], 100.0)

    def test_reaction_day_one_is_only_the_disclosure_session(self):
        snap = snapshot()
        snap["bars"] = [
            {"trade_date": "20260825", "pct_chg": 1.0, "amount": 1.0e6},
            {"trade_date": "20260826", "pct_chg": -2.0, "amount": 1.0e6},
            {"trade_date": "20260827", "pct_chg": 9.0, "amount": 1.0e6},
        ]
        snap["sector"] = {"name": "测试", "rows": [
            {"trade_date": "20260825", "pct_change": 0.0},
            {"trade_date": "20260826", "pct_change": 0.0},
            {"trade_date": "20260827", "pct_change": 0.0},
        ]}
        reaction = M._reaction(snap, date(2026, 8, 26), date(2026, 8, 27))
        self.assertAlmostEqual(reaction["after"][1]["stock"], -0.02, places=6)
        self.assertAlmostEqual(reaction["after"][1]["stock"] * 100, -2.0, places=4)

    def test_receivables_financing_is_part_of_receivables(self):
        snap = snapshot()
        for row in snap["balance"]:
            if row["end_date"] == "20260930":
                row["receiv_financing"] = 1_000_000_000.0
        widened = metrics(Q3, snap)["balance"]["ar"]
        base = metrics(Q3)["balance"]["ar"]
        self.assertAlmostEqual(widened - base, 1_000_000_000.0, delta=1)

    def test_turnaround_is_not_described_as_still_losing_money(self):
        from app.finance_review import facts as fact_sheet
        from app.finance_review import template

        snap = snapshot()
        for row in snap["income_q"] + snap["income_c"]:
            if row["end_date"] == "20250930":
                row["n_income_attr_p"] = -50_000_000.0
        m = metrics(Q3, snap)
        facts, d = fact_sheet.build(m)
        text = {fact.id: fact.text for fact in facts}
        row = next(item for item in template.answers(d, text) if item["key"] == "q2")
        self.assertIn("本期已盈利", row["answer"])
        self.assertNotIn("处在亏损中", row["answer"])
        self.assertEqual(row["status"], "support")


if __name__ == "__main__":
    unittest.main()
