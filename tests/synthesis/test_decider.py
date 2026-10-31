import copy
import unittest

from .fixtures import NOW, ScriptedModel, finance_payload, loaders, make_aligned, make_stock
from app.synthesis import decider, policy, rules
from app.synthesis.decider import judge, stabilize, strength
from app.synthesis.decider.validate import bad_numbers, validate_chief, validate_side
from app.synthesis.gateway import Gateway
from app.synthesis.producer import adapters, health
from app.synthesis.producer.collect import collect
from app.synthesis.producer.facts import build_fact_table
from app.synthesis.producer.market import market_facts
from app.synthesis.store import Store

CANDIDATES = [
    {"id": "L1", "dir": "down", "name": "20 日均线", "dist": -0.09, "sources": ["20 日均线"], "consequence": "判断可能减弱"},
    {"id": "U1", "dir": "up", "name": "前高", "dist": 0.1, "sources": ["前高"], "consequence": "位置变成偏高"},
]


def make_table(candidates=CANDIDATES, **overrides):
    aligned = make_aligned()
    bundle = collect(make_stock(), NOW, loaders(aligned, **overrides), aligned)
    snaps = [adapters.adapt(key, bundle.payloads[key], bundle.errors.get(key, "")) for key in policy.DIMENSIONS]
    market = market_facts(aligned)
    health.inspect(snaps, market, NOW)
    rule = rules.synthesize(snaps, market, True)
    context = {
        "done": rule["done"], "total": rule["total"], "fresh": True, "majority": rule["majority"],
        "dim_lean": {snap.key: snap.lean for snap in snaps if snap.done},
    }
    return build_fact_table(snaps, market, rule, list(candidates), context)


def good_chief(table, sides=None):
    sides = sides or {"bull": {"points": [
        {"ref": "bull.1", "claim": "量价形态为价涨量增", "cites": ["flow.pattern"], "weight": 2},
        {"ref": "bull.2", "claim": "板块处于主升阶段", "cites": ["sector.stage"], "weight": 2},
    ]}, "bear": {"points": [
        {"ref": "bear.1", "claim": "未来 20 个交易日内有解禁", "cites": ["fund.holder_event"], "weight": 1},
    ]}}
    arguments = judge.arguments_of(sides)
    payload = {"arguments": [{"ref": a["ref"], "claim": a["claim"], "cites": a["cites"], "weight": a["weight"]} for a in arguments],
               "candidates": table.candidates, "events": table.events}
    return ScriptedModel()._chief(payload), arguments


class ValidateSideTest(unittest.TestCase):
    def setUp(self):
        self.table = make_table()

    def points(self, rows):
        return validate_side({"points": rows}, self.table, "bull")

    def test_good_point_passes(self):
        points, errors = self.points([{"claim": "量价形态为价涨量增", "cites": ["flow.pattern"], "weight": 2}])
        self.assertEqual(errors, [])
        self.assertEqual(points[0]["ref"], "bull.1")

    def test_each_bad_point_is_dropped_alone(self):
        rows = [
            {"claim": "近 20 日涨了 45%", "cites": ["market.stock_ret_20d"], "weight": 2},
            {"claim": "量价形态为价涨量增", "cites": ["flow.pattern"], "weight": 2},
            {"claim": "出现了新的催化", "cites": ["news.nothing"], "weight": 2},
            {"claim": "建议买入", "cites": ["flow.pattern"], "weight": 2},
            {"claim": "目标 25 元", "cites": ["flow.pattern"], "weight": 2},
            {"claim": "分量过高", "cites": ["flow.pattern"], "weight": 5},
            {"claim": "没有引用", "cites": [], "weight": 1},
        ]
        points, errors = self.points(rows)
        self.assertEqual([item["claim"] for item in points], ["量价形态为价涨量增"])
        kinds = " ".join(errors)
        for word in ("数字:", "引用:", "文案:", "格式:"):
            self.assertIn(word, kinds)

    def test_rounding_and_percent_forms_are_accepted(self):
        points, errors = self.points([{"claim": "近 20 日涨了约 22%", "cites": ["market.stock_ret_20d"], "weight": 2}])
        self.assertEqual(errors, [])
        self.assertEqual(len(points), 1)

    def test_at_most_three_points_and_empty_is_fine(self):
        row = {"claim": "量价形态为价涨量增", "cites": ["flow.pattern"], "weight": 1}
        points, _ = self.points([row] * 5)
        self.assertEqual(len(points), policy.SIDE_MAX_POINTS)
        self.assertEqual(self.points([]), ([], []))
        self.assertEqual(validate_side({"points": "x"}, self.table, "bull")[0], [])

    def test_event_ids_can_be_cited_and_their_numbers_checked(self):
        event = self.table.events[1]["id"]
        text = self.table.events[1]["text"]
        points, errors = self.points([{"claim": "有减持事件，" + text.split("，")[-1], "cites": [event], "weight": 2}])
        self.assertEqual(errors, [])
        self.assertEqual(points[0]["cites"], [event])

    def test_number_checker(self):
        allowed = {22.02, 22.0, 0.2202}
        self.assertEqual(bad_numbers("涨了 22% 用了 5 天，窗口 20 日", allowed), [])
        self.assertEqual(bad_numbers("涨了 45%", allowed), ["45"])
        self.assertEqual(bad_numbers("有 3 个维度支持", set()), [])


