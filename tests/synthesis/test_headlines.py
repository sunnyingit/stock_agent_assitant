import unittest

from app.reviews.attach import remember
from app.synthesis.headlines import dimension_headlines
from app.synthesis.store import Store
from app.synthesis.types import FactTable
from app.synthesis.decider import prompts


def _headline(text, fact_id="n1"):
    return {
        "source": "model", "status": "support", "status_text": "偏多", "text": text,
        "facts": [{"id": fact_id, "label": "标题", "text": "原文", "value": 1}],
    }


class HeadlineInputTest(unittest.TestCase):
    def test_chief_receives_five_headlines_in_panel_order(self):
        payloads = {
            "news": {"headline": _headline("新产线投产。", "news.1")},
            "sector": {"headline": _headline("板块主升，本股领涨。", "sp.p.stage")},
        }
        store = Store(":memory:")
        remember(store, "600001.SH", {
            "items": [{"key": "q10", "source": "model", "status": "support", "answer": "价涨量增，大资金在跟。"}],
            "cited_fact_ids": ["fr.big_net_20d"],
            "facts": [{"id": "fr.big_net_20d", "label": "大资金", "text": "净流入", "value": 3}],
        }, kind="flow")
        rows = dimension_headlines(payloads, "600001.SH", store)
        self.assertEqual([row["label"] for row in rows], ["板块", "资金流", "新闻", "财报", "基本面"])
        self.assertEqual(rows[0]["headline"]["text"], "板块主升，本股领涨。")
        self.assertEqual(rows[1]["headline"]["text"], "价涨量增，大资金在跟。")
        self.assertEqual(rows[1]["headline"]["facts"][0]["id"], "fr.big_net_20d")
        self.assertEqual(rows[2]["name"], "新闻分析师")
        self.assertIsNone(rows[3]["headline"])
        self.assertIsNone(rows[4]["headline"])

        table = FactTable(headlines=rows)
        self.assertNotIn("headlines", table.sides_view())
        self.assertEqual([item["dimension"] for item in table.chief_view()["headlines"]], [
            "sector", "volume_flow", "news", "finance", "fundamental",
        ])
        self.assertIn("板块、资金流、新闻、财报、基本面", prompts.CHIEF_SYSTEM)

    def test_chief_sees_how_each_candidate_was_calculated_but_not_its_price(self):
        table = FactTable(candidates=[{
            "id": "L1", "dir": "down", "name": "20 日均线", "dist": -0.07, "price": 20.85,
            "sources": ["20 日均线", "大资金成本区"], "consequence": "判断可能减弱", "verified": True,
        }])
        shown = table.chief_view()["candidates"][0]
        self.assertNotIn("price", shown)
        self.assertIn("最近 20 个交易日收盘价的平均值", shown["basis"])
        self.assertIn("大资金", shown["basis"])
        self.assertIn("为什么选它", prompts.CHIEF_SYSTEM)


if __name__ == "__main__":
    unittest.main()
