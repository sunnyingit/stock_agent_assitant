import unittest
from dataclasses import replace
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from flask import Flask

from .fixtures import NOW, finance_payload, loaders, make_aligned, make_stock
from app.synthesis import quotes, service, watchlist, worker
from app.synthesis.api import bp as synthesis_bp
from app.synthesis.store import Store
from app.tools import cache

ZONE = ZoneInfo("Asia/Shanghai")
TENCENT = (
    'v_sh600519="1~贵州茅台~600519~1355.00~1350.00~1351.00~1~1~1~";\n'
    'v_sz002430="51~杭氧股份~002430~30.96~30.40~30.50~1~1~1~";\n'
    'v_hk00700="100~腾讯控股~00700~480.00~470.00~475~1~1~1~";\n'
    'v_sz000001="51~平安银行~000001~0.00~11.00~11.0~1~1~1~";\n'
)


def stock_named(code: str, name: str):
    return replace(make_stock(), ts_code=code, code=code.split(".")[0], name=name)


def judge(store, code, name, now=NOW, **overrides):
    aligned = make_aligned()
    service.compute(stock_named(code, name), store, mode="rule", now=now, loaders=loaders(aligned, **overrides), aligned=aligned)


def prices(table):
    return lambda stocks: {stock.ts_code: table[stock.ts_code] for stock in stocks if stock.ts_code in table}


def quote(price, prev=None):
    prev = prev or price
    return {"price": price, "prev": prev, "change": round(price - prev, 2), "change_pct": round((price / prev - 1) * 100, 2)}


class QuotesTest(unittest.TestCase):
    def setUp(self):
        cache.clear()

    def test_batch_parse_and_skip_unusable_quotes(self):
        calls = []
        stocks = [stock_named("600519.SH", "贵州茅台"), stock_named("002430.SZ", "杭氧股份"), stock_named("000001.SZ", "平安银行")]
        result = quotes.fetch(stocks, lambda symbols: (calls.append(symbols), TENCENT)[1])
        self.assertEqual(len(calls), 1)                       # 一批只请求一次
        self.assertEqual(calls[0], ["sh600519", "sz002430", "sz000001"])
        self.assertEqual(result["600519.SH"], {"price": 1355.0, "prev": 1350.0, "change": 5.0, "change_pct": 0.37})
        self.assertEqual(result["002430.SZ"]["change_pct"], 1.84)
        self.assertNotIn("000001.SZ", result)                  # 价格为 0：停牌或还没开盘

    def test_short_cache_and_failures_do_not_raise(self):
        stock = stock_named("600519.SH", "贵州茅台")
        quotes.fetch([stock], lambda symbols: TENCENT)
        self.assertIn("600519.SH", quotes.fetch([stock], lambda symbols: 1 / 0))   # 命中缓存，不再请求
        cache.clear()
        self.assertEqual(quotes.fetch([stock], lambda symbols: 1 / 0), {})

    def test_hk_symbol(self):
        hk = replace(make_stock(), ts_code="00700.HK", code="00700", name="腾讯控股")
        seen = []
        result = quotes.fetch([hk], lambda symbols: (seen.append(symbols), TENCENT)[1])
        self.assertEqual(seen, [["hk00700"]])
        self.assertEqual(result["00700.HK"]["price"], 480.0)

    def test_search_ranks_prefix_matches_first(self):
        listed = [
            {"ts_code": "000001.SZ", "symbol": "000001", "name": "平安银行", "industry": "银行"},
            {"ts_code": "002430.SZ", "symbol": "002430", "name": "杭氧股份", "industry": "化工"},
            {"ts_code": "600519.SH", "symbol": "600519", "name": "贵州茅台", "industry": "白酒"},
            {"ts_code": "300750.SZ", "symbol": "300750", "name": "宁德时代", "industry": "电池"},
        ]
        empty_hk = lambda: []
        self.assertEqual([row["name"] for row in quotes.search("杭氧", listed=lambda: listed, hk_listed=empty_hk)], ["杭氧股份"])
        self.assertEqual([row["ts_code"] for row in quotes.search("0024", listed=lambda: listed, hk_listed=empty_hk)], ["002430.SZ"])
        self.assertEqual([row["ts_code"] for row in quotes.search("600519.sh", listed=lambda: listed, hk_listed=empty_hk)], ["600519.SH"])
        self.assertEqual([row["name"] for row in quotes.search("股份", listed=lambda: listed, hk_listed=empty_hk)], ["杭氧股份"])
        self.assertEqual(quotes.search("  ", listed=lambda: listed, hk_listed=empty_hk), [])

    def test_search_includes_hong_kong(self):
        listed = [
            {"ts_code": "600519.SH", "symbol": "600519", "name": "贵州茅台", "industry": "白酒"},
        ]
        hk = [{
            "ts_code": "00700.HK",
            "symbol": "00700",
            "name": "腾讯控股",
            "fullname": "腾讯控股有限公司",
            "industry": None,
        }]
        found = lambda query: quotes.search(query, listed=lambda: listed, hk_listed=lambda: hk)
        self.assertEqual([row["ts_code"] for row in found("腾讯")], ["00700.HK"])
        self.assertEqual([row["ts_code"] for row in found("腾讯控股有限公司")], ["00700.HK"])
        self.assertEqual([row["ts_code"] for row in found("700")], ["00700.HK"])
        self.assertEqual([row["ts_code"] for row in found("00700")], ["00700.HK"])
        self.assertEqual([row["ts_code"] for row in found("700.HK")], ["00700.HK"])
        self.assertEqual([row["name"] for row in found("茅台")], ["贵州茅台"])
        self.assertEqual(found("腾讯")[0]["industry"], None)