class ValidateChiefTest(unittest.TestCase):
    def setUp(self):
        self.table = make_table()
        self.output, self.arguments = good_chief(self.table)

    def check(self, mutate=None, table=None):
        output = copy.deepcopy(self.output)
        if mutate:
            mutate(output)
        return validate_chief(output, table or self.table, self.arguments)

    def test_happy_path(self):
        decision, errors = self.check()
        self.assertEqual(errors, [])
        self.assertEqual(decision["direction"], "bullish")
        self.assertEqual(decision["spread"], 3)
        self.assertEqual(len(decision["adopted"]), 3)
        self.assertEqual(decision["levels"]["down"]["buffer"], 0.0)

    def test_direction_must_match_the_adopted_spread(self):
        _, errors = self.check(lambda o: o.update(direction="偏空"))
        self.assertTrue(any("底线" in item and "不一致" in item for item in errors))

        def drop_bull(output):
            for row in output["verdicts"][:2]:
                row.update(adopt=False, reason="weak")

        _, errors = self.check(drop_bull)
        self.assertTrue(any("底线" in item for item in errors))

    def test_small_spread_cannot_carry_a_direction(self):
        def lower(output):   # 采信 bull.1(2) 和 bear.1(1)，分差只有 1
            output["verdicts"][1].update(adopt=False, reason="weak")

        _, errors = self.check(lower)
        self.assertTrue(any("不足以支持" in item for item in errors))
        _, errors = self.check(lambda o: (lower(o), o.update(direction="不明")))
        self.assertEqual(errors, [])

    def test_verdict_rules(self):
        _, errors = self.check(lambda o: o["verdicts"].pop())
        self.assertTrue(any("没有裁决" in item for item in errors))
        _, errors = self.check(lambda o: o["verdicts"][0].update(weight=3))
        self.assertTrue(any("不能高于" in item for item in errors))
        _, errors = self.check(lambda o: o["verdicts"][2].update(adopt=False, reason="随便"))
        self.assertTrue(any("原因代码" in item for item in errors))
        _, errors = self.check(lambda o: o["verdicts"].append({"ref": "bull.9", "adopt": True, "weight": 1}))
        self.assertTrue(any("不存在的论据" in item for item in errors))

    def test_lowered_weight_needs_a_reason(self):
        _, errors = self.check(lambda o: o["verdicts"][0].update(weight=1, reason=""))
        self.assertTrue(any("调低分量但没有给出原因" in item for item in errors))
        decision, errors = self.check(lambda o: o["verdicts"][0].update(weight=1, reason="priced_in"))
        self.assertEqual(errors, [])
        self.assertEqual(decision["spread"], 2)

    def test_citations_numbers_and_copy(self):
        _, errors = self.check(lambda o: o.update(direction_cites=["news.ghost"]))
        self.assertTrue(any("引用" in item for item in errors))
        _, errors = self.check(lambda o: o.update(direction_text="近 20 日涨了 45%", direction_cites=["market.stock_ret_20d"]))
        self.assertTrue(any("数字" in item for item in errors))
        _, errors = self.check(lambda o: o.update(position_text="目标价 25 元"))
        self.assertTrue(any("文案" in item for item in errors))
        _, errors = self.check(lambda o: o.update(headline="量价和板块都在推而且没有任何风险提示的意思"))
        self.assertTrue(any("标题超过" in item for item in errors))
        _, errors = self.check(lambda o: o.update(postmortem={"text": "", "cites": []}))
        self.assertTrue(any("事后复盘" in item for item in errors))
        _, errors = self.check(lambda o: o.update(watch_ids=["E9"]))
        self.assertTrue(any("watch_ids" in item for item in errors))

    def test_level_menu(self):
        def level(**changes):
            return lambda o: o["levels"]["down"].update(**changes)

        for change, text in (
            ({"base": "L9"}, "不在候选里"),
            ({"buffer": "7%"}, "缓冲"),
            ({"confirm": "touch"}, "确认条件"),
            ({"why": "跌到 20.5 元就走", "cites": ["flow.pattern"]}, "价格"),
            ({"cites": []}, "没有引用"),
        ):
            _, errors = self.check(level(**change))
            self.assertTrue(any(text in item for item in errors), (change, errors))
        _, errors = self.check(lambda o: o["levels"]["up"].update(meaning="priced_in"))
        self.assertTrue(any("已反应" in item for item in errors))
        decision, errors = self.check(lambda o: o["levels"]["up"].update(
            meaning="priced_in", cites=["news.priced_in"], why="利好基本被股价反应"))
        self.assertEqual(errors, [])
        self.assertEqual(decision["levels"]["up"]["meaning"], "priced_in")

    def test_a_side_without_candidates_is_ignored_not_penalised(self):
        table = make_table(candidates=[CANDIDATES[1]])   # 只有上方候选
        output, arguments = good_chief(table)
        output["levels"] = {"down": {"base": "", "buffer": "0%", "confirm": "close", "why": "", "cites": []},
                            "up": {"base": "U1", "confirm": "close", "meaning": "stronger", "why": "板块处于主升", "cites": ["sector.stage"]}}
        decision, errors = validate_chief(output, table, arguments)
        self.assertEqual(errors, [])
        self.assertIsNone(decision["levels"]["down"])
        self.assertEqual(decision["levels"]["up"]["base"], "U1")
        output["levels"]["up"]["base"] = "U9"   # 有候选可选却选了不存在的：仍然作废
        self.assertTrue(validate_chief(output, table, arguments)[1])

    def test_bearish_cannot_leave_a_buffer(self):
        def bearish(output):
            output.update(direction="偏空", headline="量价和板块都在拖")
            for row in output["verdicts"]:
                row.update(adopt=row["ref"].startswith("bear"), reason="" if row["ref"].startswith("bear") else "weak")
            output["verdicts"][2]["weight"] = 1
            output["verdicts"].append({"ref": "bear.2", "adopt": True, "weight": 2})
            output["levels"]["down"]["buffer"] = "2%"

        arguments = self.arguments + [{"ref": "bear.2", "claim": "x", "cites": ["flow.pattern"], "weight": 3}]
        output = copy.deepcopy(self.output)
        bearish(output)
        output["verdicts"][3]["weight"] = 3
        _, errors = validate_chief(output, self.table, arguments)
        self.assertTrue(any("缓冲只能选 0" in item for item in errors), errors)

    def test_red_line_blocks_bullish(self):
        table = make_table(finance=finance_payload(red=True))
        output, arguments = good_chief(table)
        _, errors = validate_chief(output, table, arguments)
        self.assertTrue(any("红线" in item for item in errors))

    def test_going_against_the_majority_needs_two_supporting_dimensions(self):
        bear_args = {"bull": {"points": []}, "bear": {"points": [
            {"ref": "bear.1", "claim": "解禁在即", "cites": ["fund.holder_event"], "weight": 3},
            {"ref": "bear.2", "claim": "量价指标顶背离", "cites": ["flow.stall"], "weight": 1},
        ]}}
        output, arguments = good_chief(self.table, bear_args)
        output.update(direction="偏空", headline="解禁临近压力偏大")
        self.assertEqual(output["direction"], "偏空")
        _, errors = validate_chief(output, self.table, arguments)
        self.assertTrue(any("多数维度相反" in item for item in errors), errors)

    def test_headline_must_agree_with_direction(self):
        def bearish_wording(output):
            output.update(direction="偏空")
            for row in output["verdicts"]:
                row.update(adopt=row["ref"].startswith("bear"), reason="" if row["ref"].startswith("bear") else "weak")

        sides = {"bull": {"points": [{"ref": "bull.1", "claim": "板块处于主升阶段", "cites": ["sector.stage"], "weight": 1}]},
                 "bear": {"points": [{"ref": "bear.1", "claim": "未来 20 个交易日内有解禁", "cites": ["fund.holder_event"], "weight": 3}]}}
        output, arguments = good_chief(self.table, sides)
        bearish_wording(output)
        output["levels"]["down"]["buffer"] = "0%"
        self.table.context["majority"] = "bearish"   # 绕开"和多数维度相反"那一条，单独看标题
        _, errors = validate_chief(output, self.table, arguments)
        self.assertTrue(any("标题不能写成看多" in item for item in errors), errors)


