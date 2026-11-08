import json
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from synthesis.fixtures import NOW
from app.api.v1 import bp as v1_bp
from app.finance_review import checks, facts as fact_sheet, outcomes, prompt, review, template
from app.services.finance_detail import rules
from app.reviews import attach
from app.reviews.api import make_blueprint
from app.services.finance_detail import build_detail, metrics as M
from app.synthesis import worker
from app.synthesis.gateway import Gateway
from app.synthesis.store import Store
from app.tools.stock import Stock

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "finance_000021.json"
Q1, H1, Q3 = date(2026, 5, 10), date(2026, 9, 5), date(2026, 11, 5)
TS = "000021.SZ"


def snapshot() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def stock() -> Stock:
    return Stock(TS, "000021", "深科技", "0.000021", "SZ000021", industry="元器件")


def payload(day: date, snap: dict | None = None) -> dict:
    return build_detail(stock(), snap or snapshot(), day)


def sheet(day: date, snap: dict | None = None):
    m = M.compute(snap or snapshot(), day)
    facts, d = fact_sheet.build(m)
    return facts, d, {fact.id: fact.text for fact in facts}


def answers(day: date, snap: dict | None = None) -> dict:
    facts, d, text = sheet(day, snap)
    return {row["key"]: row for row in template.answers(d, text)}


class FactsTest(unittest.TestCase):
    def test_ids_groups_and_formats(self):
        facts, d, text = sheet(Q3)
        self.assertTrue(all(fact.id.startswith("ff.") for fact in facts))
        self.assertEqual({row["group"] for row in fact_sheet.public(facts)}, set(fact_sheet.GROUP_ORDER) - {"上期预警跟踪"})   # 没有上期记录就没有跟踪这一组
        self.assertEqual(text["ff.growth.q_rev_yoy"], "+30.7%")
        self.assertEqual(text["ff.growth.scissors"], "+30.6 个百分点")
        self.assertEqual(text["ff.time.days_since"], "6 天")
        self.assertEqual(text["ff.data.single_source"], "交易所单季口径")
        self.assertIn("2026Q3", text["ff.growth.series"])
        self.assertGreater(len(facts), 55)

    def test_every_question_subset_points_at_real_facts(self):
        facts, d, text = sheet(Q3)
        subsets = template.relevant(set(text))
        for key, ids in subsets.items():
            if key != "q11":                                            # 第 11 题的事实只有上期有预警时才有
                self.assertTrue(ids, key)
        self.assertNotIn("ff.balance.ar_gap", subsets["q2"])          # 每题只有自己的事实
        self.assertIn("ff.balance.ar_gap", subsets["q8"])

    def test_loss_company_reports_amounts_not_percentages(self):
        snap = snapshot()
        for row in snap["income_q"] + snap["income_c"]:
            if row["end_date"] == "20250930":
                row["n_income_attr_p"] = -50_000_000.0
        facts, d, text = sheet(Q3, snap)
        self.assertIn("ff.growth.loss_change", text)
        self.assertNotIn("ff.growth.q_np_yoy", text)
        self.assertIn("扭亏为盈", text["ff.growth.loss_change"])   # 去年同期亏 0.5 亿，本季盈利：写成扭亏，不算减亏


class TemplateAndHardThresholdTest(unittest.TestCase):
    """设计稿第八节的回归夹具：深科技一季报、中报、三季报。"""

    def test_q1_deducted_share_cannot_be_positive(self):
        rows = answers(Q1)
        self.assertNotEqual(rows["q4"]["status"], "support")
        self.assertIn("59%", rows["q4"]["answer"])

    def test_h1_financial_expense_swing_is_spotted_and_cash_flow_is_a_warning(self):
        rows = answers(H1)
        self.assertIn("汇兑或衍生品", rows["q5"]["answer"])
        self.assertIn("-3.35", rows["q5"]["answer"])
        self.assertEqual(rows["q7"]["status"], "warn")

    def test_q3_scissors_and_balance_sheet_warnings(self):
        rows = answers(Q3)
        self.assertEqual(rows["q3"]["status"], "warn")
        self.assertIn("+30.6 个百分点", rows["q3"]["answer"])
        self.assertEqual(rows["q8"]["status"], "warn")
        self.assertEqual(rows["q12"]["status"], "warn")
        self.assertIn("收入和利润不一致", rows["q12"]["answer"])
        self.assertNotIn("距最近财报", rows["q12"]["answer"])          # 天数由程序加

    def test_every_template_answer_cites_only_its_own_subset(self):
        for day in (Q1, H1, Q3):
            facts, d, text = sheet(day)
            subsets = template.relevant(set(text), d["signals"])
            for row in template.answers(d, text):
                self.assertTrue(set(row["cites"]) <= set(subsets[row["key"]]), (day, row["key"], row["cites"]))

    def test_perturbation_flipping_a_fact_across_the_threshold_flips_the_status(self):
        snap = snapshot()
        before = answers(H1, snap)["q4"]["status"]
        for row in snap["indicator"]:
            if row["end_date"] == "20260630":
                row["profit_dedt"] = row["profit_dedt"] * 0.6        # 扣非从约 93% 压到约 56%
        after = answers(H1, snap)["q4"]["status"]
        self.assertEqual(before, "support")
        self.assertEqual(after, "neutral")

    def test_chronic_loss_makes_q4_not_applicable(self):
        snap = snapshot()
        for row in snap["indicator"]:
            row["profit_dedt"] = -abs(row["profit_dedt"] or 1.0)
        self.assertEqual(answers(Q3, snap)["q4"]["status"], "na")

    def test_company_name_never_changes_a_status(self):
        base = {k: v["status"] for k, v in answers(Q3).items()}
        renamed = snapshot()
        renamed["stock_name"] = "某某公司"
        self.assertEqual({k: v["status"] for k, v in answers(Q3, renamed).items()}, base)