class StoreTest(unittest.TestCase):
    def test_watchlist_and_alerts(self):
        store = Store(":memory:")
        self.assertTrue(store.watch_add("002430.SZ", "杭氧股份", now=1))
        self.assertFalse(store.watch_add("002430.SZ", "杭氧股份", now=2))
        store.watch_add("600519.SH", "贵州茅台", now=3)
        self.assertEqual([row["ts_code"] for row in store.watch_list()], ["002430.SZ", "600519.SH"])
        self.assertTrue(store.watch_remove("002430.SZ"))
        self.assertFalse(store.watch_remove("002430.SZ"))
        first = store.alert_add("600519.SH", "down", 1300.0)
        again = store.alert_add("600519.SH", "down", 1300.004)   # 同一个价重复设置：返回已有的
        self.assertEqual(first["id"], again["id"])
        other = store.alert_add("600519.SH", "up", 1500.0)
        self.assertNotEqual(first["id"], other["id"])
        self.assertEqual(len(store.alerts("600519.SH")), 2)
        self.assertTrue(store.alert_remove(first["id"]))
        self.assertEqual(len(store.alerts()), 1)


class BuildTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.refreshed = []

    def build(self, table, now=NOW, mode="rule"):
        return watchlist.build(self.store, prices(table), mode, now)

    def add(self, code, name):
        self.store.watch_add(code, name)

    def test_state_line_shows_direction_and_position(self):
        self.add("600001.SH", "测试股份")
        judge(self.store, "600001.SH", "测试股份")
        item = self.build({"600001.SH": quote(22.35, 22.0)})[0]
        self.assertEqual(item["subtitle"], {"kind": "state", "text": "偏多 · 适中", "tone": "muted"})
        self.assertEqual((item["name"], item["price"], item["change_pct"]), ("测试股份", 22.35, 1.59))
        self.assertEqual(item["direction"]["lean"], "bullish")
        self.assertEqual(item["position"]["value"], "mid")
        self.assertFalse(item["stale"])
        self.assertEqual(self.refreshed, [])

    def test_alert_line_when_near_and_when_reached_and_ignored_when_far(self):
        self.add("600001.SH", "测试股份")
        judge(self.store, "600001.SH", "测试股份")
        self.store.alert_add("600001.SH", "down", 21.70)
        near = self.build({"600001.SH": quote(22.35)})[0]
        self.assertEqual(near["subtitle"], {"kind": "alert", "text": "离提醒价 21.70 差 2.9%", "tone": "warn", "alert_id": 1})
        self.assertEqual(near["alert"]["gap"], 0.0291)
        reached = self.build({"600001.SH": quote(21.5)})[0]
        self.assertEqual(reached["subtitle"]["text"], "已到提醒价 21.70")
        self.assertTrue(reached["alert"]["reached"])
        far = self.build({"600001.SH": quote(30.0)})[0]
        self.assertEqual(far["subtitle"]["kind"], "state")
        up = Store(":memory:")
        up.watch_add("600001.SH", "测试股份")
        judge(up, "600001.SH", "测试股份")
        up.alert_add("600001.SH", "up", 23.0)
        self.assertEqual(watchlist.build(up, prices({"600001.SH": quote(23.4)}), "rule", NOW)[0]["subtitle"]["text"], "已到提醒价 23.00")

    def test_direction_change_is_shown_for_a_few_days(self):
        self.add("600001.SH", "测试股份")
        judge(self.store, "600001.SH", "测试股份", now=datetime(2026, 10, 28, 16, 0, tzinfo=ZONE))
        judge(self.store, "600001.SH", "测试股份", now=NOW, finance=finance_payload(red=True))
        item = self.build({"600001.SH": quote(22.35)})[0]
        self.assertEqual(item["subtitle"], {"kind": "change", "text": "从偏多变为不明", "tone": "warn"})
        later = datetime(2026, 11, 3, 16, 0, tzinfo=ZONE)   # 过了几个交易日：变化不再提示（判断也已过期）
        self.assertEqual(self.build({"600001.SH": quote(22.35)}, now=later)[0]["subtitle"]["kind"], "stale")

    def test_stale_and_empty(self):
        self.add("600001.SH", "测试股份")
        self.add("600002.SH", "还没算过")
        judge(self.store, "600001.SH", "测试股份")
        items = self.build({}, now=datetime(2026, 11, 4, 16, 0, tzinfo=ZONE))
        by_code = {item["ts_code"]: item for item in items}
        self.assertEqual(by_code["600001.SH"]["subtitle"]["text"], "判断已过期")
        self.assertTrue(by_code["600001.SH"]["stale"])
        self.assertEqual(by_code["600002.SH"]["subtitle"]["text"], "还没有判断")
        self.assertIsNone(by_code["600002.SH"]["direction"])
        self.assertIsNone(by_code["600001.SH"]["price"])   # 行情取不到：不报错，价格为空
        self.assertEqual(self.refreshed, [])   # 打开列表不排队重算

    def test_order_puts_what_needs_attention_first(self):
        for code, name in (("600001.SH", "状态"), ("600002.SH", "过期"), ("600003.SH", "变化"), ("600004.SH", "提醒")):
            self.add(code, name)
        yesterday = datetime(2026, 10, 28, 16, 0, tzinfo=ZONE)
        judge(self.store, "600001.SH", "状态")
        judge(self.store, "600002.SH", "过期", now=datetime(2026, 10, 20, 16, 0, tzinfo=ZONE))
        judge(self.store, "600003.SH", "变化", now=yesterday)
        judge(self.store, "600003.SH", "变化", finance=finance_payload(red=True))
        judge(self.store, "600004.SH", "提醒")
        self.store.alert_add("600004.SH", "down", 21.7)
        table = {code: quote(22.35) for code in ("600001.SH", "600002.SH", "600003.SH", "600004.SH")}
        items = self.build(table)
        self.assertEqual([(item["name"], item["subtitle"]["kind"]) for item in items],
                         [("提醒", "alert"), ("变化", "change"), ("状态", "state"), ("过期", "stale")])

    def test_computing_flag(self):
        self.add("600001.SH", "测试股份")
        judge(self.store, "600001.SH", "测试股份")
        self.store.enqueue("600001.SH", 30, "x")
        self.assertTrue(self.build({})[0]["computing"])