class StrengthAndStabilityTest(unittest.TestCase):
    def test_spread_to_strength(self):
        self.assertEqual([strength.level_from_spread(x) for x in (0, 1, 2, 3, 4, 5, 6, 9)], [0, 0, 1, 1, 2, 2, 3, 3])
        self.assertEqual([strength.level_from_spread(x) for x in (-1, -2, -4, -6)], [0, 1, 2, 3])
        self.assertEqual(strength.strength("bullish", 7, 5, 5), 3)
        self.assertEqual(strength.strength("bullish", 7, 2, 5), policy.LOW_COVERAGE_CAP)
        self.assertEqual(strength.strength("unclear", 7, 5, 5), 0)

    def test_flip_needs_confirmation_or_a_large_spread(self):
        previous = {"direction": "bullish", "level": 2, "raw_direction": "bullish"}
        new = {"direction": "bearish", "level": 1, "spread": -2}
        self.assertEqual(stabilize.anti_flip(new, previous), ("bullish", 1, True))
        self.assertEqual(stabilize.anti_flip(new, {**previous, "raw_direction": "bearish"}), ("bearish", 1, False))
        self.assertEqual(stabilize.anti_flip({**new, "spread": -4, "level": 2}, previous), ("bearish", 2, False))
        self.assertEqual(stabilize.anti_flip({"direction": "bullish", "level": 3, "spread": 7}, previous), ("bullish", 3, False))
        self.assertEqual(stabilize.anti_flip(new, None), ("bearish", 1, False))
        unclear = {"direction": "unclear", "level": 0, "raw_direction": "unclear"}
        self.assertEqual(stabilize.anti_flip({"direction": "bullish", "level": 1, "spread": 2}, unclear), ("unclear", 0, True))

    def test_change_reason_names_what_moved(self):
        previous = {"direction": "bullish", "level": 2, "adopted": [
            {"claim": "资金持续流入", "weight": 2}, {"claim": "消息尚未反应", "weight": 2}]}
        now = [{"claim": "消息尚未反应", "weight": 2}, {"claim": "板块进入主升", "weight": 1}]
        text = stabilize.change_reason(previous, "unclear", 0, now)
        self.assertEqual(text, "从偏多变为不明：新增：板块进入主升；不再成立：资金持续流入")
        self.assertEqual(stabilize.change_reason(previous, "bullish", 2, now), "")
        self.assertTrue(stabilize.change_reason(previous, "bullish", 1, now).startswith("偏多的程度减弱"))
        self.assertEqual(stabilize.change_reason(None, "bullish", 2, now), "")