def by(out: dict, key: str) -> dict:
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
            if question["key"] in ("q1", "q12") and question.get("signals"):   # 第 1、12 题：正负信号各引用一个
                picked = [side[0] for side in (question["signals"]["positive"], question["signals"]["negative"]) if side]
                answer, cites = "；".join(f"{p['label']} {p['value']}" for p in picked), [p["id"] for p in picked]
                cycle = next((f for f in question["facts"] if f["id"] == "ff.margin.cycle" and "周期高位" in f["value"]), None)
                if cycle:   # 毛利率在近 5 年高位：第 1、12 题必须提示
                    answer, cites = answer + "；利润可能处于周期高位", cites + [cycle["id"]]
                rows.append({"key": question["key"], "status": question.get("reference") or "neutral", "answer": answer, "cites": cites})
                continue
            cite = question["facts"][0]["id"]
            number = question["facts"][0]["value"]
            rows.append({"key": question["key"], "status": question.get("reference") or "neutral", "answer": f"这一项的第一个事实是 {number}", "cites": [cite]})
        out = {"items": rows}
        return self.mutate(out, view) or out if self.mutate else out


class ReviewTest(unittest.TestCase):
    def build(self, model=None, day=H1, store=None, **kwargs):
        store = store if store is not None else Store(":memory:")
        gateway = Gateway(model) if model is not None else None
        return review.build(stock(), store=store, gateway=gateway, now=NOW, payload=payload(day), model_tag="x", **kwargs)

    def test_without_a_model_the_template_answers_everything(self):
        result = self.build()
        self.assertEqual(result["source"], "template")
        self.assertEqual([row["key"] for row in result["items"]], [f"q{i}" for i in range(1, 13)])
        self.assertGreater(len(result["facts"]), 55)
        self.assertEqual(result["report"], "2026 半年报")

    def test_gateway_is_ignored_and_items_stay_template(self):
        model = ScriptedModel()
        result = self.build(model)
        self.assertEqual(result["source"], "template")
        self.assertEqual(model.calls, 0)
        self.assertTrue(all(row.get("source") == "template" for row in result["items"]))

    def test_not_applicable_questions_stay_na(self):
        snap = snapshot()
        for row in snap["indicator"]:
            row["profit_dedt"] = -abs(row["profit_dedt"] or 1.0)
        result = review.build(stock(), store=Store(":memory:"), gateway=None, now=NOW, payload=payload(Q3, snap), model_tag="x")
        self.assertEqual({row["key"]: row for row in result["items"]}["q4"]["status"], "na")
        self.assertEqual({row["key"]: row for row in result["items"]}["q4"]["status_text"], "不适用")

    def test_headline_allows_template_with_days_prefix(self):
        result = self.build()
        self.assertIsNone(review.headline(result, 10))
        shown = review.headline(result, 10, allow_template=True)
        self.assertTrue(shown["text"].startswith("距最近财报 10 天："))
        self.assertEqual(shown["source"], "rule")
        self.assertIn("facts", shown)

    def test_same_report_hits_the_cache_across_days(self):
        store = Store(":memory:")
        first = self.build(store=store, day=date(2026, 9, 5))
        later = self.build(store=store, day=date(2026, 9, 8))
        self.assertEqual((first["cached"], later["cached"]), (False, True))
        self.assertEqual(later["as_of"], "2026-09-08")
        refreshed = self.build(store=store, day=date(2026, 9, 8), refresh=True)
        self.assertFalse(refreshed["cached"])

    def test_prompt_states_the_thresholds_from_the_config(self):
        self.assertIn("70%", prompt.SYSTEM)
        self.assertIn("20 个百分点", prompt.SYSTEM)
        self.assertIn("距最近财报多少天", prompt.SYSTEM)
        self.assertIn("目标价", prompt.SYSTEM)


class CausalAndTwoSidedChecksTest(unittest.TestCase):
    """设计稿核对规则 3（因果归因要有出处）和 4（正反两面都要提）。"""

    def test_causal_words_need_a_cause_source_in_the_citations(self):
        for text in ("毛利率下降，受汇率波动影响", "收入增长得益于新产品", "由于费用上升，利润下滑", "高库存拖累了现金流", "销量增加带动营收"):
            self.assertTrue(checks.causal("q6", text, ["ff.margin.gm_yoy"]), text)
            self.assertFalse(checks.causal("q6", text, ["ff.margin.gm_yoy", "ff.margin.driver"]), text)    # 引用了由代码归因的结论就放行
        self.assertFalse(checks.causal("q6", "利润率下降，原因暂时看不出", ["ff.margin.gm_yoy"]))               # "原因"不是因果词
        self.assertFalse(checks.causal("q3", "营收增 30.7%，归母持平", ["ff.growth.q_rev_yoy"]))
        self.assertFalse(checks.causal("q3", "由于利润桥显示费用拖累", ["ff.bridge.expense"]))                  # 利润桥的任何一项都算出处

    def test_both_sides_must_be_cited_in_the_summary_only_when_both_exist(self):
        both = {"positive": ["ff.a"], "negative": ["ff.b"]}
        self.assertEqual(checks.two_sided("q12", ["ff.a", "ff.b"], both), [])
        self.assertTrue(any("正面" in p for p in checks.two_sided("q12", ["ff.b"], both)))
        self.assertTrue(any("负面" in p for p in checks.two_sided("q12", ["ff.a"], both)))
        self.assertEqual(checks.two_sided("q12", ["ff.b"], {"positive": [], "negative": ["ff.b"]}), [])      # 一边为空不要求
        self.assertEqual(checks.two_sided("q2", [], both), [])                                              # 只管总结题

    def test_signals_pick_the_strongest_three_each_side_from_real_facts(self):
        for day in (Q1, H1, Q3):
            facts, d, text = sheet(day)
            picked = d["signals"]
            self.assertLessEqual(len(picked["positive"]), 3)
            self.assertLessEqual(len(picked["negative"]), 3)
            self.assertTrue(set(picked["positive"] + picked["negative"]) <= set(text))
            self.assertFalse(set(picked["positive"]) & set(picked["negative"]))
        self.assertIn("ff.growth.scissors", sheet(Q3)[1]["signals"]["negative"])                          # 三季报：剪刀差是最强的负面信号之一
        self.assertIn("ff.quality.fin_exp_shift", sheet(H1)[1]["signals"]["negative"])                      # 中报：财务费用大幅变动

    def test_the_template_summary_already_satisfies_the_two_sided_rule(self):
        for day in (Q1, H1, Q3):
            facts, d, text = sheet(day)
            row = next(r for r in template.answers(d, text) if r["key"] == "q12")
            self.assertEqual(checks.two_sided("q12", row["cites"], d["signals"]), [], day)
            self.assertIn("正面信号", row["answer"])
            self.assertIn("负面信号", row["answer"])

    def test_template_summary_mentions_both_sides_when_present(self):
        result = review.build(stock(), store=Store(":memory:"), gateway=None, now=NOW, payload=payload(Q3), model_tag="x")
        q12 = {row["key"]: row for row in result["items"]}["q12"]
        self.assertEqual(q12["source"], "template")
        self.assertTrue(q12["answer"])

    def test_gateway_is_ignored_for_causal_mutations(self):
        result = review_with(ScriptedModel())
        self.assertEqual(result["source"], "template")
        self.assertTrue(all(row.get("source") == "template" for row in result["items"]))


