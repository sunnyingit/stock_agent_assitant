import json
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agents.schema import apply_schema, filter_citations
from app.agents.spec import compile_spec

try:
    from app.agents.engine import run_agent
    from app.agents.news.graph import analyze_news, debate_news
except Exception:
    run_agent = None
    analyze_news = None
    debate_news = None

from app.tools.stock import Stock

ROOT = Path(__file__).resolve().parents[1]


class ScriptedModel:
    def __init__(self):
        self.calls = []
        self._lock = threading.Lock()

    def complete(self, system, user):
        with self._lock:
            self.calls.append((system, user))
        if "事件抽取员" in system:
            return {"facts": [
                {
                    "event": "贵州茅台收盘",
                    "numbers": ["1349.98"],
                    "source": "东方财富",
                    "time": "2026-10-26",
                    "article_id": "N1",
                },
                {"event": "对不上的事实", "article_id": "N9"},
            ]}
        if "看多分析员" in system:
            return {
                "sufficient": True,
                "points": [
                    {"fact_ids": ["F1"], "claim": "收盘价被报道", "reasoning": "新闻给出了收盘价", "assumption": "成交没有异常"},
                    {"fact_ids": ["F9"], "claim": "虚构利多", "reasoning": "没有这条事实", "assumption": ""},
                ],
            }
        if "看空分析员" in system:
            return {"sufficient": False, "note": "只有收盘价，看不出利空", "points": []}
        if "主分析员" in system:
            return {
                "verdict": {
                    "lean": "偏空",
                    "lean_level": 2,
                    "headline": "新闻面偏空：收盘价被报道",
                    "why": "新闻只写了收盘价，没有新的好消息",
                    "certainty": "低",
                    "certainty_note": "材料很少",
                },
                "today_move": {"change": "+9.99%", "explain": "没有新闻说明今天涨跌的原因"},
                "key_news": [
                    {
                        "lean": "偏空",
                        "title": "贵州茅台收盘 1349.98 元",
                        "plain": "只报道了收盘价",
                        "why_it_matters": "没有新信息",
                        "source": "东方财富",
                        "time": "10-26 17:27",
                        "url": "https://example.com/a",
                    },
                    {
                        "lean": "偏多",
                        "title": "编造的利好",
                        "plain": "这条链接不在新闻里",
                        "why_it_matters": "应被丢掉",
                        "source": "未知",
                        "time": "",
                        "url": "https://example.com/fake",
                    },
                ],
                "watch_next": [{
                    "what": "后续公告",
                    "when": "未知",
                    "if_better": "如果出现明确的好消息，判断会转为偏多",
                    "if_worse": "如果没有新消息，偏空判断维持",
                }],
                "more": {"news_read": 0, "news_used": 0, "note": ""},
                "fine_print": "",
            }
        if system == "extract":
            return {"facts": [{"event": "净流入"}]}
        if system == "bull":
            return {"sufficient": True, "note": "", "points": [{"fact_ids": ["F1"], "claim": "看多第一轮", "reasoning": "", "assumption": ""}]}
        if system == "bear":
            return {"sufficient": True, "note": "", "points": [{"fact_ids": ["F1"], "claim": "看空第一轮", "reasoning": "", "assumption": ""}]}
        if system == "bull-2":
            return {"sufficient": True, "note": "", "points": [{"fact_ids": ["F1"], "claim": "看多第二轮", "reasoning": "", "assumption": ""}]}
        if system == "bear-2":
            return {"sufficient": True, "note": "", "points": [{"fact_ids": ["F1"], "claim": "看空第二轮", "reasoning": "", "assumption": ""}]}
        if system == "chief":
            return {"summary": ["两轮之后的总结"]}
        raise AssertionError(system)


