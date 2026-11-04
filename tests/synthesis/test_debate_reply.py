import json
import unittest
from unittest.mock import patch

from app.main import create_app
from app.synthesis.debate_reply import answer_chief, answer_side
from app.synthesis.gateway import Gateway
from app.tools.stock import Stock


class _Model:
    def __init__(self, data):
        self.data = data

    def complete(self, system, user):
        return self.data


class DebateReplyTest(unittest.TestCase):
    def test_bear_reply_returns_model_text_as_is(self):
        previous = {
            "role": "bear",
            "points": [{
                "id": "bear.1",
                "text": "20 天已涨 27.9%，短期涨幅偏大。",
                "weight": 2,
                "facts": [{"id": "market.stock_ret_20d", "label": "20 日涨幅", "text": "+27.9%", "value": 0.279}],
                "verdict": None,
            }],
        }
        gateway = Gateway(_Model("成立，但它说的是**位置**，不是方向。\n\n20 天涨 27.9% 说明追进去成本高。"))
        message = answer_side("bear", "板块还在主升期，涨幅偏大还成立吗？", previous, gateway)
        self.assertEqual(message["kind"], "reply")
        self.assertEqual(message["quote"], "板块还在主升期，涨幅偏大还成立吗？")
        self.assertIn("**位置**", message["text"])
        self.assertIn("27.9", message["text"])

    def test_chief_reply_keeps_card_shell_and_free_text(self):
        previous = {
            "role": "chief", "name": "主分析师", "side": "主持", "kind": "analysis", "quote": "",
            "verdict": "偏多 · 比较明显", "text": "板块主升且本股领涨。",
            "adopted": "看多①② · 看空①", "rejected": "看空②",
            "score": {"bull": 6, "bear": 2, "spread": 4, "text": "6:2，分差 4 → 比较明显"},
            "however": None, "if_wrong": {"text": "大资金连续 2 天流出"},
        }
        sides = {
            "bear": {"points": [{"ref": "bear.1", "claim": "20 天已涨 27.9%", "weight": 3, "cites": []}]},
        }
        gateway = Gateway(_Model("这条我仍记着，但只压强度，不改方向。失效价看 64.35。"))
        message = answer_chief("涨幅偏大这条还压方向吗？", previous, sides, gateway)
        self.assertEqual(message["kind"], "reply")
        self.assertEqual(message["verdict"], "偏多 · 比较明显")
        self.assertIn("64.35", message["text"])

    def test_http_reply_routes_emit_sse(self):
        client = create_app().test_client()
        stock = Stock("600001.SH", "600001", "测试股份", "", "")
        payload = {"role": "bull", "name": "看多分析师", "kind": "reply", "quote": "还成立吗？", "text": "还成立。", "points": []}
        with patch("app.synthesis.api.resolve_stock", return_value=stock), \
             patch("app.synthesis.api._side_reply", return_value=payload):
            response = client.post("/api/v1/stocks/600001.SH/bull/reply", json={"question": "还成立吗？"})
            body = response.get_data(as_text=True)
        message = json.loads([line[6:] for line in body.splitlines() if line.startswith("data: ")][1])
        self.assertEqual(message["kind"], "reply")
        self.assertEqual(message["quote"], "还成立吗？")

    def test_bull_reply_without_previous_points_still_answers(self):
        gateway = Gateway(_Model("20 日涨幅 27.9%，短线位置偏高，但单凭这点还不能定方向。"))
        message = answer_side(
            "bull",
            "涨这么多还看多吗？",
            {"role": "bull", "points": []},
            gateway,
            facts=[{"id": "market.stock_ret_20d", "label": "20 日涨幅", "text": "+27.9%", "value": 0.279}],
        )
        self.assertEqual(message["kind"], "reply")
        self.assertIn("27.9", message["text"])

    def test_chief_reply_without_previous_card_still_answers(self):
        gateway = Gateway(_Model("目前材料还不够做方向裁决，只能先盯住已知事实。"))
        message = answer_chief("现在怎么看？", {}, {}, gateway, facts=[])
        self.assertEqual(message["kind"], "reply")
        self.assertEqual(message["quote"], "现在怎么看？")
        self.assertIn("材料", message["text"])


if __name__ == "__main__":
    unittest.main()