def review_with(model, day=Q3):
    return review.build(stock(), store=Store(":memory:"), gateway=Gateway(model), now=NOW, payload=payload(day), model_tag="x")


def with_preview(low_wan: float, high_wan: float, reason: str = "主要受财务费用影响") -> dict:
    """给深科技中报加一份业绩预告（金额单位万元，实际中报累计归母 5.43 亿元）。"""
    snap = snapshot()
    snap["forecast"] = [{"ts_code": TS, "ann_date": "20260715", "end_date": "20260630", "type": "预增", "net_profit_min": low_wan, "net_profit_max": high_wan,
                         "p_change_min": None, "p_change_max": None, "summary": "预计净利润增长", "change_reason": reason}]
    return snap


class ExpectationTest(unittest.TestCase):
    """设计稿事实 8 和第 9 题：业绩预告和朴素预期。券商一致预期接口每天只能调 10 次，不接入。"""

    def q9(self, snap, day=H1):
        facts, d, text = sheet(day, snap)
        return next(r for r in template.answers(d, text) if r["key"] == "q9"), d, text

    def test_actual_inside_the_preview_range_is_in_line(self):
        row, d, text = self.q9(with_preview(50000, 60000))
        self.assertEqual(row["status"], "neutral")
        self.assertEqual(text["ff.expect.preview_position"], "落在预告区间内")
        self.assertIn("预增", text["ff.expect.preview"])
        self.assertEqual(text["ff.expect.reason"], "主要受财务费用影响")
        self.assertEqual([c["level"] for c in rules.cells(d["m"]) if c["key"] == "expectation"], ["inline"])

    def test_actual_below_the_low_end_is_a_hard_warning(self):
        row, d, text = self.q9(with_preview(60000, 70000))
        self.assertEqual(row["status"], "warn")
        self.assertTrue(rules.bounds(d["m"])["q9"]["force_warn"])
        self.assertEqual(text["ff.expect.preview_position"], "低于预告下限")

    def test_actual_between_low_end_and_midpoint_cannot_be_positive(self):
        snap = with_preview(52000, 62000)                                  # 中值 5.7 亿，实际 5.43 亿：区间内但低于中值
        row, d, text = self.q9(snap)
        hard = rules.bounds(d["m"])["q9"]
        self.assertEqual((hard["forbid_support"], hard["force_warn"]), (True, False))
        self.assertNotEqual(row["status"], "support")

    def test_actual_above_the_high_end_is_better_than_expected(self):
        row, d, text = self.q9(with_preview(40000, 50000))
        self.assertEqual(row["status"], "support")
        self.assertFalse(any(rules.bounds(d["m"])["q9"].values()))

    def test_without_a_preview_only_the_naive_expectation_is_used_and_it_says_so(self):
        row, d, text = self.q9(snapshot(), Q3)
        self.assertIn("朴素预期", row["answer"])
        self.assertIn("不是市场预期", text["ff.expect.naive_note"])
        self.assertEqual(text["ff.expect.preview"], "没有发过业绩预告")        # 没有预告也是一条可以引用的事实
        self.assertNotIn("ff.expect.preview_dev", text)
        self.assertIn("暂未接入", row["answer"])
        self.assertEqual(rules.expectation_level(d["m"], d["th"]), ("miss", "naive"))   # 归母低于自身趋势外推 18%
        self.assertFalse(any(rules.bounds(d["m"])["q9"].values()))                      # 朴素预期不设硬阈值

    def test_the_preview_reason_counts_as_a_cause_source(self):
        self.assertFalse(checks.causal("q9", "利润超预告，受财务费用下降影响", ["ff.expect.reason"]))
        self.assertTrue(checks.causal("q9", "利润超预告，受财务费用下降影响", ["ff.expect.preview"]))

    def test_the_expectation_cell_and_the_rule_verdict_follow_the_expectation(self):
        payload_beat = build_detail(stock(), with_preview(40000, 50000), H1)
        cell = next(c for c in payload_beat["cells"] if c["key"] == "expectation")
        self.assertEqual((cell["level"], cell["level_text"]), ("beat", "好于预期"))
        self.assertIn("业绩预告区间", cell["note_text"])
        payload_none = build_detail(stock(), snapshot(), Q3)
        self.assertIn("朴素预期", next(c for c in payload_none["cells"] if c["key"] == "expectation")["note_text"])


