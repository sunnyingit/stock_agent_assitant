import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from .fixtures import NOW, ScriptedModel, finance_payload, loaders, make_aligned, make_stock
from app.synthesis import policy, service, worker
from app.synthesis.api import bp as synthesis_bp
from app.synthesis.gateway import Gateway
from app.synthesis.outcomes import settle
from app.synthesis.store import Store


def run(store, mode="rule", model=None, now=NOW, **extra):
    aligned = make_aligned()
    return service.compute(
        make_stock(), store, gateway=Gateway(model) if model else None, mode=mode, now=now,
        loaders=loaders(aligned, **extra.pop("overrides", {})), aligned=aligned, **extra,
    )


class RuleModeTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def test_page_shape_without_any_model(self):
        reply = run(self.store)
        page = reply["display"]
        self.assertEqual(page["headline"], "偏多：量价资金、新闻都在推")
        self.assertEqual(page["direction"]["label"], "偏多 · 明显")
        self.assertEqual(page["position"]["label"], "适中")
        self.assertEqual(page["credibility"]["done"], 5)
        self.assertEqual(page["credibility"]["total"], 5)
        self.assertEqual(page["credibility"]["level"], "high")
        self.assertEqual(page["credibility"]["subtitle"], "5 个方面里 5 个有结论：4 个偏多 · 1 个中性，数据都是今天的")
        self.assertEqual([item["key"] for item in page["dimensions"]], list(policy.DIMENSIONS))
        self.assertTrue(page["counter"]["text"].startswith("不过"))
        self.assertIn("up", page["levels"])
        self.assertIsNotNone(page["levels"]["down"])   # 下方价不为空
        self.assertFalse(page["levels"]["down"]["verified"])
        self.assertTrue(page["levels"]["up"]["verified"])
        self.assertIn("没有通过重算验证", page["level_logic"])
        self.assertEqual(page["levels"]["up"]["line"], "▲ 25.00 再涨 13.8%")
        self.assertIn("25.00", page["level_logic"])
        self.assertIn("13.8%", page["level_logic"])
        self.assertIn("3% 到 15%", page["level_logic"])
        self.assertIn("净得分", page["judgment_logic"])
        self.assertIn("主分析员没有改它", page["judgment_logic"])
        self.assertEqual(page["disclaimer"], policy.DISCLAIMER)
        self.assertEqual(reply["internal"]["source"], "rule")
        text = str(page)
        for word in policy.BANNED_WORDS:
            self.assertNotIn(word, text)

    def test_second_call_with_same_facts_is_reused(self):
        first = run(self.store)
        second = run(self.store)
        self.assertFalse(first["internal"]["reused"])
        self.assertTrue(second["internal"]["reused"])
        self.assertEqual(first["internal"]["run_id"], second["internal"]["run_id"])
        self.assertEqual(len(self.store.recent("600001.SH")), 1)
        third = run(self.store, force=True)
        self.assertFalse(third["internal"]["reused"])

    def test_changed_facts_make_a_new_run_and_a_change_reason(self):
        run(self.store)
        reply = run(self.store, overrides={"finance": finance_payload(red=True)})
        page = reply["display"]
        self.assertEqual(page["direction"]["lean"], "unclear")
        self.assertTrue(page["change"]["changed"])
        self.assertTrue(page["change"]["text"].startswith("从偏多变为不明"))
        self.assertEqual(page["credibility"]["level"], "medium")   # 命中红线降一档

    def test_a_failed_dimension_lowers_coverage_not_the_whole_answer(self):
        def boom():
            raise RuntimeError("数据源挂了")

        reply = run(self.store, overrides={"sector": boom})
        page = reply["display"]
        self.assertEqual(page["credibility"]["done"], 4)
        sector = next(item for item in page["dimensions"] if item["key"] == "sector")
        self.assertEqual(sector["status"], "unavailable")
        self.assertIn("数据源挂了", sector["reason"])
        self.assertIn(page["direction"]["lean"], ("bullish", "unclear"))

    def test_archive_keeps_what_an_audit_needs(self):
        run(self.store)
        record = self.store.latest("600001.SH")
        data = record["data"]
        self.assertEqual((record["policy_version"], record["session"], record["trade_date"]), (policy.POLICY_VERSION, "close", "2026-10-29"))
        for key in ("table", "rule", "hygiene", "display_rule", "levels_rule", "final_state"):
            self.assertIn(key, data)
        self.assertEqual(data["table"]["facts"][0]["id"], "flow.view")


class AgentModesTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def test_shadow_shows_rule_but_archives_the_agent(self):
        reply = run(self.store, mode="shadow", model=ScriptedModel())
        self.assertEqual(reply["internal"]["source"], "rule")
        self.assertEqual(reply["display"]["direction"]["lean"], "bullish")
        record = self.store.latest("600001.SH")
        self.assertEqual(record["source"], "agent")
        self.assertIsNotNone(record["data"]["display_agent"])
        self.assertEqual(len(record["data"]["decision"]["runs"]), 3)
        self.assertEqual(len(record["data"]["calls"]), 5)

    def test_agent_mode_shows_the_agent_with_code_numbers(self):
        reply = run(self.store, mode="agent", model=ScriptedModel())
        page = reply["display"]
        self.assertEqual(reply["internal"]["source"], "agent")
        self.assertEqual(page["headline"], "偏多：量价和板块都在推")
        self.assertEqual(page["direction"]["label"], "偏多 · 稍微")   # 分差 3 → 强度 1，由代码换算
        self.assertEqual([item["text"] for item in page["reasons"]][:2], ["量价形态为价涨量增", "板块处于主升阶段"])
        self.assertEqual(page["counter"]["text"], "不过，未来 20 个交易日内有解禁")
        self.assertEqual(page["levels"]["up"]["line"], "▲ 25.00 再涨 13.8%")
        self.assertIn("采信了", page["judgment_logic"])
        self.assertIn("分差", page["judgment_logic"])
        self.assertIn("看多", page["judgment_logic"])
        self.assertEqual(page["watch"][0]["id"], "E1")
        self.assertTrue(self.store.latest("600001.SH")["diverged"] in (0, 1))

    def test_agent_failure_falls_back_to_the_rule_conclusion(self):
        reply = run(self.store, mode="agent", model=ScriptedModel(fail=True))
        self.assertEqual(reply["internal"]["source"], "rule")
        self.assertEqual(reply["internal"]["fallback_reason"], "model_error")
        self.assertEqual(reply["display"]["direction"]["lean"], "bullish")

    def test_inconsistent_runs_lower_the_credibility(self):
        positions = iter(["偏低", "适中", "偏高"])

        def vary(output, _payload):
            output["position"] = next(positions)

        reply = run(self.store, mode="agent", model=ScriptedModel(chief=vary))
        self.assertEqual(reply["internal"]["fallback_reason"], "inconsistent")
        self.assertEqual(reply["display"]["credibility"]["level"], "medium")

    def test_diverged_samples_are_flagged(self):
        def bearish(output, payload):
            output.update(direction="偏空", headline="解禁带来压力")
            for row in output["verdicts"]:
                keep = row["ref"] == "bear.1"
                row.update(adopt=keep, reason="" if keep else "weak")
            output["levels"].pop("down", None)

        model = ScriptedModel(
            bull=[{"claim": "板块处于主升阶段", "cites": ["sector.stage"], "weight": 1}],
            bear=[{"claim": "未来 20 个交易日内有解禁", "cites": ["fund.holder_event"], "weight": 3}], chief=bearish,
        )
        reply = run(self.store, mode="agent", model=model)
        # 多数维度偏多而 Agent 偏空，且只引用了一个维度：底线不通过，退回规则版
        self.assertEqual(reply["internal"]["fallback_reason"], "validation")
        self.assertFalse(self.store.latest("600001.SH")["diverged"])

    def test_transient_failures_are_retried_not_reused(self):
        run(self.store, mode="agent", model=ScriptedModel(fail=True))
        reply = run(self.store, mode="agent", model=ScriptedModel())
        self.assertFalse(reply["internal"]["reused"])
        self.assertEqual(reply["internal"]["source"], "agent")
        again = run(self.store, mode="agent", model=ScriptedModel())
        self.assertTrue(again["internal"]["reused"])

    def test_intraday_uses_a_single_chief_call_and_its_own_session(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        morning = datetime(2026, 10, 29, 10, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
        model = ScriptedModel()
        # 盘中数据日期是上一个收盘日之后的"今天"，这里只检查会话与调用次数
        run(self.store, mode="shadow", model=model, now=morning)
        record = self.store.latest("600001.SH")
        self.assertEqual(record["session"], "intraday")
        self.assertEqual(len(record["data"]["decision"]["runs"]), 1)


class ExplainTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def test_price_logic_says_where_each_price_comes_from(self):
        logic = run(self.store)["display"]["price_logic"]
        self.assertEqual(len(logic["principles"]), 4)
        up, down = logic["up"], logic["down"]
        self.assertEqual(up["price"], 25.0)
        self.assertIn("最近 60 天里最高的收盘价", "".join(up["how_calc"]))
        self.assertIn("最近 250 天里最高的收盘价", "".join(up["how_calc"]))
        self.assertTrue(up["verified"])
        self.assertFalse(down["verified"])
        self.assertIn("只当参考", down["chosen_because"])
        results = {item["name"]: item["result"] for item in up["candidates"] + down["candidates"]}
        self.assertEqual(results["前高"], "留下")
        self.assertEqual(results["一年高点"], "合并")
        self.assertEqual(results["位置变偏高价"], "剔除")
        notes = {item["name"]: item["note"] for item in down["candidates"]}
        self.assertIn("太近", notes["消息发布前价位"])
        self.assertIn("太远", notes["量价形态失效价"])
        self.assertIn("只作参考", notes["放量密集区"])
        self.assertIn("合并", [item["result"] for item in down["candidates"] if item["name"] == "20 日均线"][0])
        self.assertNotIn("20 日最低收盘 18.18", str(down["summary"]))

    def test_rule_judgment_separates_bull_bear_and_the_missing_chief(self):
        judgment = run(self.store)["display"]["judgment"]
        self.assertEqual(judgment["source"], "rule")
        self.assertEqual(len(judgment["bull"]["points"]), 4)
        self.assertEqual(judgment["bear"]["points"], [])
        self.assertIn("没有哪个方面看空", judgment["bear"]["summary"])
        first = judgment["bull"]["points"][0]
        self.assertIn("量价形态：价涨量增", first["evidence"])
        self.assertIn("规则打分", judgment["chief"]["role"])
        self.assertEqual(judgment["chief"]["conclusion"], "偏多，明显；位置适中")
        self.assertIn("净得分", judgment["chief"]["calc"])
        self.assertIn("一年高点下方", judgment["chief"]["position_basis"])

    def test_agent_judgment_shows_each_side_the_verdict_and_the_arithmetic(self):
        model = ScriptedModel(
            bull=[
                {"claim": "量价形态为价涨量增", "cites": ["flow.pattern"], "weight": 3},
                {"claim": "板块处于主升阶段", "cites": ["sector.stage"], "weight": 2},
            ],
            bear=[{"claim": "未来 20 个交易日内有解禁", "cites": ["fund.holder_event"], "weight": 1}],
            chief=lambda output, payload: output["verdicts"][1].update(weight=1, reason="priced_in"),
        )
        judgment = run(self.store, mode="agent", model=model)["display"]["judgment"]
        self.assertEqual(judgment["source"], "agent")
        bull, bear, chief = judgment["bull"], judgment["bear"], judgment["chief"]
        self.assertEqual(bull["counted"], 4)
        self.assertIn("调低", bull["points"][1]["status"])
        self.assertIn("利好已反应", bull["points"][1]["status"])
        self.assertEqual(bear["counted"], 1)
        self.assertIn("解禁", bear["points"][0]["text"])
        self.assertIn("采信的看多 4 分，减去采信的看空 1 分，分差 +3", chief["calc"])
        self.assertIn("2～3 分是「稍微」", chief["calc"])
        self.assertEqual(chief["conclusion"], "偏多，稍微；位置适中")
        self.assertIn("解禁", chief["if_wrong"])
        self.assertIn("不能新编论据", chief["role"])

    def test_rejected_and_empty_sides_are_explained(self):
        def reject_bear(output, payload):
            output["verdicts"][-1].update(adopt=False, reason="weak")

        model = ScriptedModel(bear=[{"claim": "未来 20 个交易日内有解禁", "cites": ["fund.holder_event"], "weight": 1}], chief=reject_bear)
        judgment = run(self.store, mode="agent", model=model)["display"]["judgment"]
        self.assertEqual(judgment["bear"]["points"][0]["status"], "不采信：证据偏弱")
        self.assertEqual(judgment["bear"]["counted"], 0)
        none = run(Store(":memory:"), mode="agent", model=ScriptedModel(bear=[]))["display"]["judgment"]
        self.assertIn("没有找到有力的看空论据", none["bear"]["summary"])

    def test_fallback_reason_is_explained_in_plain_words(self):
        judgment = run(self.store, mode="agent", model=ScriptedModel(fail=True))["display"]["judgment"]
        self.assertIn("模型这次调用失败", judgment["chief"]["role"])

    def test_explanations_stay_clean(self):
        page = run(self.store, mode="agent", model=ScriptedModel())["display"]
        text = str(page["judgment"]) + str(page["price_logic"])
        for word in policy.BANNED_WORDS:
            self.assertNotIn(word, text)


class StoreWorkerTest(unittest.TestCase):
    def test_enqueue_debounces_and_caps_the_wait(self):
        store = Store(":memory:")
        store.enqueue("A", 30, "x", max_wait=100, now=1000)
        store.enqueue("A", 30, "y", max_wait=100, now=1020)
        self.assertEqual(store.claim_due(now=1040), [])
        due = store.claim_due(now=1051)
        self.assertEqual([item["ts_code"] for item in due], ["A"])
        store.finish("A")
        store.enqueue("B", 30, "", max_wait=100, now=1000)
        store.enqueue("B", 30, "", max_wait=100, now=1090)
        store.enqueue("B", 30, "", max_wait=100, now=1099)
        self.assertEqual([item["ts_code"] for item in store.claim_due(now=1100)], ["B"])   # 最久等 100 秒

    def test_worker_retries_while_a_dimension_is_pending_then_gives_up(self):
        store = Store(":memory:")
        calls = []

        def runner(ts_code, reason):
            calls.append((ts_code, reason))
            return {"pending": ["news"]}

        runner_worker = worker.Worker(store, runner)
        store.enqueue("A", 0, "dimension:news", now=0)
        for tick in range(5):
            runner_worker.tick(now=tick * 100 + 1)
        self.assertEqual(len(calls), worker.MAX_ATTEMPTS)
        self.assertFalse(store.pending("A"))

    def test_worker_survives_runner_errors_and_notify_needs_a_running_worker(self):
        store = Store(":memory:")

        def runner(ts_code, reason):
            raise RuntimeError("boom")

        runner_worker = worker.Worker(store, runner)
        store.enqueue("A", 0, "", now=0)
        self.assertEqual(runner_worker.tick(now=1), 1)
        self.assertTrue(store.pending("A"))
        worker.set_store(store)
        try:
            worker.notify("Z", "news")   # 没有起后台线程：什么都不做
            self.assertFalse(store.pending("Z"))
        finally:
            worker.set_store(None)

    def test_stuck_jobs_are_released_after_a_restart(self):
        store = Store(":memory:")
        store.enqueue("A", 0, "", now=0)
        store.claim_due(now=1)
        self.assertEqual(store.claim_due(now=2), [])
        store.release_stuck(older_than=10, now=100)
        self.assertEqual(len(store.claim_due(now=101)), 1)

    def test_file_store_persists(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "synthesis.db"
            first = Store(path)
            first.enqueue("A", 0, "")
            self.assertTrue(Store(path).pending("A"))


class OutcomeTest(unittest.TestCase):
    def test_settle_and_stats_split_agent_and_rule(self):
        store = Store(":memory:")
        run(store, mode="agent", model=ScriptedModel())
        dates = [f"2026-10-{day:02d}" for day in range(29, 32)] + [f"2026-11-{day:02d}" for day in range(1, 29)]
        closes = {date: 10.0 + index * 0.1 for index, date in enumerate(dates)}
        series = lambda _code: [{"date": date, "close": closes[date]} for date in dates]
        self.assertEqual(settle(store, series), 3)
        self.assertEqual(settle(store, series), 0)   # 已回填的不重复
        stats = {(row["source"], row["level"]): row for row in store.stats(5)}
        self.assertEqual(stats[("agent", 1)]["hit_rate"], 1.0)
        self.assertEqual(stats[("rule", 3)]["n"], 1)

    def test_runs_without_enough_future_bars_stay_unsettled(self):
        store = Store(":memory:")
        run(store)
        self.assertEqual(settle(store, lambda _code: [{"date": "2026-10-29", "close": 10.0}]), 0)
        self.assertEqual(len(store.unsettled(5)), 1)


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        worker.set_store(self.store)
        app = Flask(__name__)
        app.register_blueprint(synthesis_bp)
        self.client = app.test_client()
        stock = make_stock()
        aligned = make_aligned()

        def compute(stock_arg, store, mode=None, **_):
            return service.compute(stock_arg, store, mode=mode or "rule", now=NOW, loaders=loaders(aligned), aligned=aligned)

        patches = [
            patch("app.synthesis.api.resolve_stock", lambda code: stock if code in ("600001.SH", "测试股份") else None),
            patch("app.synthesis.api.compute", compute),
            patch("app.synthesis.api.now_cn", lambda: NOW),   # 固定"现在"，测试不随真实时间漂移
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.addCleanup(worker.set_store, None)

    def test_first_view_computes_the_rule_version_inline(self):
        body = self.client.get("/api/v1/stocks/600001.SH/synthesis").get_json()
        self.assertEqual(body["status"], "ok")
        self.assertNotIn("display", body)
        self.assertEqual(body["headline"]["status"], "support")
        self.assertNotIn("internal", body)
        self.assertFalse(body["computing"])

    def test_old_records_get_the_explanations_filled_in_on_read(self):
        self.client.get("/api/v1/stocks/600001.SH/synthesis")
        record = self.store.latest("600001.SH")
        page = record["data"]["display_rule"]
        for key in ("price_logic", "judgment", "level_logic", "judgment_logic"):
            page.pop(key)
        with self.store._conn() as conn:
            import json as _json
            conn.execute("UPDATE runs SET data=? WHERE id=?", (_json.dumps(record["data"], ensure_ascii=False), record["id"]))
        body = self.client.get("/api/v1/stocks/600001.SH/synthesis").get_json()
        self.assertNotIn("display", body)
        self.assertEqual(body["headline"]["status"], "support")
        self.assertIn("watch", body)

    def test_debug_flag_adds_internal_and_unknown_stock_is_404(self):
        body = self.client.get("/api/v1/stocks/600001.SH/synthesis?debug=1").get_json()
        self.assertIn("run_id", body["internal"])
        self.assertEqual(self.client.get("/api/v1/stocks/000000.SZ/synthesis").status_code, 404)

    def test_stale_record_is_served_as_it_is_and_nothing_is_queued(self):
        self.client.get("/api/v1/stocks/600001.SH/synthesis")
        with patch("app.synthesis.api.now_cn", lambda: NOW.replace(day=30)), \
                patch("app.synthesis.api.last_close_date", lambda now: now.date()):
            body = self.client.get("/api/v1/stocks/600001.SH/synthesis").get_json()
        self.assertFalse(body["computing"])        # 数据服务不重算，过期的结论照样返回，由客户端的定时任务更新
        self.assertEqual(body["headline"]["status"], "support")

    def test_records_from_an_older_policy_are_served_and_nothing_is_queued(self):
        self.client.get("/api/v1/stocks/600001.SH/synthesis")
        with self.store._conn() as conn:
            conn.execute("UPDATE runs SET policy_version='policy-0'")
        body = self.client.get("/api/v1/stocks/600001.SH/synthesis").get_json()
        self.assertFalse(body["computing"])

    def test_manual_refresh_is_ignored_because_the_client_makes_the_conclusion(self):
        response = self.client.post("/api/v1/stocks/600001.SH/synthesis/refresh")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json()["status"], "ignored")
        self.assertFalse(self.store.pending("600001.SH"))


if __name__ == "__main__":
    unittest.main()
