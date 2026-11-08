import json
import unittest
from unittest.mock import patch

from app.main import create_app
from app.synthesis.chief_board import card, detail
from app.synthesis.trend import build
from app.tools.stock import Stock


def _sample() -> dict:
    return {
        "decision": {
            "source": "agent",
            "final": {
                "direction": "bullish", "level": 2, "position": "mid",
                "direction_text": "板块主升且本股领涨，大资金在跟。",
                "direction_cites": ["sector.stage", "flow.pattern"],
                "spread": 4,
                "adopted": [
                    {"ref": "bull.1", "side": "bull", "weight": 3},
                    {"ref": "bull.2", "side": "bull", "weight": 3},
                    {"ref": "bear.1", "side": "bear", "weight": 2},
                ],
                "rejected": [
                    {"ref": "bull.3", "side": "bull", "reason": "priced_in"},
                    {"ref": "bear.2", "side": "bear", "reason": "weak"},
                ],
                "conflict_note": {"text": "20 天已涨，一旦转向跌得会快。", "cites": []},
                "postmortem": {"text": "大资金连续 2 天流出", "cites": []},
                "notices": [{"dimension": "sector", "tag": "利好", "cites": ["sp.p.stage"]}],
            },
        },
        "display_agent": {
            "generated_at": "2026-10-29 16:00",
            "direction": {"lean": "bullish", "level": 2, "text": "板块主升且本股领涨，大资金在跟。"},
            "position": {"label": "适中", "text": "位置适中"},
            "trend": {"window": 60, "ma": {"ma7": 21.48, "ma20": 20.85, "ma60": 18.05}, "lines": {}, "bars": []},
        },
        "levels_agent": {
            "down": {"price": 20.85, "dist": -0.07, "base_name": "20 日均线", "why": "跌破均线这轮方向就不成立", "condition": "连续 2 日收盘跌破", "consequence": "→ 偏多变为不明", "verified": True},
            "up": {"price": 24.60, "dist": 0.098, "base_name": "前高", "why": "站上前高压力才去掉", "condition": "收盘站上", "consequence": "→ 更强，但位置变偏高", "verified": True},
        },
        "table": {
            "facts": [{"id": "market.from_year_high", "value": -0.35, "text": "-35.0%"}],
            "headlines": [
                {"dimension": "sector", "label": "板块", "headline": {"text": "主升第 12 天，本股领涨。", "facts": [{"id": "sp.p.stage", "text": "主升", "value": 0.058}]}},
                {"dimension": "volume_flow", "label": "资金流", "headline": None},
            ],
        },
    }