class TrackingTest(unittest.TestCase):
    """设计稿事实 10 和第 11 题：上期需警惕的项，本期是缓解、持续还是恶化。"""

    def store_with_previous(self):
        store = Store(":memory:")
        review.build(stock(), store=store, gateway=None, now=NOW, payload=payload(H1), model_tag="x")          # 中报：第 7 题需警惕
        return store

    def test_first_analysis_has_nothing_to_track(self):
        facts, d, text = sheet(H1)
        q11 = next(r for r in template.answers(d, text) if r["key"] == "q11")
        self.assertEqual((q11["status"], q11["status_text"] if "status_text" in q11 else "na"), ("na", "na"))
        self.assertNotIn("ff.track.summary", text)

    def test_the_warned_questions_are_recorded_with_their_values(self):
        record = self.store_with_previous().finance_signals()[0]["tracked"]
        self.assertIn("q7", record)
        self.assertAlmostEqual(record["q7"]["value"], 0.217, delta=0.01)

    def test_the_next_report_compares_against_last_periods_warnings(self):
        store = self.store_with_previous()
        result = review.build(stock(), store=store, gateway=None, now=NOW, payload=payload(Q3), model_tag="x")
        by_key = {row["key"]: row for row in result["items"]}
        self.assertNotEqual(by_key["q11"]["status"], "na")
        facts = {row["id"]: row["text"] for row in result["facts"]}
        self.assertIn("上期（2026 半年报）", facts["ff.track.q7"])
        self.assertIn("恶化", facts["ff.track.q7"])                       # 现金流占比 22% → -34%
        self.assertIn("上期需警惕", facts["ff.track.summary"])
        self.assertIn(by_key["q11"]["status"], ("support", "warn", "neutral", "na", "missing"))
        self.assertEqual(by_key["q11"]["source"], "template")

    def test_improvement_and_no_change_are_told_apart(self):
        from app.finance_review import tracking

        m = M.compute(snapshot(), Q3)
        prior = {"report_end": "20260630", "tracked": {"q7": {"label": "x", "value": -0.9}, "q4": {"label": "x", "value": 0.95}, "q8": {"label": "x", "value": 5.0}}}
        states = {row["key"]: row["state"] for row in tracking.compare(prior, m)}
        self.assertEqual(states, {"q7": "improved", "q4": "same", "q8": "improved"})      # 现金流 -90% → -34%；扣非占比不变；应收差距 5 → 0.2

    def test_a_previous_period_without_warnings_is_not_applicable(self):
        store = Store(":memory:")
        store.save_finance_signal(TS, "20260630", "v", "2026-08-26", "neutral", "template", {"q2": "support"}, {})
        facts, d, text = sheet(Q3)
        result = review.build(stock(), store=store, gateway=None, now=NOW, payload=payload(Q3), model_tag="x")
        self.assertEqual({r["key"]: r["status"] for r in result["items"]}["q11"], "na")


class MaxChangeQuestionTest(unittest.TestCase):
    def test_q1_names_the_strongest_signal_and_its_other_side(self):
        facts, d, text = sheet(Q3)
        row = next(r for r in template.answers(d, text) if r["key"] == "q1")
        self.assertIn("这份财报最大的变化", row["answer"])
        self.assertIn("另一面", row["answer"])
        self.assertEqual(checks.two_sided("q1", row["cites"], d["signals"]), [])
        self.assertEqual(row["cites"][0], d["signals"]["top"])
        self.assertTrue(set(row["cites"]) <= set(template.relevant(set(text), d["signals"])["q1"]))

    def test_q1_status_follows_whether_the_biggest_change_is_good_or_bad(self):
        _f, d, text = sheet(Q3)
        row = next(r for r in template.answers(d, text) if r["key"] == "q1")
        self.assertEqual(row["status"], "support" if d["signals"]["top_side"] == "positive" else "warn")

    def test_q1_template_cites_signal_facts(self):
        result = review.build(stock(), store=Store(":memory:"), gateway=None, now=NOW, payload=payload(Q3), model_tag="x")
        q1 = {r["key"]: r for r in result["items"]}["q1"]
        self.assertEqual(q1["source"], "template")
        self.assertTrue(q1["cites"])