class DecideTest(unittest.TestCase):
    def setUp(self):
        self.table = make_table()

    def decide(self, model, session="close", previous=None, cache=None):
        gateway = Gateway(model)
        return decider.decide(self.table, gateway, session, previous, cache, "fake"), gateway

    def test_close_runs_sides_once_and_chief_three_times(self):
        model = ScriptedModel()
        result, _ = self.decide(model)
        self.assertEqual(result["source"], "agent")
        self.assertEqual(sorted(model.calls), ["【角色：主Agent", "【角色：主Agent", "【角色：主Agent", "【角色：反方", "【角色：正方"])
        final = result["final"]
        self.assertEqual((final["direction"], final["level"], final["position"]), ("bullish", 1, "mid"))
        self.assertEqual(final["spread"], 3)
        self.assertEqual(final["change_reason"], "")

    def test_intraday_runs_chief_once(self):
        model = ScriptedModel()
        result, _ = self.decide(model, session="intraday")
        self.assertEqual(result["source"], "agent")
        self.assertEqual(len(model.calls), 3)

    def test_sides_are_cached_by_fact_table_hash(self):
        store = Store(":memory:")
        first = ScriptedModel()
        self.decide(first, cache=store)
        second = ScriptedModel()
        result, _ = self.decide(second, cache=store)
        self.assertTrue(result["sides"]["cached"])
        self.assertEqual(second.calls.count("【角色：正方"), 0)
        self.assertEqual(second.calls.count("【角色：主Agent"), 3)

    def test_one_bad_run_of_three_still_passes(self):
        state = {"n": 0}

        def flaky(output, _payload):
            state["n"] += 1
            if state["n"] == 1:
                output["direction"] = "看涨"

        result, _ = self.decide(ScriptedModel(chief=flaky))
        self.assertEqual(result["source"], "agent")
        self.assertEqual(sum(1 for run in result["runs"] if run["decision"]), 2)

    def test_two_bad_runs_fall_back(self):
        state = {"n": 0}

        def mostly_bad(output, _payload):
            state["n"] += 1
            if state["n"] <= 2:
                output["headline"] = "买入机会"

        result, _ = self.decide(ScriptedModel(chief=mostly_bad))
        self.assertEqual((result["source"], result["reason"]), ("fallback", "validation"))
        self.assertIsNone(result["final"])
        self.assertTrue(any("文案" in err for run in result["runs"] for err in run["errors"]))

    def test_disagreeing_runs_fall_back_as_inconsistent(self):
        positions = iter(["偏低", "适中", "偏高"])

        def vary(output, _payload):
            output["position"] = next(positions)

        result, _ = self.decide(ScriptedModel(chief=vary))
        self.assertEqual((result["source"], result["reason"]), ("fallback", "inconsistent"))

    def test_two_of_three_agreeing_is_enough(self):
        positions = iter(["偏低", "适中", "适中"])

        def vary(output, _payload):
            output["position"] = next(positions)

        result, _ = self.decide(ScriptedModel(chief=vary))
        self.assertEqual((result["source"], result["final"]["position"]), ("agent", "mid"))

    def test_model_errors_fall_back(self):
        result, _ = self.decide(ScriptedModel(fail=True))
        self.assertEqual((result["source"], result["reason"]), ("fallback", "model_error"))
        none = decider.decide(self.table, Gateway(None, error="没有密钥"))
        self.assertEqual(none["reason"], "no_model")

    def test_empty_sides_give_an_unclear_conclusion(self):
        def unclear(output, payload):
            output.update(direction="不明", headline="证据不足，暂无明确方向")
            output["verdicts"] = []
            output["levels"] = {}

        result, _ = self.decide(ScriptedModel(bull=[], bear=[], chief=unclear))
        self.assertEqual((result["source"], result["final"]["direction"], result["final"]["level"]), ("agent", "unclear", 0))

    def test_flip_is_held_back_and_the_strength_drops_one_step(self):
        def to_bear(output, payload):
            output.update(direction="偏空", headline="解禁带来压力")
            for row in output["verdicts"]:
                keep = row["ref"] == "bear.1"
                row.update(adopt=keep, reason="" if keep else "weak")
            output["levels"]["down"]["buffer"] = "0%"

        bull = [{"claim": "量价形态为价涨量增", "cites": ["flow.pattern"], "weight": 1},
                {"claim": "板块处于主升阶段", "cites": ["sector.stage"], "weight": 1}]
        bear = [{"claim": "未来 20 个交易日内有解禁", "cites": ["fund.holder_event"], "weight": 3},
                {"claim": "量价指标顶背离", "cites": ["flow.stall"], "weight": 1}]
        self.table.context["majority"] = "bearish"   # 绕开"和多数维度相反"的另一条底线，单独看防翻
        self.table.context["dim_lean"] = {"fundamental": "bearish", "volume_flow": "bearish"}
        previous = {"direction": "bullish", "level": 2, "raw_direction": "bullish", "adopted": []}
        result, _ = self.decide(ScriptedModel(bull=bull, bear=bear, chief=to_bear), previous=previous)
        self.assertEqual(result["source"], "agent", result["runs"][0]["errors"])
        final = result["final"]
        self.assertEqual((final["raw_direction"], final["direction"], final["level"]), ("bearish", "bullish", 1))
        self.assertTrue(final["held_by_anti_flip"])


if __name__ == "__main__":
    unittest.main()
