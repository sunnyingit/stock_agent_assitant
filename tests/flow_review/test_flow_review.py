import json
import unittest
from unittest.mock import patch

from flask import Flask

from synthesis.fixtures import NOW, make_aligned, make_stock, volume_payload
from app.flow_review import anomalies, data, facts as fact_sheet, review, template, validate
from app.flow_review.api import bp as review_bp
from app.synthesis import worker
from app.synthesis.gateway import Gateway
from app.synthesis.store import Store
from app.synthesis.types import FactTable


def inputs(aligned=None):
    aligned = aligned or make_aligned()
    return volume_payload(aligned), aligned


def sheet(aligned=None, extra=None):
    payload, aligned = inputs(aligned)
    extra = extra if extra is not None else {"missing": []}
    items, d = fact_sheet.build(payload, aligned, extra, make_stock(), NOW)
    return items, d, {item.id: item.text for item in items}


def flip_flow(aligned, sign):
    for row in aligned["flow"]:
        for size in ("super_large", "large"):
            row[size] = sign * abs(row[size])
    return aligned


class FactsTest(unittest.TestCase):
    def test_numbers_are_computed_by_code_in_yi_yuan_and_percent(self):
        _items, d, text = sheet()
        self.assertEqual(text["fr.ret_20d"], "+22.0%")
        self.assertEqual(text["fr.big_net_20d"], "+0.60 亿元")
        self.assertEqual(text["fr.pattern"], "价涨量增")
        self.assertEqual(text["fr.streak"], "连续净流入 60 天以上")
        self.assertEqual(text["fr.data_lag"], "最新")
        self.assertEqual(text["fr.limit_days"], "0 天")
        window = make_aligned()["bars"][-20:]
        self.assertAlmostEqual(d["net_rate_20d"], 20 * 3e6 / sum(row["amount"] for row in window), places=4)
        self.assertEqual(d["flow_coverage"], 1.0)

    def test_extra_data_adds_facts_and_missing_data_adds_none(self):
        _items, _d, plain = sheet()
        for fact_id in ("fr.circ_mv", "fr.toplist_days", "fr.margin_buy_pct", "fr.sector_rank", "fr.block_trade"):
            self.assertNotIn(fact_id, plain)
        extra = {
            "missing": [],
            "circ": {"circ_mv_yi": 100.0},
            "toplist": {"items": [
                {"trade_date": "2026-10-20", "net_amount": 2e8, "seats": [{"exalter": "机构专用", "net_buy": 1.5e8}, {"exalter": "某营业部", "net_buy": 5e7}]},
                {"trade_date": "2026-10-22", "net_amount": -1e8, "seats": []},
            ]},
            "margin": {"items": [{"rzye": 1e9 + i * 1e7, "rzmre_pct": 8.0} for i in range(12)]},
            "sector_rank": {"rank": 3, "total": 90},
            "market": {"turnover_change": 0.12},
            "block": {"count": 2, "amount_yi": 1.5},
        }
        _items, d, text = sheet(extra=extra)
        self.assertEqual(text["fr.circ_mv"], "100 亿元")
        self.assertEqual(text["fr.net_to_circ"], "+0.6%")
        self.assertEqual((text["fr.toplist_days"], text["fr.toplist_net"], text["fr.toplist_inst_net"]), ("2 天", "+1.00 亿元", "+1.50 亿元"))
        self.assertEqual(text["fr.margin_buy_pct"], "8%")
        self.assertEqual(text["fr.sector_rank"], "第 3 位（共 90 个行业板块）")
        self.assertEqual(text["fr.market_turnover"], "+12.0%")
        self.assertEqual(text["fr.block_trade"], "2 笔，合计 1.50 亿元")

    def test_public_facts_carry_group_kind_unit_and_raw_value(self):
        extra = {"missing": [], "toplist": {"items": [{"trade_date": "2026-10-20", "net_amount": 2e8, "seats": []}]},
                 "margin": {"items": [{"rzye": 1e9, "rzmre_pct": 8.0} for _ in range(12)]}, "block": {"count": 2, "amount_yi": 1.5}}
        items, _d, _t = sheet(extra=extra)
        rows = fact_sheet.public(items)
        by_id = {row["id"]: row for row in rows}
        self.assertEqual(len(rows), len(items))
        self.assertEqual(by_id["fr.ret_20d"], {
            "id": "fr.ret_20d", "label": "近 20 日涨跌幅", "group": "价格与量能", "kind": "pct", "unit": "%",
            "value": by_id["fr.ret_20d"]["value"], "text": "+22.0%"})
        self.assertAlmostEqual(by_id["fr.ret_20d"]["value"], 0.2202, places=4)      # 原始数值是小数，不是 22.0
        self.assertEqual((by_id["fr.big_net_20d"]["kind"], by_id["fr.big_net_20d"]["unit"]), ("yi", "亿元"))
        self.assertEqual((by_id["fr.toplist_days"]["group"], by_id["fr.toplist_net"]["group"]), ("龙虎榜", "龙虎榜"))
        self.assertEqual(by_id["fr.margin_buy_pct"]["group"], "融资融券")
        self.assertEqual((by_id["fr.block_trade"]["group"], by_id["fr.block_trade"]["value"], by_id["fr.block_trade"]["unit"]), ("大宗交易", 2, "笔"))
        self.assertEqual(by_id["fr.margin_buy_pct"]["value"], 0.08)                  # 没有 0.0800000000001 这类浮点噪声
        seen = list(dict.fromkeys(row["group"] for row in rows))   # 按分组排好，前端按出现顺序分组
        self.assertEqual(seen, [name for name in fact_sheet.GROUP_ORDER if name in seen])
        self.assertEqual([row["group"] for row in rows], sorted((row["group"] for row in rows), key=fact_sheet.GROUP_ORDER.index))
        self.assertTrue(set(by_id) <= set(fact_sheet.META))                          # 每条事实都登记了分组，没有掉进"其他"
        json.dumps(rows)                                                              # 可以直接序列化

    def test_limit_days_follow_the_board(self):
        aligned = make_aligned()
        aligned["bars"][-3]["chg"] = 10.0
        aligned["bars"][-1]["chg"] = -9.9
        _i, d, text = sheet(aligned)
        self.assertEqual(d["limit_days"], 2)
        self.assertEqual(text["fr.limit_last"], "就在最近一个交易日")
        board = fact_sheet._limit_of
        self.assertEqual((board("600519.SH"), board("300750.SZ"), board("688001.SH"), board("430047.BJ")), (10.0, 20.0, 20.0, 30.0))

    def test_distance_to_the_last_limit_day_counts_trading_days_not_weekdays(self):
        aligned = make_aligned()
        aligned["bars"][-8]["chg"] = 10.0
        aligned["bars"][-8]["date"] = "2026-10-09"   # 国庆假期：日期之间隔了很多个工作日，但日线只隔 7 根
        _i, _d, text = sheet(aligned)
        self.assertEqual(text["fr.limit_last"], "7 个交易日前")

    def test_no_bars_means_no_facts(self):
        payload, aligned = inputs()
        self.assertEqual(fact_sheet.build(payload, {"bars": []}, {"missing": []}, make_stock(), NOW)[0], [])