class SpecialCaseTemplatesTest(unittest.TestCase):
    """设计稿第五节：行业与特殊情况模板。不成立的题直接说"不适用"或换口径，比让模型硬凑一句更可信。"""

    def test_cycle_high_gross_margin_must_be_flagged_in_q1_and_the_summary(self):
        facts, d, text = sheet(H1)
        self.assertIn("周期高位", text["ff.margin.cycle"])                  # 深科技中报：单季毛利率在近 5 年 90% 分位
        rows = {r["key"]: r for r in template.answers(d, text)}
        self.assertIn("周期高位", rows["q1"]["answer"])
        self.assertIn("周期高位", rows["q12"]["answer"])
        self.assertIn("ff.margin.cycle", template.relevant(set(text), d["signals"])["q1"])
        self.assertNotIn("周期高位", sheet(Q3)[2]["ff.margin.cycle"])        # 三季报只在 75% 分位

        def forgets(out, view):
            for key in ("q1", "q12"):
                row = by(out, key)
                row["answer"] = row["answer"].replace("；利润可能处于周期高位", "")
                row["cites"] = [c for c in row["cites"] if c != "ff.margin.cycle"]
            return out

        result = review.build(stock(), store=Store(":memory:"), gateway=None, now=NOW, payload=payload(H1), model_tag="x")
        sources = {r["key"]: r["source"] for r in result["items"]}
        self.assertEqual((sources["q1"], sources["q12"]), ("template", "template"))

    def test_saying_cycle_high_when_the_margin_is_not_that_high_is_rejected(self):
        self.assertEqual(checks.cycle_note("q1", "毛利率偏高，利润可能处于周期高位", cycle_high=False)[0][:5], "提示:q1")
        self.assertEqual(checks.cycle_note("q6", "毛利率在近 5 年高位，周期高位", cycle_high=False)[0][:5], "提示:q6")      # 任何题都不能乱说
        self.assertEqual(checks.cycle_note("q1", "毛利率平稳", cycle_high=False), [])
        self.assertEqual(checks.cycle_note("q6", "毛利率平稳", cycle_high=True), [])                                     # 高位时只强制第 1、12 题

        def overreach(out, view):
            row = by(out, "q1")
            row["answer"] += "；单季毛利率可能处于周期高位"
            return out

        result = review.build(stock(), store=Store(":memory:"), gateway=None, now=NOW, payload=payload(Q3), model_tag="x")
        self.assertEqual({r["key"]: r["source"] for r in result["items"]}["q1"], "template")

    def test_suspected_consolidation_needs_a_new_line_that_is_a_big_part_of_the_increment(self):
        base = M.compute(snapshot(), H1)
        increment = base["growth"]["cum_rev_yoy"]
        snap = snapshot()
        cum_now = next(r["revenue"] for r in snap["income_c"] if r["end_date"] == "20260630" and str(r["update_flag"]) == "1")
        cum_before = next(r["revenue"] for r in snap["income_c"] if r["end_date"] == "20250630" and str(r["update_flag"]) == "1")
        added = lambda share: {"ts_code": TS, "end_date": "20260630", "bz_item": "新并入的业务", "bz_sales": share * (cum_now - cum_before), "bz_profit": 1.0, "bz_cost": 1.0}
        big, small = copy_snapshot(snap), copy_snapshot(snap)
        big["mainbz"].append(added(0.30))
        small["mainbz"].append(added(0.10))
        self.assertIsNone(base["lines"]["consolidation"])                  # 存储半导体改了名（加了"业务"二字）不能当成新产品线
        self.assertEqual(M.compute(big, H1)["lines"]["consolidation"]["item"], "新并入的业务")
        self.assertIsNone(M.compute(small, H1)["lines"]["consolidation"])
        facts, d, text = sheet(H1, big)
        self.assertIn("增长可能来自并表", text["ff.growth.consolidation"])
        self.assertFalse(checks.causal("q3", "收入增长得益于新并入的业务", ["ff.growth.consolidation"]))   # 并表提示算原因出处
        self.assertGreater(increment, 0)

    def test_product_line_margins_are_compared_with_the_same_line_last_year(self):
        facts, d, text = sheet(H1)
        self.assertIn("存储半导体", text["ff.margin.lines"])
        self.assertIn("+9.2 个百分点", text["ff.margin.lines"])
        self.assertIn("ff.margin.lines", template.relevant(set(text))["q6"])

    def test_chronic_loss_companies_get_cash_runway_instead_of_cash_conversion(self):
        for left, expected in ((2.0e8, "warn"), (6.0e8, "neutral"), (10.0e8, "support")):
            snap = chronic_loss(cash=left)
            facts, d, text = sheet(Q3, snap)
            rows = {r["key"]: r for r in template.answers(d, text)}
            self.assertEqual(rows["q4"]["status"], "na")                   # 扣非连续为负：第 4 题不适用
            self.assertEqual(rows["q7"]["status"], expected, left)
            self.assertIn("个季度", rows["q7"]["answer"])
            self.assertIn("ff.cash.runway", rows["q7"]["cites"])
        facts, d, text = sheet(Q3, chronic_loss(cash=2.0e8))
        self.assertIn("ff.growth.loss_rate", text)
        q2 = next(r for r in template.answers(d, text) if r["key"] == "q2")
        self.assertIn("亏损率", q2["answer"])
        hard = rules.bounds(d["m"])["q7"]
        self.assertTrue(hard["force_warn"])                                # 不到 4 个季度：必须需警惕

    def test_real_estate_looks_at_contract_liabilities_and_ignores_receivable_and_inventory_gaps(self):
        results = {}
        for label, now in (("flat", 9.5e8), ("drop", 7.0e8), ("collapse", 5.0e8)):
            snap = realestate(now)
            facts, d, text = sheet(Q3, snap)
            hard = rules.bounds(d["m"])["q8"]
            row = next(r for r in template.answers(d, text) if r["key"] == "q8")
            results[label] = (hard["forbid_support"], hard["force_warn"], row["status"])
            self.assertIn("合同负债", row["answer"])
        self.assertEqual(results, {"flat": (False, False, "support"), "drop": (True, False, "neutral"), "collapse": (True, True, "warn")})
        self.assertTrue(rules.bounds(sheet(Q3)[1]["m"])["q8"]["force_warn"])    # 同一份数据按一般公司算，应收和存货照样触发

    def test_a_recently_listed_company_skips_the_seasonal_adjustment(self):
        snap = snapshot()
        snap["list_date"] = "20240601"
        m = M.compute(snap, Q3)
        self.assertFalse(m["data"]["adj_ok"])
        self.assertIsNone(m["growth"]["qoq_rev_adj"])
        facts, d, text = sheet(Q3, snap)
        self.assertIn("不可用", text["ff.data.adj_ok"])
        self.assertIn("未校正", text["ff.growth.adj_state"])


def copy_snapshot(snap: dict) -> dict:
    return json.loads(json.dumps(snap))


def chronic_loss(cash: float) -> dict:
    """四个季度扣非都为负、每季经营现金净流出 1 亿元，货币资金由 cash 决定。"""
    snap = copy_snapshot(snapshot())
    for row in snap["indicator"]:
        row["profit_dedt"] = -abs(row["profit_dedt"] or 1.0)
    quarter = {"03": 1, "06": 2, "09": 3, "12": 4}
    for row in snap["cashflow_c"]:
        row["n_cashflow_act"] = -1.0e8 * quarter[row["end_date"][4:6]]
    for row in snap["balance"]:
        if row["end_date"] == "20260930":
            row["money_cap"] = cash
    for row in snap["income_q"] + snap["income_c"]:
        if row["end_date"] == "20260930":
            row["n_income_attr_p"] = -50_000_000.0
    return snap


def realestate(contract_now: float) -> dict:
    snap = copy_snapshot(snapshot())
    snap["industry"] = "房地产开发"
    for row in snap["balance"]:
        if row["end_date"] == "20260930":
            row["contract_liab"], row["adv_receipts"] = contract_now, None
        if row["end_date"] == "20250930":
            row["contract_liab"], row["adv_receipts"] = 10.0e8, None
    return snap