class ApiTest(unittest.TestCase):
    def setUp(self):
        cache.clear()
        self.store = Store(":memory:")
        worker.set_store(self.store)
        self.addCleanup(worker.set_store, None)
        app = Flask(__name__)
        app.register_blueprint(synthesis_bp)
        self.client = app.test_client()
        stocks = {"600001.SH": make_stock(), "测试股份": make_stock()}
        self.patches = [
            patch("app.synthesis.api.resolve_stock", lambda code: stocks.get(code)),
            patch("app.synthesis.api.now_cn", lambda: NOW),   # 固定"现在"：否则过了 15:00 夹具里的判断就变成"已过期"，测试随时间漂移
            patch("app.synthesis.api.quotes.fetch", prices({"600001.SH": quote(22.35, 22.0)})),
            patch("app.synthesis.api.quotes.search", lambda query: [
                {"ts_code": "600001.SH", "code": "600001", "name": "测试股份", "industry": "白酒"},
                {"ts_code": "600002.SH", "code": "600002", "name": "测试二号", "industry": "白酒"},
            ] if "测试" in query else []),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        judge(self.store, "600001.SH", "测试股份")

    def test_add_list_remove(self):
        response = self.client.post("/api/v1/watchlist", json={"q": "测试股份"})
        self.assertEqual(response.status_code, 201)
        body = response.get_json()
        self.assertTrue(body["added"])
        self.assertEqual(body["item"]["subtitle"]["text"], "偏多 · 适中")
        self.assertFalse(self.store.pending("600001.SH"))   # 数据服务不排队出判断：结论由客户端的定时任务生成
        again = self.client.post("/api/v1/watchlist", json={"q": "600001.SH"})
        self.assertEqual((again.status_code, again.get_json()["added"]), (200, False))
        listing = self.client.get("/api/v1/watchlist").get_json()
        self.assertEqual([item["ts_code"] for item in listing["items"]], ["600001.SH"])
        self.assertEqual(listing["items"][0]["change_pct"], 1.59)
        self.assertEqual(self.client.delete("/api/v1/watchlist/600001.SH").status_code, 200)
        self.assertEqual(self.client.delete("/api/v1/watchlist/600001.SH").status_code, 404)
        self.assertEqual(self.client.get("/api/v1/watchlist").get_json()["items"], [])

    def test_delete_preflight_allows_cors(self):
        from app.main import create_app

        client = create_app().test_client()
        response = client.open(
            "/api/v1/watchlist/600001.SH",
            method="OPTIONS",
            headers={
                "Origin": "http://127.0.0.1:8080",
                "Access-Control-Request-Method": "DELETE",
            },
        )
        self.assertIn(response.status_code, (200, 204))
        self.assertEqual(response.headers.get("Access-Control-Allow-Origin"), "*")
        methods = response.headers.get("Access-Control-Allow-Methods", "")
        self.assertIn("DELETE", methods)

    def test_add_validation(self):
        self.assertEqual(self.client.post("/api/v1/watchlist", json={}).status_code, 400)
        self.assertEqual(self.client.post("/api/v1/watchlist", json={"q": "不存在"}).status_code, 404)

    def test_search_marks_what_is_already_watched(self):
        self.client.post("/api/v1/watchlist", json={"q": "测试股份"})
        items = self.client.get("/api/v1/stocks/search?q=测试").get_json()["items"]
        self.assertEqual([(item["ts_code"], item["in_watchlist"]) for item in items], [("600001.SH", True), ("600002.SH", False)])
        self.assertEqual(self.client.get("/api/v1/stocks/search?q=").get_json()["items"], [])

    def test_alert_from_the_current_level_then_it_shows_on_the_page_and_in_the_list(self):
        self.client.post("/api/v1/watchlist", json={"q": "测试股份"})
        page = self.client.get("/api/v1/stocks/600001.SH/synthesis").get_json()
        down = next(item for item in page["watch"]["levels"] if item["side"] == "down")
        up = next(item for item in page["watch"]["levels"] if item["side"] == "up")
        self.assertEqual(up["alert"], {"subscribed": False, "alert_id": None})
        created = self.client.post("/api/v1/stocks/600001.SH/alerts", json={"side": "down"})
        self.assertEqual(created.status_code, 201)
        alert = created.get_json()["alert"]
        self.assertEqual((alert["side"], alert["price"]), ("down", down["price"]))
        page = self.client.get("/api/v1/stocks/600001.SH/synthesis").get_json()
        down = next(item for item in page["watch"]["levels"] if item["side"] == "down")
        self.assertEqual(down["alert"], {"subscribed": True, "alert_id": alert["id"]})
        self.assertEqual(len(self.client.get("/api/v1/stocks/600001.SH/alerts").get_json()["items"]), 1)
        again = self.client.post("/api/v1/stocks/600001.SH/alerts", json={"side": "down"}).get_json()["alert"]
        self.assertEqual(again["id"], alert["id"])   # 重复设置不会多出一条
        self.assertEqual(self.client.delete(f"/api/v1/alerts/{alert['id']}").status_code, 200)
        self.assertEqual(self.client.delete(f"/api/v1/alerts/{alert['id']}").status_code, 404)

    def test_alert_with_an_explicit_price_and_validation(self):
        ok = self.client.post("/api/v1/stocks/600001.SH/alerts", json={"side": "down", "price": "21.7"})
        self.assertEqual(ok.get_json()["alert"]["price"], 21.7)
        self.assertEqual(self.client.post("/api/v1/stocks/600001.SH/alerts", json={"side": "sideways"}).status_code, 400)
        self.assertEqual(self.client.post("/api/v1/stocks/600001.SH/alerts", json={"side": "up", "price": "abc"}).status_code, 400)
        self.assertEqual(self.client.post("/api/v1/stocks/600001.SH/alerts", json={"side": "up", "price": -1}).status_code, 400)
        self.assertEqual(self.client.post("/api/v1/stocks/000000.SZ/alerts", json={"side": "up", "price": 1}).status_code, 404)
        listing = self.client.post("/api/v1/watchlist", json={"q": "测试股份"}).get_json()["item"]
        self.assertEqual(listing["subtitle"]["kind"], "alert")   # 现价 22.35 离 21.7 只差 2.9%


if __name__ == "__main__":
    unittest.main()