class AnomalyTest(unittest.TestCase):
    def detect(self, aligned=None, extra=None):
        items, d, _text = sheet(aligned, extra)
        return anomalies.detect(d, items)

    def test_healthy_rise_has_no_anomaly_except_the_hint_the_fixture_triggers(self):
        titles = [row["title"] for row in self.detect()]
        self.assertEqual(titles, ["出现位置提示：指标顶背离"])

    def test_rising_price_with_outflow_is_high(self):
        found = self.detect(flip_flow(make_aligned(), -1))
        self.assertEqual(found[0]["title"], "上涨，但大资金在净流出")
        self.assertEqual(found[0]["severity"], "high")
        self.assertIn("fr.big_net_20d", found[0]["cites"])

    def test_falling_price_with_inflow_is_medium(self):
        falling = make_aligned([30.0 * 0.99 ** i for i in range(300)])
        found = [row for row in self.detect(falling) if row["title"].startswith("下跌")]
        self.assertEqual(found[0]["severity"], "medium")

    def test_single_day_spike_and_distortion_and_quality(self):
        aligned = make_aligned()
        aligned["flow"][-1]["super_large"] = 5e8
        aligned["bars"][-2]["chg"] = 10.0
        aligned["bars"][-5]["volume"] = 0
        aligned["flow"] = aligned["flow"][:-12] + [dict(row, super_large=None, large=None) for row in aligned["flow"][-12:-1]] + aligned["flow"][-1:]
        titles = [row["title"] for row in self.detect(aligned)]
        self.assertIn("最近一天大资金流入异常大", titles)
        self.assertIn("窗口内有疑似涨跌停", titles)
        self.assertIn("窗口内疑似停牌", titles)
        self.assertIn("资金流数据缺了几天", titles)

    def test_implausible_amounts_are_flagged_as_a_data_problem(self):
        aligned = make_aligned()
        for row in aligned["flow"]:
            row["super_large"] = 1e12
        self.assertIn("资金流金额超过成交额", [row["title"] for row in self.detect(aligned)])

    def test_stale_data(self):
        aligned = make_aligned()
        payload, _ = inputs(aligned)
        old = make_aligned()
        old["as_of"] = "2026-10-27"
        items, d = fact_sheet.build(payload, old, {"missing": []}, make_stock(), NOW)
        row = anomalies.detect(d, items)[0]
        self.assertEqual((row["title"], row["severity"]), ("数据不是最新的", "medium"))

    def test_extra_sources_raise_flags(self):
        extra = {"missing": [], "toplist": {"items": [
            {"trade_date": f"2026-10-{20 + i}", "net_amount": -1e8, "seats": [{"exalter": "机构专用", "net_buy": -1e8}]} for i in range(3)]},
            "margin": {"items": [{"rzye": 1e9, "rzmre_pct": 25.0} for _ in range(12)]}}
        titles = [row["title"] for row in self.detect(extra=extra)]
        for expected in ("融资买入占比偏高", "近期频繁上龙虎榜", "龙虎榜上机构席位净卖出"):
            self.assertIn(expected, titles)


