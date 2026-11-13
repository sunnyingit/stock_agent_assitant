import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flask import Flask

from app.api import v1
from app.services import market_overseas as overseas


_ZONE = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 11, 11, 10, 0, tzinfo=_ZONE)


class SessionLabelTest(unittest.TestCase):
    def test_kr_live_in_beijing_morning(self):
        state, label = overseas._kr_session(NOW)
        self.assertEqual(state, "live")
        self.assertIn("实时", label)

    def test_us_as_of_skips_weekend(self):
        sat = datetime(2026, 11, 7, 12, 0, tzinfo=_ZONE)  # 周六
        self.assertEqual(overseas._us_as_of(sat), "11-06 收盘")


class ParseTencentTest(unittest.TestCase):
    def test_short_and_full_quote(self):
        text = (
            'v_s_us.INX="200~标普500~.INX~7520.93~-29.01~-0.38~1~1~~";\n'
            'v_kr005930="352~Samsung~005930.KS~206000~198900~197100~1~~~~~~~~~~~~~~~~~~~~~~~~2026-11-11 14:30:20~";\n'
        )
        out = overseas._parse_tencent(text)
        self.assertEqual(out["s_us.INX"]["pct"], -0.38)
        self.assertEqual(out["kr005930"]["pct"], 3.57)


class BuildTest(unittest.TestCase):
    def test_groups_us_and_kr(self):
        quotes = [
            {"f12": "SPX", "f13": 100, "f14": "标普500", "f2": 7520.93, "f3": -0.38, "f4": -29.01},
            {"f12": "NDX", "f13": 100, "f14": "纳斯达克", "f2": 25065.87, "f3": -0.74, "f4": -187.27},
            {"f12": "SOX", "f13": 251, "f14": "费城半导体指数", "f2": 5000.0, "f3": -2.1, "f4": -100.0},
            {"f12": "KS11", "f13": 100, "f14": "韩国KOSPI", "f2": 5375.36, "f3": 2.6, "f4": 136.23},
            {"f12": "000660", "f13": 177, "f14": "SK海力士", "f2": 500000.0, "f3": -2.6, "f4": -1.0},
            {"f12": "005930", "f13": 177, "f14": "三星电子", "f2": 70000.0, "f3": -1.2, "f4": -1.0},
            {"f12": "NVDA", "f13": 105, "f14": "英伟达", "f2": 173.73, "f3": 2.87, "f4": 4.8},
            {"f12": "AAPL", "f13": 105, "f14": "苹果", "f2": 355.63, "f3": 2.04, "f4": 7.1},
            {"f12": "MSFT", "f13": 105, "f14": "微软", "f2": 400.0, "f3": 1.0, "f4": 4.0},
            {"f12": "TSM", "f13": 105, "f14": "台积电", "f2": 200.0, "f3": 1.5, "f4": 3.0},
            {"f12": "AMD", "f13": 105, "f14": "AMD", "f2": 150.0, "f3": -0.5, "f4": -0.8},
            {"f12": "META", "f13": 105, "f14": "Meta", "f2": 500.0, "f3": 0.8, "f4": 4.0},
        ]
        with patch.object(overseas, "_fetch_eastmoney", return_value=quotes), \
             patch.object(overseas, "_fetch_tencent", return_value={}):
            out = overseas.build(now=NOW)
        self.assertEqual(out["status"], "ok")
        self.assertFalse(out["partial"])
        self.assertEqual(out["us"]["title"], "美股")
        self.assertEqual([r["name"] for r in out["us"]["items"]], ["标普 500", "纳斯达克", "费城半导体"])
        self.assertEqual([r["name"] for r in out["kr"]["items"]], ["KOSPI", "SK 海力士", "三星电子"])
        self.assertEqual([r["name"] for r in out["tech"]["items"]], ["英伟达", "苹果", "微软", "台积电", "AMD", "Meta"])
        self.assertEqual(out["us"]["items"][0]["pct"], -0.38)
        self.assertEqual(out["kr"]["items"][0]["pct"], 2.6)
        self.assertEqual(out["tech"]["items"][0]["pct"], 2.87)
        self.assertEqual(out["kr"]["session"], "live")

    def test_tencent_fallback_when_eastmoney_down(self):
        tx = {
            "s_us.INX": {"price": 7520.93, "pct": -0.38, "change": -29.01},
            "s_usIXIC": {"price": 25065.87, "pct": -0.74, "change": -187.27},
            "s_usSOXX": {"price": 146.59, "pct": -0.12, "change": -0.17},
            "kr000660": {"price": 1277000.0, "pct": 5.36, "change": 65000.0},
            "kr005930": {"price": 206000.0, "pct": 3.57, "change": 7100.0},
        }
        with patch.object(overseas, "_fetch_eastmoney", side_effect=RuntimeError("down")), \
             patch.object(overseas, "_fetch_tencent", return_value=tx) as tx_mock, \
             patch.object(overseas, "_fill_kospi_tushare"):
            out = overseas.build(now=NOW)
        self.assertEqual(out["status"], "ok")
        self.assertTrue(out["partial"])  # KOSPI 仍缺
        self.assertTrue(tx_mock.called)
        self.assertEqual(out["us"]["items"][0]["pct"], -0.38)
        self.assertTrue(out["kr"]["items"][1]["ok"])
        self.assertFalse(out["kr"]["items"][0]["ok"])  # KOSPI