class ChiefBoardTest(unittest.TestCase):
    def test_stream_card_keeps_the_verdict_and_leaves_out_the_chart(self):
        shown = card(_sample())
        self.assertEqual(shown["verdict"], "偏多 · 比较明显")
        self.assertEqual(shown["adopted"], "看多①② · 看空①")
        self.assertIn("已反应", shown["rejected"])
        self.assertEqual(shown["score"]["text"], "6:2，分差 4 → 比较明显")
        self.assertIn("20.85", shown["if_wrong"]["text"])
        self.assertNotIn("trend", shown)

    def test_detail_includes_notices_prices_and_the_trend(self):
        shown = detail(_sample())
        self.assertEqual(shown["position"]["text"], "离一年高点还有 35.0%")
        self.assertEqual(shown["notices"][0]["tag"], "利好")
        self.assertEqual(shown["notices"][0]["value"], 0.058)
        self.assertEqual([row["label"] for row in shown["notices"]], ["板块", "资金流", "新闻", "财报", "基本面"])
        self.assertEqual(shown["levels"]["down"]["price"], 20.85)
        self.assertEqual(shown["levels"]["up"]["why"], "站上前高压力才去掉")
        self.assertEqual(shown["trend"]["ma"]["ma20"], 20.85)
        self.assertEqual(shown["basis"][0]["label"], "板块")

    def test_moving_averages_use_closes_before_the_window(self):
        closes = [{"date": f"2026-08-{day:02d}", "close": float(day)} for day in range(1, 21)]
        chart = build({
            "bars": closes[-5:],
            "series": {"close": {row["date"]: row["close"] for row in closes}},
            "history": [],
            "date": closes[-1]["date"],
        }, {}, {"lean": "bullish", "level": 1, "text": ""}, {"total": 5})
        self.assertEqual(chart["ma"]["ma7"], round(sum(range(14, 21)) / 7, 2))
        self.assertIsNotNone(chart["ma"]["points"][-1]["ma20"])

    def test_detail_opens_the_saved_run_without_recomputing(self):
        client = create_app().test_client()
        record = {
            "policy_version": 1, "trade_date": "2026-11-06", "session": "close", "created_at": 0,
            "data": _sample(),
        }
        stock = Stock("600001.SH", "600001", "测试股份", "", "")
        with patch("app.synthesis.api.resolve_stock", return_value=stock), \
                patch("app.synthesis.api._saved_detail_record", return_value=record), \
                patch("app.synthesis.api._stale", return_value=False), \
                patch("app.synthesis.api._agent_record", side_effect=AssertionError("不应重算")):
            payload = client.get("/api/v1/stocks/600001.SH/chief/detail").get_json()
        self.assertEqual(payload["trend"]["ma"]["ma7"], 21.48)
        self.assertEqual(payload["direction"]["verdict"], "偏多 · 比较明显")

    def test_http_stream_is_the_card_and_detail_adds_the_chart(self):
        client = create_app().test_client()
        record = {"data": _sample()}
        stock = Stock("600001.SH", "600001", "测试股份", "", "")
        with patch("app.synthesis.api.resolve_stock", return_value=stock), patch("app.synthesis.api._saved_detail_record", return_value=None), patch("app.synthesis.api._agent_record", return_value=record):
            stream = client.get("/api/v1/stocks/600001.SH/chief/stream")
            body = stream.get_data(as_text=True)
            detail_response = client.get("/api/v1/stocks/600001.SH/chief/detail")
        events = []
        for block in body.split("\n\n"):
            if not block.strip():
                continue
            event, data = "message", ""
            for line in block.splitlines():
                if line.startswith("event: "):
                    event = line[7:]
                elif line.startswith("data: "):
                    data = line[6:]
            events.append((event, json.loads(data)))
        self.assertEqual([item[0] for item in events], ["start", "message", "done"])
        self.assertEqual(events[1][1]["verdict"], "偏多 · 比较明显")
        self.assertNotIn("trend", events[1][1])
        payload = detail_response.get_json()
        self.assertEqual(payload["trend"]["ma"]["ma7"], 21.48)
        self.assertEqual(payload["levels"]["up"]["price"], 24.60)

    def test_bull_message_names_one_analyst_and_keeps_the_chief_chip(self):
        from app.synthesis.chief_board import side_message
        from app.synthesis.types import Fact, FactTable

        table = FactTable(facts=[Fact("sector.stage", "main", "主升")])
        packed = {"failed": False, "points": [{"ref": "bull.1", "claim": "板块主升，本股领涨。", "cites": ["sector.stage"], "weight": 3}]}
        final = {"adopted": [{"ref": "bull.1", "side": "bull", "weight": 2, "reason": ""}], "rejected": []}
        message = side_message("bull", packed, table, final)
        self.assertEqual(message["name"], "看多分析师")
        self.assertEqual(message["side"], "正方")
        self.assertEqual(message["address"], "主分析师")
        self.assertEqual(message["points"][0]["analyst"]["name"], "板块分析师")
        self.assertEqual(message["points"][0]["verdict"]["text"], "采纳 · 强3→中2")
        self.assertEqual(message["points"][0]["facts"][0]["text"], "主升")

    def test_bull_stream_returns_only_that_side(self):
        client = create_app().test_client()
        stock = Stock("600001.SH", "600001", "测试股份", "", "")
        message = {"role": "bull", "name": "看多分析师", "points": [{"id": "bull.1", "text": "板块主升。"}]}
        with patch("app.synthesis.api.resolve_stock", return_value=stock), patch("app.synthesis.api._side_events", return_value=message):
            response = client.get("/api/v1/stocks/600001.SH/bull/stream")
            body = response.get_data(as_text=True)
        self.assertIn("看多分析师", body)
        self.assertNotIn("看空分析师", body)
        self.assertIn("板块主升。", body)

    def test_a_finished_bull_is_not_called_again_with_the_bear(self):
        from synthesis.fixtures import ScriptedModel
        from app.synthesis.decider.debate import run_side, run_sides
        from app.synthesis.gateway import Gateway
        from app.synthesis.store import Store
        from app.synthesis.types import Fact, FactTable

        table = FactTable(facts=[Fact("sector.stage", "main", "主升")])
        store = Store(":memory:")
        bull = [{"claim": "板块处于主升阶段", "cites": ["sector.stage"], "weight": 2}]
        first = ScriptedModel(bull=bull)
        run_side(Gateway(first), table, "bull", store, "fake")
        self.assertEqual(first.calls.count("【角色：正方"), 1)
        second = ScriptedModel(bull=bull)
        run_sides(Gateway(second), table, store, "fake")
        self.assertEqual(second.calls.count("【角色：正方"), 0)
        self.assertEqual(second.calls.count("【角色：反方"), 1)


if __name__ == "__main__":
    unittest.main()