class TemplateTest(unittest.TestCase):
    def answers(self, aligned=None, extra=None):
        items, d, text = sheet(aligned, extra)
        found = anomalies.detect(d, items)
        return {row["key"]: row for row in template.answers(d, text, found)}, found

    def test_all_ten_questions_are_answered_with_cites(self):
        rows, _found = self.answers()
        self.assertEqual(list(rows), [f"q{i}" for i in range(1, 11)])
        self.assertEqual(rows["q1"]["status"], "support")
        self.assertIn("价格和资金方向一致", rows["q1"]["answer"])
        self.assertEqual(rows["q2"]["status"], "support")
        self.assertEqual(rows["q3"]["status"], "support")
        self.assertEqual(rows["q7"]["status"], "support")
        self.assertEqual(rows["q9"]["status"], "warn")        # 夹具里有"指标顶背离"提示
        self.assertEqual(rows["q6"]["status"], "missing")     # 没有龙虎榜、融资、大宗数据
        for key, row in rows.items():
            if row["status"] != "missing":
                self.assertTrue(row["cites"], key)

    def test_divergence_is_called_out(self):
        rows, found = self.answers(flip_flow(make_aligned(), -1))
        self.assertEqual(rows["q1"]["status"], "warn")
        self.assertIn("出现背离", rows["q1"]["answer"])
        self.assertEqual(rows["q2"]["status"], "warn")
        self.assertIn("上涨，但大资金在净流出", rows["q9"]["answer"])
        self.assertEqual(rows["q10"]["status"], "warn")

    def test_q2_looks_inside_the_big_orders_because_the_four_sizes_sum_to_zero(self):
        rows, _ = self.answers()
        self.assertEqual(rows["q2"]["status"], "support")
        self.assertIn("超大单和大单同时净流入", rows["q2"]["answer"])
        self.assertIn("合计接近 0", rows["q2"]["answer"])
        self.assertEqual(rows["q2"]["cites"], ["fr.elg_net_20d", "fr.lg_net_20d", "fr.md_net_20d", "fr.sm_net_20d"])
        mixed = make_aligned()
        for row in mixed["flow"]:
            row["large"] = -abs(row["large"])
        rows, _ = self.answers(mixed)
        self.assertEqual(rows["q2"]["status"], "neutral")
        self.assertIn("方向不一致", rows["q2"]["answer"])
        self.assertNotIn("筹码", rows["q2"]["answer"])

    def test_missing_data_is_never_invented(self):
        aligned = make_aligned()
        aligned["flow"] = []
        rows, _found = self.answers(aligned)
        for key in ("q1", "q2", "q3", "q4"):
            self.assertEqual(rows[key]["status"], "missing", key)
            self.assertTrue(rows[key]["answer"].startswith("数据缺失"))

    def test_extra_sources_turn_q6_into_an_answer(self):
        rows, _ = self.answers(extra={"missing": [], "margin": {"items": [{"rzye": 1e9, "rzmre_pct": 8.0} for _ in range(12)]}})
        self.assertNotEqual(rows["q6"]["status"], "missing")
        self.assertIn("融资买入占成交额 8%", rows["q6"]["answer"])