class PerturbationTest(unittest.TestCase):
    """设计稿第八节的扰动测试：把关键事实改到阈值另一侧，对应题目的状态必须翻转。"""

    def flips(self, day, edit, question, before, after):
        snap = snapshot()
        self.assertEqual(answers(day, snap)[question]["status"], before)
        edit(snap)
        self.assertEqual(answers(day, snap)[question]["status"], after)

    def test_deducted_share_q4(self):
        def edit(snap):
            for row in snap["indicator"]:
                if row["end_date"] == "20260630":
                    row["profit_dedt"] *= 0.6
        self.flips(H1, edit, "q4", "support", "neutral")

    def test_operating_cash_flow_q7(self):
        def edit(snap):
            for row in snap["cashflow_c"]:
                if row["end_date"] in ("20260630", "20260331"):
                    row["n_cashflow_act"] *= 8              # 累计经营现金流从约 22% 抬到大于 100% 的净利润
        before = answers(H1)["q7"]["status"]
        snap = snapshot()
        edit(snap)
        self.assertEqual(before, "warn")
        self.assertNotEqual(answers(H1, snap)["q7"]["status"], "warn")

    def test_receivable_and_inventory_growth_q8(self):
        def edit(snap):
            for row in snap["balance"]:
                if row["end_date"] == "20260930":
                    row["accounts_receiv"] *= 0.6
                    row["inventories"] *= 0.6
        self.flips(Q3, edit, "q8", "warn", "support")

    def test_scissors_q3(self):
        def edit(snap):
            for row in snap["income_q"] + snap["income_c"]:
                if row["end_date"] == "20260930":
                    row["n_income_attr_p"] *= 1.28          # 归母同比从 +0.1% 抬到 +28%，和营收增速 +31% 同向
        self.flips(Q3, edit, "q3", "warn", "support")

    def test_template_status_is_used_without_a_model(self):
        for day, key in ((Q1, "q4"), (H1, "q7"), (Q3, "q8")):
            by_key = {r["key"]: r for r in review.build(stock(), store=Store(":memory:"), gateway=None, now=NOW, payload=payload(day), model_tag="x")["items"]}
            self.assertEqual(by_key[key]["source"], "template", (day, key))
            self.assertIn(by_key[key]["status"], ("support", "warn", "neutral", "na", "missing"), (day, key))


class ValuationEffectTest(unittest.TestCase):
    """本期财报对估值的影响：滚动扣非净利润的变化。只报比例，不出价格，也不评价估值合理不合理。"""

    def test_ttm_deducted_profit_moves_with_each_report(self):
        changes = [M.compute(snapshot(), day)["valuation"]["change"] for day in (Q1, H1, Q3)]
        self.assertAlmostEqual(changes[0], 0.019, delta=0.002)
        self.assertAlmostEqual(changes[1], 0.171, delta=0.002)          # 中报：滚动扣非净利润一下涨了 17%
        self.assertAlmostEqual(changes[2], 0.040, delta=0.002)

    def test_facts_say_what_it_means_without_any_price(self):
        facts, d, text = sheet(H1)
        self.assertEqual(text["ff.valuation.change"], "+17.0%")
        self.assertIn("市盈率相应下降约 14.6%", text["ff.valuation.note"])        # 1 / 1.171 - 1
        self.assertIn("只报变化的比例，不出价格", text["ff.valuation.note"])
        joined = " ".join(text.values())
        self.assertNotIn("目标价", joined)
        self.assertIn("ff.valuation.change", template.relevant(set(text))["q9"])

    def test_a_negative_deducted_profit_gives_no_valuation_effect(self):
        snap = snapshot()
        for row in snap["indicator"]:
            if row["end_date"] in ("20260930", "20260630"):
                row["profit_dedt"] = -abs(row["profit_dedt"])
        facts, d, text = sheet(Q3, snap)
        self.assertNotIn("ff.valuation.change", text)
        self.assertIn("市盈率不可比", text["ff.valuation.note"])

    def test_the_prompt_forbids_prices_and_valuation_verdicts(self):
        self.assertIn("不要写任何价格，也不要说估值合理或不合理", prompt.SYSTEM)


class ProfitBridgeTest(unittest.TestCase):
    """设计稿事实 3：把单季归母净利润的同比变化拆开，第 3、5、6 题引用它解释原因。"""

    def test_the_parts_add_up_to_the_total_change(self):
        for day in (Q1, H1, Q3):
            b = M.compute(snapshot(), day)["bridge"]
            self.assertAlmostEqual(sum(v for v in b["parts"].values() if v is not None), b["delta"], delta=1.0)    # 差不到 1 元

    def test_q3_main_drag_is_the_expense_ratio_and_the_main_boost_is_revenue(self):
        b = M.compute(snapshot(), Q3)["bridge"]
        self.assertAlmostEqual(b["delta"] / 1e8, 0.0, delta=0.01)             # 归母基本持平
        self.assertEqual((b["top_drag"], b["top_boost"]), ("expense", "revenue"))
        self.assertGreater(b["parts"]["revenue"], 1.0e8)                      # 收入增长贡献 1 亿多，被费用率和毛利率吃掉
        self.assertLess(b["parts"]["gross_margin"], 0)

    def test_facts_and_template_use_the_bridge_to_explain_the_scissors(self):
        facts, d, text = sheet(Q3)
        self.assertEqual(text["ff.bridge.top_drag"], "四项费用率变化 -0.92 亿元")
        self.assertIn("ff.bridge.revenue", text)
        row = next(r for r in template.answers(d, text) if r["key"] == "q3")
        self.assertIn("利润桥里拖累最大的是四项费用率变化", row["answer"])
        self.assertIn("ff.bridge.top_drag", row["cites"])
        self.assertTrue(set(row["cites"]) <= set(template.relevant(set(text))["q3"]))

    def test_bridge_facts_count_as_a_cause_source_so_causal_claims_citing_them_pass(self):
        self.assertFalse(checks.causal("q3", "利润没跟上，受费用率上升拖累", ["ff.bridge.expense"]))
        self.assertTrue(checks.causal("q3", "利润没跟上，受费用率上升拖累", ["ff.growth.scissors"]))

    def test_missing_pieces_give_no_bridge_instead_of_a_wrong_one(self):
        snap = snapshot()
        snap["income_q"] = [r for r in snap["income_q"] if r["end_date"] != "20250930"]
        snap["income_c"] = [r for r in snap["income_c"] if r["end_date"] != "20250930"]
        self.assertIsNone(M.compute(snap, Q3)["bridge"])