class ExtraAndSearchTest(unittest.TestCase):
    def test_parse_extra_secids(self):
        self.assertEqual(overseas._parse_extra_secids("105.TSLA,177.005930"), ["105.TSLA", "177.005930"])
        self.assertEqual(overseas._parse_extra_secids("bad,105."), [])

    def test_build_includes_extra_group(self):
        quotes = [
            {"f12": "SPX", "f13": 100, "f14": "标普500", "f2": 1.0, "f3": 0.1, "f4": 0.0},
            {"f12": "NDX", "f13": 100, "f14": "纳斯达克", "f2": 1.0, "f3": 0.1, "f4": 0.0},
            {"f12": "SOX", "f13": 251, "f14": "费城半导体指数", "f2": 1.0, "f3": 0.1, "f4": 0.0},
            {"f12": "KS11", "f13": 100, "f14": "韩国KOSPI", "f2": 1.0, "f3": 0.1, "f4": 0.0},
            {"f12": "000660", "f13": 177, "f14": "SK海力士", "f2": 1.0, "f3": 0.1, "f4": 0.0},
            {"f12": "005930", "f13": 177, "f14": "三星电子", "f2": 1.0, "f3": 0.1, "f4": 0.0},
            {"f12": "NVDA", "f13": 105, "f14": "英伟达", "f2": 1.0, "f3": 0.1, "f4": 0.0},
            {"f12": "AAPL", "f13": 105, "f14": "苹果", "f2": 1.0, "f3": 0.1, "f4": 0.0},
            {"f12": "MSFT", "f13": 105, "f14": "微软", "f2": 1.0, "f3": 0.1, "f4": 0.0},
            {"f12": "TSM", "f13": 105, "f14": "台积电", "f2": 1.0, "f3": 0.1, "f4": 0.0},
            {"f12": "AMD", "f13": 105, "f14": "AMD", "f2": 1.0, "f3": 0.1, "f4": 0.0},
            {"f12": "META", "f13": 105, "f14": "Meta", "f2": 1.0, "f3": 0.1, "f4": 0.0},
            {"f12": "TSLA", "f13": 105, "f14": "特斯拉", "f2": 400.0, "f3": 1.2, "f4": 5.0},
        ]
        with patch.object(overseas, "_fetch_eastmoney", return_value=quotes), \
             patch.object(overseas, "_fetch_tencent", return_value={}):
            out = overseas.build(now=NOW, extra="105.TSLA", names={"105.TSLA": "特斯拉"})
        self.assertEqual(len(out["extra"]["items"]), 1)
        self.assertEqual(out["extra"]["items"][0]["code"], "TSLA")
        self.assertEqual(out["extra"]["items"][0]["pct"], 1.2)
        self.assertEqual(len(out["tech"]["items"]), 6)

    def test_search_filters_us_and_kr(self):
        fake = {
            "QuotationCodeTable": {
                "Data": [
                    {"Code": "TSLA", "Name": "特斯拉", "MktNum": "105", "Classify": "UsStock", "SecurityTypeName": "美股", "QuoteID": "105.TSLA"},
                    {"Code": "600519", "Name": "贵州茅台", "MktNum": "1", "Classify": "AStock", "SecurityTypeName": "沪市A股", "QuoteID": "1.600519"},
                    {"Code": "005930", "Name": "三星电子", "MktNum": "177", "Classify": "KRX", "SecurityTypeName": "韩股", "QuoteID": "177.005930"},
                ]
            }
        }
        with patch.object(overseas, "_http_json", return_value=fake):
            items = overseas.search("tesla")
        self.assertEqual([i["secid"] for i in items], ["105.TSLA", "177.005930"])


class ApiTest(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.register_blueprint(v1.bp, url_prefix="/api/v1")
        self.client = app.test_client()

    def test_overseas_route(self):
        fake = {
            "status": "ok", "generated_at": "2026-11-11 10:00", "partial": False,
            "errors": [], "us": {"title": "美股 · 隔夜", "subtitle": "", "items": []},
            "kr": {"title": "韩股", "subtitle": "", "session": "live", "items": []},
            "tech": {"title": "科技股", "subtitle": "", "items": []},
            "extra": {"title": "自选", "subtitle": "", "items": []},
            "note": "x",
        }
        with patch.object(overseas, "build", return_value=fake) as build:
            res = self.client.get("/api/v1/market/overseas?extra=105.TSLA")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["status"], "ok")
        build.assert_called()
        self.assertEqual(build.call_args.kwargs.get("extra"), "105.TSLA")

    def test_overseas_search_route(self):
        with patch.object(overseas, "search", return_value=[{"secid": "105.TSLA", "name": "特斯拉", "code": "TSLA"}]):
            res = self.client.get("/api/v1/market/overseas/search?q=TSLA")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["items"][0]["code"], "TSLA")


if __name__ == "__main__":
    unittest.main()