class ValidateTest(unittest.TestCase):
    def setUp(self):
        items, d, text = sheet()
        self.table = FactTable(facts=items)
        self.base = template.answers(d, text, anomalies.detect(d, items))

    def raw(self, **overrides):
        rows = {
            f"q{i}": {"key": f"q{i}", "status": "support", "answer": "资金方向和价格一致", "cites": ["fr.ret_20d", "fr.big_net_20d"]}
            for i in range(1, 11)
        }
        rows.update(overrides)
        return {"items": list(rows.values())}

    def test_valid_items_come_from_the_model_but_missing_stays_missing(self):
        items, errors = validate.merge_items(self.raw(), self.table, self.base)
        by_key = {row["key"]: row for row in items}
        self.assertEqual(errors, [])
        self.assertEqual(by_key["q1"]["source"], "model")
        self.assertEqual(by_key["q6"]["status"], "missing")   # 程序判定缺失，模型不能改
        self.assertEqual(by_key["q6"]["source"], "template")

    def test_each_bad_item_falls_back_alone(self):
        bad = {
            "q1": {"key": "q1", "status": "support", "answer": "近 20 日涨了 45%", "cites": ["fr.ret_20d"]},
            "q2": {"key": "q2", "status": "support", "answer": "有主力在吸筹", "cites": ["fr.big_net_20d"]},
            "q3": {"key": "q3", "status": "bullish", "answer": "资金持续流入", "cites": ["fr.streak"]},
            "q4": {"key": "q4", "status": "support", "answer": "力度较强", "cites": ["fr.ghost"]},
            "q5": {"key": "q5", "status": "support", "answer": "目标 25 元", "cites": ["fr.ret_20d"]},
            "q7": {"key": "q7", "status": "support", "answer": "没有失真", "cites": []},
        }
        items, errors = validate.merge_items(self.raw(**bad), self.table, self.base)
        by_key = {row["key"]: row for row in items}
        for key in bad:
            self.assertEqual(by_key[key]["source"], "template", key)
        self.assertEqual(by_key["q8"]["source"], "model")
        joined = " ".join(errors)
        for kind in ("数字:", "文案:", "格式:", "引用:"):
            self.assertIn(kind, joined)

    def test_index_names_are_not_numbers_but_real_numbers_still_are(self):
        ok = validate.check("本股涨幅 +22.0%，高于沪深300 的 +2.0%", ["fr.ret_20d", "fr.index_ret"], self.table, "q5")
        self.assertEqual(ok, [])
        spaced = validate.check("本股涨幅 +22.0%，高于沪深 300 的 +2.0%", ["fr.ret_20d", "fr.index_ret"], self.table, "q5")
        self.assertEqual(spaced, [])
        wrong = validate.check("本股涨幅 +22.0%，高于沪深300 的 +9.0%", ["fr.ret_20d", "fr.index_ret"], self.table, "q5")
        self.assertTrue(any("数字" in item for item in wrong))


