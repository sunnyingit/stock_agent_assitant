import json
import unittest
from unittest.mock import patch

from app.main import create_app
from app.services.analyst_reply import analysis_message, answer
from app.synthesis.gateway import Gateway
from app.tools.stock import Stock


class _Model:
    def __init__(self, data):
        self.data = data

    def complete(self, system, user):
        return self.data


class ReplyTest(unittest.TestCase):
    def test_first_pass_and_reply_share_the_headline(self):
        headline = {
            "source": "model", "status": "support", "status_text": "偏多",
            "text": "板块主升，本股领涨。",
            "facts": [{"id": "sp.p.stage", "label": "阶段", "text": "主升第 12 天", "value": 12}],
        }
        first = analysis_message("sector", "板块分析师", headline)
        self.assertEqual(first["kind"], "analysis")
        self.assertEqual(first["quote"], "")
        self.assertEqual(first["text"], headline["text"])
        gateway = Gateway(_Model("主升第 12 天还在，方向没有变。\n\n- 依据：阶段事实"))
        replied = answer("板块分析师", "还在主升吗？", headline, gateway)
        self.assertIn("12", replied["text"])
        self.assertIn("- 依据", replied["text"])

    def test_reply_keeps_any_numbers_the_model_writes(self):
        headline = {"text": "大资金在跟。", "facts": [{"id": "f1", "text": "净流入", "value": None}]}
        gateway = Gateway(_Model("已经涨了 27.9%，短线拥挤。"))
        replied = answer("资金流分析师", "涨幅还大吗？", headline, gateway)
        self.assertIn("27.9", replied["text"])

    def test_reply_without_previous_headline_still_answers(self):
        gateway = Gateway(_Model("还没有整理出上一轮结论，先按现有事实说：主升第 12 天。"))
        replied = answer(
            "板块分析师",
            "还在主升吗？",
            None,
            gateway,
            facts=[{"id": "sp.p.stage", "label": "阶段", "text": "主升第 12 天", "value": 12}],
        )
        self.assertIn("12", replied["text"])

    def test_http_reply_uses_the_same_fields_as_the_first_stream(self):
        client = create_app().test_client()
        stock = Stock("600001.SH", "600001", "测试股份", "", "")
        headline = {"source": "model", "status": "support", "status_text": "偏多", "text": "价涨量增。", "facts": [{"id": "f1", "text": "价涨量增", "value": None}]}
        with patch("app.api.v1.resolve_stock", return_value=stock), \
             patch("app.api.v1._analyst_page", return_value={"headline": headline}), \
             patch("app.services.analyst_reply.answer", return_value={"text": "价涨量增还在。"}):
            response = client.post("/api/v1/stocks/600001.SH/dimensions/volume_flow/reply", json={"question": "还有人跟吗？"})
            body = response.get_data(as_text=True)
        message = json.loads([line[6:] for line in body.splitlines() if line.startswith("data: ")][1])
        self.assertEqual(message["kind"], "reply")
        self.assertEqual(message["quote"], "还有人跟吗？")
        self.assertEqual(message["dimension"], "volume_flow")
        self.assertEqual(message["headline"]["text"], "价涨量增。")
        self.assertEqual(message["text"], "价涨量增还在。")
        self.assertEqual(set(message), {"role", "dimension", "name", "kind", "quote", "text", "headline"})


if __name__ == "__main__":
    unittest.main()