class SchemaTest(unittest.TestCase):
    def test_assigns_fact_ids_and_drops_unknown_citations(self):
        schema = {
            "type": "array",
            "max": 8,
            "require_field": "event",
            "id_prefix": "F",
            "item": {"type": "object", "fields": {"event": "string"}},
        }
        facts = apply_schema([{"event": "  发布年报  "}, {"event": "   "}], schema)
        self.assertEqual([fact["id"] for fact in facts], ["F1"])
        side = filter_citations(
            {"sufficient": True, "note": "", "points": [
                {"fact_ids": ["F1", "F9"], "claim": "年报已发布", "reasoning": "", "assumption": ""},
            ]},
            {"F1"},
            {"point_field": "fact_ids"},
            3,
            {},
        )
        self.assertEqual(side["points"][0]["fact_ids"], ["F1"])
        self.assertNotIn("F9", side["points"][0]["fact_ids"])

    def test_moneyflow_config_does_not_need_news_names(self):
        compiled = compile_spec({
            "source": "moneyflow",
            "rounds": 2,
            "records": "flow",
            "read": {
                "list": "rows",
                "require": "trade_date",
                "fields": {"trade_date": "trade_date", "net": "net"},
            },
            "facts": {
                "must": "net",
                "max": 6,
                "fields": {"net": "string", "trade_date": "string"},
                "prompt": ["整理资金流"],
            },
            "debate": {
                "cite": "fact_ids",
                "point": {"fact_ids": "string[]", "view": "string"},
                "roles": [{"id": "bull", "prompt": ["看多"]}, {"id": "bear", "prompt": ["看空"]}],
            },
            "summary": {"prompt": ["总结"], "fields": {"view": "string[]"}},
        })
        self.assertEqual(compiled["input"]["payload_key"], "flow")
        self.assertEqual(compiled["input"]["require"], "trade_date")
        self.assertEqual(compiled["result"]["flow"], "material")
        self.assertNotIn("articles", compiled["result"])
        self.assertEqual(compiled["extract"]["schema"]["fields"]["facts"]["require_field"], "net")
        self.assertEqual(compiled["context"]["debate"], ["id", "net"])
        self.assertEqual(compiled["context"]["previous"], ["fact_ids", "view"])
        self.assertEqual(compiled["cache_ttl"], "analysis")
        self.assertEqual(compiled["debate_rounds"], 2)