class ScriptedModel:
    def __init__(self, fail=False, mutate=None):
        self.fail, self.mutate, self.calls, self.views, self.systems = fail, mutate, 0, [], []

    def complete(self, system, user):
        self.calls += 1
        if self.fail:
            raise RuntimeError("模型不可用")
        view = json.loads(user)
        self.views.append(view)
        self.systems.append(system)
        missing = set(view["must_be_missing"])
        answers = {"q1": "股价和大资金同向", "q2": "大额和小额资金方向相反", "q3": "大资金连续净流入", "q4": "力度较强",
                   "q5": "走势跑赢板块", "q6": "没有龙虎榜印证", "q7": "没有涨跌停", "q8": "位置适中",
                   "q9": "需要留意顶背离", "q10": "资金持续流入，需要留意顶背离"}
        out = {"items": [
            {"key": key, "status": "support", "answer": text, "cites": ["fr.ret_20d", "fr.big_net_20d"]}
            for key, text in answers.items() if key not in missing
        ]}
        return self.mutate(out, view) or out if self.mutate else out


class PromptTest(unittest.TestCase):
    def test_every_question_has_a_rubric_and_the_event_day_gap_is_stated(self):
        from app.flow_review import prompt

        for key in (f"q{i}" for i in range(1, 11)):
            self.assertIn(f"- {key}：", prompt.RUBRIC)
        for threshold in ("不少于 3 天", "5%", "80% 分位", "3 个百分点", "0.5 亿元", "不到 10%"):
            self.assertIn(threshold, prompt.RUBRIC)
        self.assertIn("不要对事件日下任何结论", prompt.RUBRIC)
        self.assertNotIn("一字板、事件日", prompt.SYSTEM)
        self.assertIn("事件日暂未接入", prompt.HEAD)
        self.assertIn("示例", prompt.SYSTEM)
        self.assertIn("不要用\"口径打架\"", prompt.RULES)
        self.assertIn("修正", prompt.FIX_SYSTEM)
        self.assertIn("只重写这几项", prompt.FIX_SYSTEM)

    def test_the_model_gets_an_index_of_what_to_look_at_and_a_reference_status(self):
        store = Store(":memory:")
        payload, aligned = inputs()
        model = ScriptedModel()
        review.build(make_stock(), store=store, gateway=Gateway(model), now=NOW, payload=payload, aligned=aligned, extra={"missing": []}, model_tag="x")
        view = model.views[0]
        self.assertEqual(view["by_question"]["q1"], ["fr.ret_20d", "fr.ret_5d", "fr.big_net_20d", "fr.net_rate_20d", "fr.pattern"])
        self.assertNotIn("fr.toplist_days", view["by_question"]["q6"])        # 没取到的事实不会出现在索引里
        self.assertEqual(view["reference"]["q1"], "support")
        self.assertEqual(view["reference"]["q2"], "support")
        self.assertNotIn("q6", view["reference"])                              # 程序判定缺失的没有参考状态
        self.assertIn("q6", view["must_be_missing"])
        self.assertTrue(all(set(ids) <= {fact["id"] for fact in view["facts"]} for ids in view["by_question"].values()))


class ReviewTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.payload, self.aligned = inputs()
        self.extra = {"missing": ["龙虎榜：没有取到数据", "大宗交易：取数超时"]}

    def build(self, model=None, **kwargs):
        gateway = Gateway(model) if model is not None else None
        return review.build(
            make_stock(), store=self.store, gateway=gateway, now=NOW, payload=self.payload, aligned=self.aligned,
            extra=self.extra, model_tag="fake", **kwargs,
        )

    def test_without_a_model_the_template_answers_everything(self):
        result = self.build()
        self.assertEqual(result["source"], "template")
        self.assertEqual([row["key"] for row in result["items"]], [f"q{i}" for i in range(1, 11)])
        for gone in ("overall", "anomalies", "watch"):
            self.assertNotIn(gone, result)
        self.assertEqual(result["missing"], self.extra["missing"])
        item = result["items"][0]
        self.assertEqual((item["title"], item["ask"]), ("资金和价格一致吗", "涨时有没有净流入，有没有背离"))
        self.assertEqual(item["cites"][0], "fr.ret_20d")
        self.assertNotIn("evidence", item)
        self.assertNotIn("summary", result)
        self.assertEqual(result["disclaimer"], review.DISCLAIMER)

    def test_the_response_carries_every_fact_not_only_the_cited_ones(self):
        result = self.build()
        ids = {row["id"] for row in result["facts"]}
        cited = {cite for row in result["items"] for cite in row["cites"]}
        self.assertTrue(cited <= ids)
        self.assertGreater(len(ids), len(cited))
        self.assertIn("fr.net_rate_20d", ids)
        self.assertEqual(result["facts"][0]["group"], "价格与量能")
        self.assertNotIn("fact_groups", result)
        self.assertEqual(self.build()["facts"], result["facts"])                    # 缓存命中时同样带着

    def test_model_answers_replace_templates_item_by_item(self):
        model = ScriptedModel()
        result = self.build(model)
        self.assertEqual(result["source"], "model")
        by_key = {row["key"]: row for row in result["items"]}
        self.assertEqual(by_key["q1"]["answer"], "股价和大资金同向")
        self.assertEqual(by_key["q1"]["source"], "model")
        self.assertEqual(by_key["q6"]["status"], "missing")

    def test_a_bad_item_makes_the_result_mixed_and_is_logged(self):
        def corrupt(out, view):
            out["items"][1]["answer"] = "近 20 日涨了 45%"
            return out

        result = self.build(ScriptedModel(mutate=corrupt), )
        self.assertEqual(result["source"], "mixed")
        self.assertEqual({row["key"]: row["source"] for row in result["items"]}["q2"], "template")
        self.assertTrue(any("数字" in error for error in review.build(
            make_stock(), store=Store(":memory:"), gateway=Gateway(ScriptedModel(mutate=corrupt)), now=NOW,
            payload=self.payload, aligned=self.aligned, extra=self.extra, model_tag="x")["debug"]["errors"]))

    def test_a_failed_item_is_sent_back_for_a_rewrite_and_recovered(self):
        def corrupt_first(out, view):
            if view.get("fix"):
                for row in out["items"]:
                    if row["key"] == "q2":
                        row["answer"] = "超大单和大单同时净流入，大额内部方向一致"
                        row["cites"] = ["fr.elg_net_20d", "fr.lg_net_20d"]
                return out
            out["items"][1]["answer"] = "近 20 日涨了 45%"
            return out

        model = ScriptedModel(mutate=corrupt_first)
        result = self.build(model)
        self.assertEqual(model.calls, 2)                                     # 一次解读 + 一次只重写出错的那项
        fix = model.views[1]["fix"]
        self.assertEqual([row["key"] for row in fix], ["q2"])
        self.assertEqual(fix[0]["previous_answer"], "近 20 日涨了 45%")
        self.assertTrue(any("数字" in error for error in fix[0]["errors"]))
        self.assertIn("修正", model.systems[1])
        by_key = {row["key"]: row for row in result["items"]}
        self.assertEqual(by_key["q2"]["source"], "model")
        self.assertEqual(by_key["q2"]["answer"], "超大单和大单同时净流入，大额内部方向一致")
        self.assertEqual(result["source"], "model")
        self.assertEqual(self.build(ScriptedModel(mutate=corrupt_first), refresh=True)["debug"]["repaired"], ["q2"])

    def test_a_rewrite_never_changes_the_status(self):
        def flip_in_rewrite(out, view):
            if view.get("fix"):
                for row in out["items"]:
                    if row["key"] == "q2":
                        row.update(status="warn", answer="超大单和大单同时净流入，大额内部方向一致", cites=["fr.elg_net_20d", "fr.lg_net_20d"])
                return out
            out["items"][1].update(status="neutral", answer="近 20 日涨了 45%")
            return out

        result = self.build(ScriptedModel(mutate=flip_in_rewrite))
        item = {row["key"]: row for row in result["items"]}["q2"]
        self.assertEqual((item["source"], item["status"]), ("model", "neutral"))   # 状态是重写前模型给的 neutral，不是重写时冒出来的 warn

    def test_opposite_polarity_to_the_reference_is_overridden_for_rubric_questions_only(self):
        def flip(out, view):
            for row in out["items"]:
                if row["key"] in ("q1", "q9"):
                    row["status"] = "warn" if row["key"] == "q1" else "support"
            return out

        result = self.build(ScriptedModel(mutate=flip))
        by_key = {row["key"]: row for row in result["items"]}
        self.assertEqual(by_key["q1"]["status"], "support")      # 参考是偏正面，模型给了需警惕 → 以参考为准
        self.assertEqual(by_key["q1"]["source"], "model")        # 文字仍用模型的
        self.assertEqual(by_key["q9"]["status"], "support")      # q9 不在受保护的问题里，模型可以不同意
        self.assertEqual(by_key["q8"]["status"], "warn")         # 夹具里命中了顶背离，参考是需警惕，假模型一律给 support → 也被改回
        self.assertEqual(result["debug"]["status_overridden"], ["q1", "q8"])

    def test_a_rewrite_that_still_fails_falls_back_to_the_template(self):
        def always_bad(out, view):
            for row in out["items"]:
                if row["key"] == "q2":
                    row["answer"] = "有主力在吸筹"
            return out

        result = self.build(ScriptedModel(mutate=always_bad))
        self.assertEqual({row["key"]: row["source"] for row in result["items"]}["q2"], "template")
        self.assertEqual(result["source"], "mixed")
        self.assertEqual(result["debug"]["repaired"], [])
        self.assertTrue(any(error.startswith("修正后仍未通过") for error in result["debug"]["errors"]))

    def test_the_rewrite_only_touches_failed_items(self):
        def greedy(out, view):
            if view.get("fix"):
                return {"items": [{"key": "q1", "status": "warn", "answer": "我顺手改了别的项", "cites": ["fr.ret_20d"]},
                                  {"key": "q2", "status": "support", "answer": "超大单和大单同时净流入", "cites": ["fr.elg_net_20d"]}]}
            out["items"][1]["answer"] = "有主力在吸筹"
            return out

        result = self.build(ScriptedModel(mutate=greedy))
        by_key = {row["key"]: row for row in result["items"]}
        self.assertEqual(by_key["q1"]["answer"], "股价和大资金同向")            # 没出错的项不会被重写覆盖
        self.assertEqual(by_key["q2"]["answer"], "超大单和大单同时净流入")

    def test_no_rewrite_call_when_everything_passes(self):
        model = ScriptedModel()
        self.build(model)
        self.assertEqual(model.calls, 1)

    def test_a_failed_rewrite_call_keeps_the_template(self):
        class Flaky(ScriptedModel):
            def complete(self, system, user):
                if "修正" in system:
                    raise RuntimeError("超时")
                return super().complete(system, user)

        def bad(out, view):
            out["items"][1]["answer"] = "有主力在吸筹"
            return out

        result = self.build(Flaky(mutate=bad))
        self.assertEqual({row["key"]: row["source"] for row in result["items"]}["q2"], "template")
        self.assertTrue(any("修正调用失败" in error for error in result["debug"]["errors"]))

    def test_model_failure_falls_back_and_is_not_cached(self):
        result = self.build(ScriptedModel(fail=True))
        self.assertEqual(result["source"], "template")
        self.assertIn("模型不可用", result["debug"]["call_error"])
        good = ScriptedModel()
        self.build(good)
        self.assertEqual(good.calls, 1)   # 失败那次没有缓存，下一次会重新调用

    def test_cache_and_refresh(self):
        model = ScriptedModel()
        first = self.build(model)
        second = self.build(model)
        self.assertEqual((first["cached"], second["cached"], model.calls), (False, True, 1))
        self.build(model, refresh=True)
        self.assertEqual(model.calls, 2)

    def test_unavailable_when_there_are_no_bars(self):
        with self.assertRaises(review.Unavailable):
            review.build(make_stock(), now=NOW, payload=self.payload, aligned={"bars": []}, extra={"missing": []})

    def test_load_extra_records_failures_and_timeouts_without_raising(self):
        def boom(stock):
            raise RuntimeError("接口挂了")

        loaders = {"toplist": boom, "margin": lambda stock: {"items": []}, "block": lambda stock: None}
        out = data.load_extra(make_stock(), loaders)
        self.assertEqual(out["margin"], {"items": []})
        self.assertTrue(any(item.startswith("龙虎榜：取数失败") for item in out["missing"]))
        self.assertTrue(any(item.startswith("大宗交易：没有取到数据") for item in out["missing"]))
        self.assertFalse(any(item.startswith("融资融券") for item in out["missing"]))


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        worker.set_store(self.store)
        self.addCleanup(worker.set_store, None)
        app = Flask(__name__)
        app.register_blueprint(review_bp)
        self.client = app.test_client()
        stocks = {"600001.SH": make_stock()}
        payload, shared = inputs()
        patches = [
            patch("app.reviews.api.resolve_stock", lambda code: stocks.get(code)),
            patch("app.reviews.api.Gateway.from_config", lambda: Gateway(None)),
            patch("app.flow_review.review.load_inputs", lambda stock, now, aligned=None: (payload, shared)),
            patch("app.flow_review.review.extra_data.load_extra", lambda stock: {"missing": ["龙虎榜：没有取到数据"]}),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def test_returns_the_ten_answers(self):
        body = self.client.get("/api/v1/stocks/600001.SH/dimensions/volume_flow/review").get_json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(len(body["items"]), 10)
        self.assertGreater(len(body["facts"]), 30)
        self.assertEqual(body["facts"][0]["group"], "价格与量能")
        self.assertNotIn("anomalies", body)
        self.assertNotIn("debug", body)
        self.assertEqual(body["missing"], ["龙虎榜：没有取到数据"])
        debug = self.client.get("/api/v1/stocks/600001.SH/dimensions/volume_flow/review?debug=1").get_json()
        self.assertIn("debug", debug)
        self.assertEqual(self.client.get("/api/v1/stocks/000000.SZ/dimensions/volume_flow/review").status_code, 404)

    def test_unavailable_is_reported_not_raised(self):
        def boom(stock, now, aligned=None):
            raise review.Unavailable("港股暂无资金流数据")

        with patch("app.flow_review.review.load_inputs", boom):
            body = self.client.get("/api/v1/stocks/600001.SH/dimensions/volume_flow/review?refresh=1").get_json()
        self.assertEqual((body["status"], body["error"]), ("unavailable", "港股暂无资金流数据"))


if __name__ == "__main__":
    unittest.main()