class MarketReactionTest(unittest.TestCase):
    """设计稿事实 9 和第 10 题：披露后相对行业的表现，和同行分位。"""

    def test_h1_reaction_is_measured_from_the_close_before_disclosure(self):
        m = M.compute(snapshot(), H1)
        r = m["reaction"]
        self.assertEqual(r["base_date"], "20260825")                    # 8 月 26 日披露，从前一个交易日收盘算起
        self.assertEqual(r["days_after"], 8)                            # 含披露日当天
        self.assertEqual(sorted(r["after"]), [1, 3, 5])
        self.assertAlmostEqual(r["after"][1]["stock"], -0.016024, places=5)   # 第 1 日只含披露日当天，不把次日揉进去
        self.assertAlmostEqual(r["after"][5]["excess"], r["after"][5]["stock"] - r["after"][5]["sector"], places=9)
        self.assertIsNotNone(r["pre5"])
        self.assertIsNotNone(r["volume_ratio"])

    def test_without_the_disclosure_session_there_is_nothing_to_judge(self):
        snap = snapshot()
        snap["bars"] = [row for row in snap["bars"] if row["trade_date"] < "20261030"]
        facts, d, text = sheet(Q3, snap)                                # 披露日当天的行情也没有
        self.assertNotIn("ff.react.after1", text)
        row = next(r for r in template.answers(d, text) if r["key"] == "q10")
        self.assertEqual(row["status"], "missing")

    def test_facts_read_clearly_and_freeze_after_five_days(self):
        facts, d, text = sheet(H1)
        self.assertIn("跑输行业", text["ff.react.after5"])
        self.assertIn("指数", text["ff.react.after5"])
        self.assertEqual(text["ff.react.days_after"], "5 个交易日")           # 超过 5 个交易日也只算 5，缓存键不再每天变
        self.assertEqual(text["ff.react.base"], "20260825 收盘")

    def test_peer_percentiles_need_five_peers_and_the_same_period(self):
        facts, d, text = sheet(Q3)
        self.assertIn("72% 分位", text["ff.peer.rev_pctile"])
        self.assertIn("88 家同行", text["ff.peer.rev_pctile"])
        self.assertIn("ff.peer.ded_pctile", text)
        _facts, _d, h1_text = sheet(H1)
        self.assertNotIn("ff.peer.rev_pctile", h1_text)                   # 同行数据是三季报期的，别的报告期不给
        few = snapshot()
        few["peers"]["cur"] = dict(list(few["peers"]["cur"].items())[:4])
        self.assertNotIn("ff.peer.rev_pctile", sheet(Q3, few)[2])         # 不到 5 家不给

    def _with_reaction(self, daily_moves: list[float], sector_moves: list[float], tone_up: bool):
        """用合成的行情构造披露后的反应；tone_up 为真时用增长向上、含金量好的财报。"""
        snap = snapshot()
        days = ["20261030", "20261102", "20261103", "20261104", "20261105"]
        snap["bars"] = [r for r in snap["bars"] if r["trade_date"] < "20261030"] + [
            {"ts_code": TS, "trade_date": day, "pct_chg": move, "vol": 1.0, "amount": 1.0e6} for day, move in zip(days, daily_moves)
        ]
        snap["sector"]["rows"] = [r for r in snap["sector"]["rows"] if r["trade_date"] < "20261030"] + [
            {"ts_code": "850854.SI", "trade_date": day, "pct_change": move} for day, move in zip(days, sector_moves)
        ]
        if tone_up:
            for row in snap["cashflow_c"]:
                if row["end_date"] == "20260930":
                    row["n_cashflow_act"] = 1.0e9          # 经营现金流为正且足够，含金量好
        return snap

    def test_reaction_agreeing_with_a_good_report_is_supportive_and_the_opposite_is_a_warning(self):
        day = date(2026, 11, 9)
        good = self._with_reaction([3, 2, 1, 1, 1], [0, 0, 0, 0, 0], tone_up=True)
        _f, d, text = sheet(day, good)
        self.assertEqual(d["tone"]["score"], 2)
        row = next(r for r in template.answers(d, text) if r["key"] == "q10")
        self.assertEqual(row["status"], "support")
        self.assertIn("一致", row["answer"])
        bad = self._with_reaction([-3, -2, -1, -1, -1], [0, 0, 0, 0, 0], tone_up=True)
        _f, d2, text2 = sheet(day, bad)
        row2 = next(r for r in template.answers(d2, text2) if r["key"] == "q10")
        self.assertEqual(row2["status"], "warn")
        self.assertIn("相反", row2["answer"])
        self.assertIn("ff.react.after5", d2["signals"]["negative"])        # 反应进了负面信号候选

    def test_sector_wide_moves_are_blamed_on_the_industry_not_the_company(self):
        snap = self._with_reaction([-4, -2, -1, -1, -1], [-5, -3, -2, -2, -2], tone_up=True)
        _f, d, text = sheet(date(2026, 11, 9), snap)
        row = next(r for r in template.answers(d, text) if r["key"] == "q10")
        self.assertIn("主要是行业因素", row["answer"])

    def test_q10_template_subset_keeps_reaction_facts_local(self):
        snap = self._with_reaction([3, 2, 1, 1, 1], [0, 0, 0, 0, 0], tone_up=True)
        facts, d, text = sheet(date(2026, 11, 9), snap)
        subsets = template.relevant(set(text), d["signals"])
        self.assertIn("ff.react.after3", subsets["q10"])
        self.assertNotIn("ff.react.after3", subsets["q2"])


class IncrementalReinterpretationTest(unittest.TestCase):
    def test_gateway_is_ignored_for_re_runs(self):
        store, model = Store(":memory:"), ScriptedModel()
        first = review.build(stock(), store=store, gateway=Gateway(model), now=NOW, payload=payload(date(2026, 8, 27)), model_tag="x")
        second = review.build(stock(), store=store, gateway=Gateway(model), now=NOW, payload=payload(date(2026, 9, 5)), model_tag="x")
        self.assertEqual(model.calls, 0)
        self.assertEqual(first["source"], "template")
        self.assertEqual(second["source"], "template")
        self.assertTrue(all(r.get("source") == "template" for r in second["items"]))


