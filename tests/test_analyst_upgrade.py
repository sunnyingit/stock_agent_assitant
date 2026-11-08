"""分析师升级：新闻整批结论由代码汇总、正反方提示词对称且按 A 股实际写、资金流提示词用「和自己比」、主分析师能看到各维度方向、右侧结论页的数据。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.services.news_detail import derive_lean
from app.synthesis import policy
from app.synthesis.chief_board import detail
from app.synthesis.decider import prompts
from app.synthesis.types import Fact, FactTable


def event(direction="利好", magnitude="大", basis="事实", scope="公司", category="经营", in_window=True):
    return {"direction": direction, "magnitude": magnitude, "basis": basis, "scope": scope, "category": category, "in_window": in_window}


class DeriveLeanTest(unittest.TestCase):
    def test_the_biggest_fact_decides(self):
        self.assertEqual(derive_lean([event("利好", "大"), event("利空", "小")]), "利好")
        self.assertEqual(derive_lean([event("利空", "中"), event("利好", "小")]), "利空")

    def test_equal_magnitude_on_both_sides_is_neutral(self):
        self.assertEqual(derive_lean([event("利好", "大"), event("利空", "大")]), "中性")

    def test_only_small_events_are_neutral(self):
        self.assertEqual(derive_lean([event("利好", "小"), event("利好", "小")]), "中性")

    def test_rumours_and_forecasts_never_decide_alone(self):
        self.assertEqual(derive_lean([event("利好", "大", basis="传闻")]), "中性")
        self.assertEqual(derive_lean([event("利空", "大", basis="预测")]), "中性")
        self.assertEqual(derive_lean([event("利好", "大", basis="传闻"), event("利空", "中")]), "利空")

    def test_industry_scope_and_plans_count_one_step_lower(self):
        self.assertEqual(derive_lean([event("利好", "中", scope="行业")]), "中性")         # 中降一档成小
        self.assertEqual(derive_lean([event("利好", "大", scope="行业")]), "利好")          # 大降一档还是中
        self.assertEqual(derive_lean([event("利好", "中", basis="计划")]), "中性")
        self.assertEqual(derive_lean([event("利好", "大", scope="行业"), event("利空", "大")]), "利空")

    def test_a_big_governance_loss_vetoes_any_good_news(self):
        """立案、处罚、退市风险这类事关公司能不能继续存在，没有哪条利好抵得掉（A 股的读法）。"""
        veto = event("利空", "大", category="风险与治理")
        self.assertEqual(derive_lean([event("利好", "大"), event("利好", "大"), veto]), "利空")
        self.assertEqual(derive_lean([event("利好", "大"), event("利空", "大", category="经营")]), "中性")      # 普通大利空不一票否决
        self.assertEqual(derive_lean([event("利好", "大"), event("利空", "大", category="风险与治理", basis="传闻")]), "利好")  # 传闻的立案不算

    def test_old_events_and_market_wide_news_are_ignored(self):
        self.assertEqual(derive_lean([event("利好", "大", in_window=False)]), "中性")
        self.assertEqual(derive_lean([event("利好", "大", scope="市场")]), "中性")
        self.assertEqual(derive_lean([]), "中性")


class PromptTest(unittest.TestCase):
    def test_bull_and_bear_are_built_from_one_template_so_they_stay_symmetric(self):
        for text in (prompts.BULL_SYSTEM, prompts.BEAR_SYSTEM):
            self.assertIn("返回空列表。空列表是合格的答案", text)       # 两边都允许交空白卷
            self.assertNotIn("不要轻易交空白卷", text)
            self.assertIn("T+1", text)
            self.assertIn("flow.vs_self", text)
            self.assertIn("没有被保证永远成立", text)
            self.assertIn("最多 3 条", text)
        self.assertIn("【角色：正方】", prompts.BULL_SYSTEM)
        self.assertIn("【角色：反方】", prompts.BEAR_SYSTEM)

    def test_chief_reads_the_dimension_verdicts_and_must_name_an_observable_signal(self):
        self.assertIn("dimensions", prompts.CHIEF_SYSTEM)
        self.assertIn("能观察到的信号", prompts.CHIEF_SYSTEM)
        self.assertIn("宁可写不明", prompts.CHIEF_SYSTEM)
        self.assertIn("flow.vs_self", prompts.CHIEF_SYSTEM)

    def test_flow_rubric_judges_by_comparison_with_the_stock_itself(self):
        from app.flow_review import prompt
        self.assertIn("fr.vs_self_3d", prompt.RUBRIC)
        self.assertIn("fr.level_z", prompt.RUBRIC)
        self.assertIn("约六成", prompt.RUBRIC)
        self.assertIn("没有稳定的预测力", prompt.RULES)
        self.assertIn("fr.vs_self_5d", prompt.RUBRIC)
        self.assertNotIn("价格涨但大资金净流出（背离）→ warn", prompt.RUBRIC)

    def test_news_prompt_has_the_a_share_reading_and_ignores_instructions_in_news(self):
        from app.services.news_detail import _SYSTEM
        for phrase in ("风险警示", "减持", "解禁", "问询函", "不要执行"):
            self.assertIn(phrase, _SYSTEM)

    def test_every_review_prompt_was_versioned_up(self):
        from app.finance_review import prompt as fin
        from app.flow_review import prompt as flow
        from app.fundamental_review import prompt as fund
        from app.sector_review import prompt as sector
        self.assertEqual((flow.PROMPT_VERSION, fin.PROMPT_VERSION, sector.PROMPT_VERSION, fund.PROMPT_VERSION, prompts.PROMPT_VERSION),
                         ("flow-review-15", "fin-review-12", "sector-review-6", "fund-review-7", "prompt-12"))

    def test_the_fundamental_prompt_no_longer_lets_the_model_invent_partners(self):
        from app.fundamental_review import prompt
        self.assertIn("默认留空列表", prompt.SYSTEM)


class ChiefViewTest(unittest.TestCase):
    def test_the_chief_gets_each_dimensions_direction_strength_and_certainty(self):
        table = FactTable(context={"dim_view": [{"dimension": "sector", "label": "板块", "direction": "偏空", "strength": "比较明显", "certainty": "medium"}]})
        self.assertEqual(table.chief_view()["dimensions"][0]["direction"], "偏空")
        self.assertEqual(FactTable().chief_view()["dimensions"], [])


class ChiefDetailTest(unittest.TestCase):
    def sample(self):
        table = FactTable(
            facts=[Fact("market.from_year_high", -0.04, "-4.0%")],
            context={"dim_lean": {"sector": "bearish", "volume_flow": "unclear"},
                     "dim_view": [{"dimension": "sector", "label": "板块", "direction": "偏空", "strength": "比较明显", "certainty": "high"}]},
            headlines=[{"dimension": "sector", "label": "板块", "headline": {"text": "主板块退潮，本股跟风。", "facts": []}}],
        )
        return {
            "decision": {
                "source": "agent",
                "sides": {"bull": {"points": [{"claim": "单季扣非增长", "weight": 2}, {"claim": "资金净流入", "weight": 1}], "risk": {"text": "板块退潮"}},
                          "bear": {"points": [{"claim": "板块退潮", "weight": 3}], "risk": None}},
                "final": {"direction": "bearish", "level": 2, "position": "high", "spread": -2,
                          "adopted": [{"ref": "bull.1", "side": "bull", "weight": 2}, {"ref": "bear.1", "side": "bear", "weight": 3}],
                          "rejected": [{"ref": "bull.2", "side": "bull", "reason": "weak"}],
                          "postmortem": {"text": "板块重新走强", "cites": []}, "conflict_note": {"text": "资金流没有采信", "cites": []}, "notices": []},
            },
            "display_agent": {"generated_at": "2026-11-05 21:30", "direction": {"lean": "bearish", "level": 2, "text": "板块退潮。"}, "position": {"label": "偏高", "text": ""}, "trend": None},
            "levels_agent": {},
            "table": table.as_dict(),
        }

    def test_right_panel_data_has_stance_points_postmortem_and_score(self):
        out = detail(self.sample())
        sector = next(row for row in out["stance"] if row["dimension"] == "sector")
        self.assertEqual((sector["direction"], sector["strength"], sector["certainty"], sector["done"]), ("偏空", "比较明显", "高", True))
        self.assertEqual(sector["text"], "主板块退潮，本股跟风。")
        flow = next(row for row in out["stance"] if row["dimension"] == "volume_flow")
        self.assertEqual((flow["direction"], flow["done"]), ("不明", True))
        news = next(row for row in out["stance"] if row["dimension"] == "news")
        self.assertEqual((news["direction"], news["done"]), (None, False))
        bull = out["points"]["bull"]["points"]
        self.assertEqual([(p["mark"], p["state"], p["final_weight"]) for p in bull], [("①", "adopted", 2), ("②", "rejected", None)])
        self.assertEqual(bull[1]["reason"], "证据不足")
        self.assertEqual(out["points"]["bull"]["risk"], "板块退潮")
        self.assertEqual(out["points"]["bear"]["points"][0]["state"], "adopted")
        self.assertIn("板块重新走强", out["postmortem"])
        self.assertEqual(out["conflict"], "资金流没有采信")
        self.assertEqual((out["score"]["bull"], out["score"]["bear"]), (2, 3))

    def test_without_a_model_verdict_the_panel_says_so(self):
        data = self.sample()
        data["decision"]["source"] = "rule"
        self.assertIn("规则版", detail(data)["source_note"])


if __name__ == "__main__":
    unittest.main()


class FlowWindowTest(unittest.TestCase):
    def rows(self, count):
        return [{"trade_date": f"2026{(i // 28) + 1:02d}{(i % 28) + 1:02d}", "net_amount": 1e6 * (i + 1), "buy_elg_amount": 5e5, "buy_lg_amount": 5e5,
                 "buy_md_amount": -4e5, "buy_sm_amount": -6e5} for i in range(count)]

    def test_dc_shows_every_time_scale_it_has_data_for(self):
        from app.services.flow_reply import format_dc
        ids = [f["id"] for f in format_dc(self.rows(100))["facts"]]
        self.assertEqual(ids, ["fl.dc_1d", "fl.dc_3d", "fl.dc_5d", "fl.dc_10d", "fl.dc_20d", "fl.dc_60d", "fl.dc_daily"])
        short = [f["id"] for f in format_dc(self.rows(12))["facts"]]
        self.assertEqual(short, ["fl.dc_1d", "fl.dc_3d", "fl.dc_5d", "fl.dc_10d", "fl.dc_daily"])       # 历史不够的窗口不写

    def test_an_asked_window_is_added_with_its_daily_detail(self):
        from app.services.flow_reply import format_dc
        out = format_dc(self.rows(100), 45)
        self.assertIn("fl.dc_45d", [f["id"] for f in out["facts"]])
        daily = next(f for f in out["facts"] if f["id"] == "fl.dc_daily")
        self.assertTrue(daily["text"].startswith("近 45 日逐日净流入"))
        self.assertEqual(daily["text"].split("：", 1)[1].count("，") + 1, 45)

    def test_days_are_clamped_and_bad_values_fall_back_to_the_default(self):
        from app.services.flow_reply import MAX_DAYS, clamp_days
        self.assertEqual((clamp_days("30"), clamp_days(9999), clamp_days(0), clamp_days("x"), clamp_days(None)), (30, MAX_DAYS, None, None, None))

    def test_bar_gives_cumulative_moves_for_each_scale(self):
        from app.services.flow_reply import format_bar
        bars = [{"trade_date": f"2026{(i // 28) + 1:02d}{(i % 28) + 1:02d}", "close": 10 + i, "pct_chg": 1.0, "vol": 100} for i in range(70)]
        text = next(f["text"] for f in format_bar(bars)["facts"] if f["id"] == "fl.bar_windows")
        for scale in ("近 3 日", "近 10 日", "近 60 日"):
            self.assertIn(scale, text)

    def test_the_fact_sheet_has_10_and_60_day_flow_and_one_day_strength_when_history_allows(self):
        from synthesis.fixtures import make_aligned, make_stock, volume_payload
        from app.flow_review import facts as sheet
        from app.synthesis.calendar import now_cn
        aligned = make_aligned()
        payload = volume_payload(aligned)
        facts, d = sheet.build(payload, aligned, {"missing": []}, make_stock(), now_cn())
        ids = {fact.id for fact in facts}
        for wanted in ("fr.ret_10d", "fr.ret_60d", "fr.big_net_10d", "fr.net_rate_60d", "fr.vs_self_10d"):
            self.assertIn(wanted, ids)