class NewsGraphTest(unittest.TestCase):
    def setUp(self):
        if analyze_news is None:
            self.skipTest("langgraph is not installed")

    def test_news_config_is_one_round(self):
        spec = json.loads((ROOT / "app/agents/configs/news.json").read_text(encoding="utf-8"))
        self.assertEqual(spec["rounds"], 1)
        self.assertEqual(spec["source"], "news")
        self.assertEqual([role["id"] for role in spec["debate"]["roles"]], ["bull", "bear"])
        self.assertIn("verdict", spec["summary"]["fields"])
        self.assertNotIn("schema", spec)
        compiled = compile_spec(spec)
        self.assertEqual(compiled["chief"]["schema"]["fields"]["key_news"]["max"], 6)
        self.assertIn("必须在偏多和偏空里二选一", compiled["chief"]["prompt"])
        self.assertIn("输出 JSON", compiled["chief"]["prompt"])

    def test_empty_news_skips_the_model(self):
        model = ScriptedModel()
        stock = Stock("600519.SH", "600519", "贵州茅台", "1.600519", "SH600519")
        result = analyze_news(stock, model=model, fetch_news=lambda: {"items": []})
        self.assertEqual(result["summary"]["verdict"]["lean"], "")
        self.assertEqual(result["bull"]["note"], "没有可分析的新闻")
        self.assertEqual(model.calls, [])
        self.assertFalse(result["bull"]["sufficient"])

    def test_chief_only_sees_cited_facts(self):
        model = ScriptedModel()
        stock = Stock("600519.SH", "600519", "贵州茅台", "1.600519", "SH600519")
        result = analyze_news(stock, model=model, fetch_news=lambda: {"items": [
            {
                "title": "贵州茅台收盘 1349.98 元",
                "source_name": "东方财富",
                "time": "2026-10-26 17:27",
                "url": "https://example.com/a",
                "summary": "",
            },
        ]})
        self.assertEqual(result["facts"][0]["id"], "F1")
        self.assertEqual(result["facts"][0]["article_id"], "N1")
        self.assertEqual(result["facts"][1]["article_id"], "")
        self.assertEqual(result["articles"][0]["url"], "https://example.com/a")
        self.assertEqual(result["bull"]["points"][0]["fact_ids"], ["F1"])
        self.assertFalse(result["bear"]["sufficient"])
        self.assertEqual(result["summary"]["verdict"]["headline"], "新闻面偏空：收盘价被报道")
        extract_user = next(user for system, user in model.calls if "事件抽取员" in system)
        bull_user = next(user for system, user in model.calls if "看多分析员" in system)
        chief_user = next(user for system, user in model.calls if "主分析员" in system)
        self.assertIn("N1", extract_user)
        self.assertNotIn("https://example.com/a", extract_user)
        self.assertNotIn("1349.98", bull_user)
        self.assertIn("F1", bull_user)
        self.assertIn("1349.98", chief_user)
        self.assertIn("https://example.com/a", chief_user)
        self.assertNotIn("F9", chief_user)
        self.assertNotIn("虚构利多", chief_user)

    def test_debate_response_keeps_news_and_conclusion(self):
        if debate_news is None:
            self.skipTest("langgraph is not installed")
        model = ScriptedModel()
        stock = Stock("600519.SH", "600519", "贵州茅台", "1.600519", "SH600519")
        result = debate_news(stock, model=model, fetch_news=lambda: {"items": [
            {"title": "贵州茅台收盘 1349.98 元", "source_name": "东方财富", "url": "https://example.com/a"},
        ]})
        self.assertEqual(result["display"]["verdict"]["lean"], "偏空")
        self.assertEqual(result["display"]["verdict"]["lean_level"], 1)
        self.assertEqual(result["display"]["verdict"]["certainty"], "低")
        self.assertTrue(result["display"]["verdict"]["headline"].startswith("新闻面偏空："))
        self.assertEqual(result["display"]["today_move"]["change"], "")
        self.assertEqual(result["display"]["key_news"][0]["url"], "https://example.com/a")
        self.assertEqual(len(result["display"]["key_news"]), 1)
        self.assertEqual(result["display"]["fine_print"], "以上为基于公开新闻的研究分析，不构成投资建议")
        self.assertEqual(result["display"]["more"]["news_read"], 1)
        self.assertEqual(result["internal"]["bull"]["points"][0]["fact_ids"], ["F1"])
        self.assertEqual(result["internal"]["verdict"]["signal"], "bearish")
        self.assertEqual(result["internal"]["audit"]["articles"][0]["title"], "贵州茅台收盘 1349.98 元")
        self.assertNotIn("news", result)
        chief_user = next(user for system, user in model.calls if "主分析员" in system)
        self.assertIn("today", chief_user)

    def test_code_score_overrides_the_model_direction(self):
        class OverrideModel(ScriptedModel):
            def complete(self, system, user):
                with self._lock:
                    self.calls.append((system, user))
                if "事件抽取员" in system:
                    return {"facts": [{
                        "event": "签订合同",
                        "article_id": "N1",
                        "category": "经营",
                        "direction": "利好",
                        "basis": "事实",
                        "amount": "30亿",
                        "base": "100亿",
                    }]}
                if "看多分析员" in system:
                    return {"sufficient": True, "points": [{"fact_ids": ["F1"], "claim": "合同金额不小", "reasoning": "", "assumption": ""}]}
                if "看空分析员" in system:
                    return {"sufficient": False, "note": "没有利空", "points": []}
                if "主分析员" in system:
                    return {
                        "verdict": {
                            "lean": "偏空",
                            "lean_level": 3,
                            "headline": "新闻面偏空：模型想写成利空",
                            "why": "模型写成了利空，不过代码应该改回偏多",
                            "certainty": "高",
                            "certainty_note": "模型自己写的",
                        },
                        "today_move": {"change": "", "explain": "没有新闻对上今天的涨跌"},
                        "key_news": [{
                            "lean": "偏空",
                            "title": "贵州茅台签订30亿合同",
                            "plain": "合同占营收比例高",
                            "why_it_matters": "金额大",
                            "source": "东方财富",
                            "time": "2026-10-26 10:00",
                            "url": "https://example.com/a",
                        }],
                        "watch_next": [{
                            "what": "合同确认",
                            "when": "尚未确定",
                            "if_better": "如果确认收入，偏多会更明显",
                            "if_worse": "如果合同取消，判断会转为偏空",
                        }],
                        "more": {"news_read": 0, "news_used": 0, "note": ""},
                        "fine_print": "",
                    }
                raise AssertionError(system)

        model = OverrideModel()
        stock = Stock("600519.SH", "600519", "贵州茅台", "1.600519", "SH600519")
        result = debate_news(stock, model=model, fetch_news=lambda: {"items": [{
            "title": "贵州茅台签订30亿合同",
            "summary": "上年营收100亿",
            "source_name": "东方财富",
            "time": "2026-10-26 10:00",
            "url": "https://example.com/a",
        }]})
        self.assertEqual(result["display"]["verdict"]["lean"], "偏多")
        self.assertEqual(result["display"]["verdict"]["lean_level"], 2)
        self.assertEqual(result["display"]["verdict"]["certainty"], "低")
        self.assertTrue(result["display"]["verdict"]["headline"].startswith("新闻面偏多："))
        self.assertEqual(result["internal"]["verdict"]["score"], 1.5)
        self.assertEqual(result["internal"]["events"][0]["magnitude"], "大")

    def test_second_round_sees_the_other_side(self):
        spec = {
            "debate_rounds": 2,
            "messages": {"no_material": "没有材料", "no_facts": "没有事实", "summary_field": "summary"},
            "input": {"payload_key": "articles"},
            "extract": {
                "name": "整理",
                "temperature": 0,
                "output_key": "facts",
                "prompt": "extract",
                "schema": {
                    "type": "object",
                    "fields": {
                        "facts": {
                            "type": "array",
                            "id_prefix": "F",
                            "require_field": "event",
                            "item": {"type": "object", "fields": {"event": "string"}},
                        }
                    },
                },
            },
            "side_schema": {
                "type": "object",
                "fields": {
                    "sufficient": "boolean",
                    "note": "string",
                    "points": {
                        "type": "array",
                        "item": {"type": "object", "fields": {"fact_ids": "string[]", "claim": "string", "reasoning": "string", "assumption": "string"}},
                    },
                },
            },
            "sides": [
                {"id": "bull", "name": "看多", "temperature": 0, "max_points": 1, "cite": {"point_field": "fact_ids", "id_field": "id"}, "prompt": "看多分析"},
                {"id": "bear", "name": "看空", "temperature": 0, "max_points": 1, "cite": {"point_field": "fact_ids", "id_field": "id"}, "prompt": "看空分析"},
            ],
            "chief": {
                "name": "总结",
                "temperature": 0,
                "prompt": "chief",
                "schema": {"type": "object", "fields": {"summary": "string[]"}},
            },
            "result": {"facts": "facts", "bull": "sides.bull", "bear": "sides.bear", "summary": "summary"},
        }

        class RoundModel(ScriptedModel):
            def complete(self, system, user):
                with self._lock:
                    self.calls.append((system, user))
                if system == "extract":
                    return {"facts": [{"event": "净流入", "article_id": "N9"}]}
                if "看多" in system:
                    claim = "看多第二轮" if "previous" in user else "看多第一轮"
                    return {"sufficient": True, "points": [{"fact_ids": ["F1"], "claim": claim}]}
                if "看空" in system:
                    claim = "看空第二轮" if "previous" in user else "看空第一轮"
                    return {"sufficient": True, "points": [{"fact_ids": ["F1"], "claim": claim}]}
                if system == "chief":
                    return {"summary": ["两轮之后的总结"]}
                raise AssertionError(system)

        model = RoundModel()
        spec["sides"][0]["prompt"] = "看多分析"
        stock = Stock("600519.SH", "600519", "贵州茅台", "1.600519", "SH600519")
        result = run_agent("news", stock, model=model, fetch=lambda: {"items": [{"title": "净流入"}]}, spec=spec)
        bull_calls = [user for system, user in model.calls if "看多" in system]
        bull_prompts = [system for system, user in model.calls if "看多" in system]
        self.assertEqual(len(bull_calls), 2)
        self.assertNotIn("previous", bull_calls[0])
        self.assertNotIn("后续轮次", bull_prompts[0])
        self.assertIn("看空第一轮", bull_calls[1])
        self.assertNotIn("assumption", bull_calls[1])
        self.assertNotIn("history", bull_calls[1])
        self.assertIn("后续轮次", bull_prompts[1])
        self.assertEqual(result["bull"]["points"][0]["claim"], "看多第二轮")
        self.assertEqual(result["summary"]["summary"], ["两轮之后的总结"])


class JsonRetryTest(unittest.TestCase):
    def test_retries_once_after_invalid_json(self):
        from app.agents.llm import complete_json

        class Message:
            def __init__(self, content):
                self.content = content

        class FlakyModel:
            def __init__(self):
                self.calls = 0
                self.bound = {}

            def bind(self, **kwargs):
                self.bound = kwargs
                return self

            def invoke(self, _messages):
                self.calls += 1
                if self.calls == 1:
                    return Message("不是 JSON")
                return Message('{"ok": true}')

        model = FlakyModel()
        self.assertEqual(complete_json(model, "系统", "用户"), {"ok": True})
        self.assertEqual(model.calls, 2)
        self.assertEqual(model.bound["response_format"], {"type": "json_object"})

    def test_connection_error_is_not_reported_as_bad_json(self):
        from app.agents.llm import ModelError, complete_json

        class DownModel:
            def bind(self, **kwargs):
                return self

            def invoke(self, _messages):
                raise RuntimeError("Connection error.")

        with self.assertRaises(ModelError) as caught:
            complete_json(DownModel(), "系统", "用户")
        self.assertIn("无法连接模型服务", str(caught.exception))
        self.assertNotIn("合法 JSON", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