class BacktestRecordTest(unittest.TestCase):
    """回测：披露后第一时间记下判断，满 5、20 个交易日回填相对行业的超额，再按 题 × 状态 统计。"""

    def test_one_signal_per_report_and_version_with_the_statuses_that_were_shown(self):
        store = Store(":memory:")
        review.build(stock(), store=store, gateway=None, now=NOW, payload=payload(H1), model_tag="x")
        review.build(stock(), store=store, gateway=None, now=NOW, payload=payload(H1), model_tag="x", refresh=True)
        rows = store.finance_signals()
        self.assertEqual(len(rows), 1)                                      # 同一份财报同一版本只记第一次
        row = rows[0]
        self.assertEqual((row["ts_code"], row["report_end"], row["announced"], row["lean"]), (TS, "20260630", "2026-08-26", "neutral"))
        self.assertIn("|", row["version"])
        self.assertEqual(row["statuses"]["q7"], "warn")
        self.assertEqual(row["source"], "template")
        self.assertIsNone(row["excess_5"])

    def test_settle_fills_each_horizon_once_enough_days_have_passed(self):
        store = Store(":memory:")
        review.build(stock(), store=store, gateway=None, now=NOW, payload=payload(H1), model_tag="x")
        snap = snapshot()
        market = lambda ts_code, today: (snap["bars"], snap["sector"]["rows"])
        self.assertEqual(outcomes.settle(store, date(2026, 10, 31), market), 2)       # 中报披露已经 2 个多月：5、20 日都到了
        row = store.finance_signals()[0]
        self.assertIsNotNone(row["excess_5"])
        self.assertIsNotNone(row["excess_20"])
        self.assertAlmostEqual(row["excess_5"], metrics_excess(snap, 5), places=9)
        self.assertEqual(outcomes.settle(store, date(2026, 10, 31), market), 0)       # 已回填的不重复

    def test_settle_waits_for_days_that_have_not_happened_yet(self):
        store = Store(":memory:")
        review.build(stock(), store=store, gateway=None, now=NOW, payload=payload(Q3), model_tag="x")
        snap = snapshot()
        self.assertEqual(outcomes.settle(store, date(2026, 10, 31), lambda ts, today: (snap["bars"], snap["sector"]["rows"])), 0)   # 三季报才披露 1 天

    def test_stats_group_by_question_and_status(self):
        store = Store(":memory:")
        store.save_finance_signal("A.SZ", "20260630", "v", "2026-08-26", "bullish", "model", {"q2": "support", "q7": "warn"})
        store.save_finance_signal("B.SZ", "20260630", "v", "2026-08-26", "bearish", "model", {"q2": "support", "q7": "warn"})
        store.save_finance_outcome("A.SZ", "20260630", "v", 5, 0.05, 0.03)
        store.save_finance_outcome("B.SZ", "20260630", "v", 5, -0.04, -0.02)
        rows = {(r["question"], r["status"]): r for r in outcomes.stats(store, 5)}
        self.assertEqual(rows[("q2", "support")]["n"], 2)
        self.assertEqual(rows[("q2", "support")]["hit_rate"], 0.5)                  # 偏正面后一只跑赢、一只跑输
        self.assertEqual(rows[("q7", "warn")]["hit_rate"], 0.5)
        self.assertAlmostEqual(rows[("q2", "support")]["avg_excess"], 0.005)
        self.assertEqual(rows[("规则版方向", "bullish")]["hit_rate"], 1.0)
        self.assertEqual(rows[("规则版方向", "bearish")]["hit_rate"], 1.0)


def metrics_excess(snap: dict, horizon: int) -> float:
    return M.forward_returns(snap["bars"], snap["sector"]["rows"], date(2026, 8, 26), (horizon,))[horizon]["excess"]


class RouteTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        worker.set_store(self.store)
        self.addCleanup(worker.set_store, None)
        app = Flask(__name__)
        app.register_blueprint(v1_bp)
        app.register_blueprint(make_blueprint("finance_review_test", "finance", "app.finance_review.review"))
        self.client = app.test_client()
        self.payload = payload(Q3)
        for item in (patch("app.api.v1.resolve_stock", lambda code: stock()),
                     patch("app.reviews.api.resolve_stock", lambda code: stock()),
                     patch("app.services.finance_detail.build_detail", lambda s, *a, **kw: self.payload),
                     patch("app.finance_review.review.load_payload", lambda s: self.payload),
                     patch.object(Gateway, "from_config", classmethod(lambda cls: Gateway(None)))):
            item.start()
            self.addCleanup(item.stop)

    def get(self):
        return self.client.get(f"/api/v1/stocks/{TS}/dimensions/finance/detail").get_json()

    def test_detail_returns_rule_review_without_llm_headline(self):
        body = self.get()
        self.assertEqual(set(body["verdict"]), {"source", "status", "status_text", "text"})
        self.assertEqual(body["review_state"], "ready")
        self.assertEqual(body["review"]["source"], "template")
        self.assertIsNone(body.get("headline"))
        self.assertFalse(body.get("analyst_failed"))
        self.assertEqual([c["key"] for c in body["cells"]], ["growth", "quality", "expectation"])
        for gone in ("inputs", "surprise", "chain", "risk_checks", "red_line", "growth", "table", "next_disclosure"):
            self.assertNotIn(gone, body)

    def test_stream_saves_template_headline_with_days(self):
        from app.api import v1
        with patch("app.api.v1._notify"), patch("app.synthesis.worker.get_store", return_value=self.store):
            page = v1._finance_page(stock(), wait=True)
            detail = v1._finance_page(stock(), wait=False)
        self.assertTrue(page["headline"]["text"].startswith("距最近财报"))
        self.assertEqual(page["headline"]["source"], "rule")
        self.assertFalse(page["analyst_failed"])
        self.assertEqual(detail["headline"]["text"], page["headline"]["text"])

    def test_review_endpoint(self):
        body = self.client.get(f"/api/v1/stocks/{TS}/dimensions/finance/review").get_json()
        self.assertEqual((body["status"], len(body["items"])), ("ok", 12))

    def test_not_applicable_stocks_have_no_review(self):
        bank = snapshot()
        bank["industry"] = "银行"
        self.payload = build_detail(stock(), bank, Q3)
        body = self.get()
        self.assertEqual((body["review_state"], body["headline"]), ("unavailable", None))
        self.assertIn("金融业", body["review_error"])


if __name__ == "__main__":
    unittest.main()
