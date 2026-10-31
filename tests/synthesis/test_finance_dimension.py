import unittest

from .fixtures import finance_payload, news_payload, volume_payload
from app.synthesis import display, policy, rules
from app.synthesis.producer import adapters


def snaps(**payloads):
    return [adapters.adapt(key, payload) for key, payload in payloads.items()]


def finance_snap(**kwargs):
    return adapters.adapt("finance", finance_payload(**kwargs))


class FinanceTimeWeightTest(unittest.TestCase):
    """设计稿第六节：财报一季一出，离披露越远，参考价值越低。"""

    def contribution(self, weight):
        out = rules.synthesize([finance_snap(lean="bullish", level=2, time_weight=weight)], {})
        return next(row for row in out["per_dim"] if row["key"] == "finance")

    def test_the_weight_scales_the_financial_dimensions_contribution(self):
        full, half, none = self.contribution(1.0), self.contribution(0.5), self.contribution(0.0)
        self.assertGreater(full["contribution"], 0)
        self.assertAlmostEqual(half["contribution"], full["contribution"] / 2, places=3)
        self.assertEqual(none["contribution"], 0.0)
        self.assertEqual((full["time_weight"], half["time_weight"], none["time_weight"]), (1.0, 0.5, 0.0))

    def test_the_adapter_reads_the_weight_from_the_detail(self):
        self.assertEqual(finance_snap(time_weight=0.5).flags["time_weight"], 0.5)
        self.assertEqual(adapters.adapt("finance", {**finance_payload(), "inputs": {}}).flags["time_weight"], 1.0)


class DivergenceTest(unittest.TestCase):
    def synth(self, finance_lean, flow_payload=None, news=None, weight=1.0):
        items = {"finance": finance_payload(lean=finance_lean, level=2, time_weight=weight), "volume_flow": flow_payload or volume_payload()}
        if news is not None:
            items["news"] = news
        return rules.synthesize(snaps(**items), {})

    def test_opposite_directions_are_flagged_with_each_sides_key_facts(self):
        flow_lean = adapters.adapt("volume_flow", volume_payload()).lean
        opposite = "bearish" if flow_lean == "bullish" else "bullish"
        out = self.synth(opposite)
        self.assertEqual(len(out["divergence"]), 1)
        item = out["divergence"][0]
        self.assertEqual(item["pair"], ["finance", "volume_flow"])
        self.assertEqual(item["leans"], {"finance": opposite, "volume_flow": flow_lean})
        self.assertIn("fin.growth", item["facts"]["finance"])
        self.assertTrue(all(fact.startswith("flow.") for fact in item["facts"]["volume_flow"]))

    def test_same_direction_or_a_stale_report_is_not_a_divergence(self):
        flow_lean = adapters.adapt("volume_flow", volume_payload()).lean
        self.assertEqual(self.synth(flow_lean)["divergence"], [])
        opposite = "bearish" if flow_lean == "bullish" else "bullish"
        self.assertEqual(self.synth(opposite, weight=0.0)["divergence"], [])           # 财报太旧，只作背景

    def test_news_can_diverge_too_and_the_page_says_so_without_averaging(self):
        out = self.synth("bearish", news=news_payload(lean="bullish", level=2))
        self.assertIn(["finance", "news"], [item["pair"] for item in out["divergence"]])
        view = display._divergence_view(next(item for item in out["divergence"] if item["pair"] == ["finance", "news"]))
        self.assertIn("财报偏空", view["text"])
        self.assertIn("新闻偏多", view["text"])
        self.assertIn("没有取平均", view["text"])
        self.assertTrue(view["facts"]["finance"])

    def test_every_fact_the_policy_lists_is_a_registered_fact(self):
        from app.synthesis.registry import REGISTRY

        for facts in policy.KEY_FACTS.values():
            self.assertTrue(set(facts) <= set(REGISTRY), facts)


if __name__ == "__main__":
    unittest.main()
