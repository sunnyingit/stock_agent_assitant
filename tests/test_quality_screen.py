import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flask import Flask

from app.api.shared import bp
from app.services import quality_screen as q
from app.tools import cache


def row(end, **kw):
    base = {"end_date": end, "roe": 12.0, "debt_to_assets": 40.0, "tr_yoy": 10.0, "netprofit_yoy": 10.0, "profit_dedt": 1e8, "ocf_to_profit": 90.0, "ocf_to_or": 8.0, "netprofit_margin": 9.0}
    base.update(kw)
    return base


GOOD = [row("20241231"), row("20251231"), row("20260930", roe=9.0)]


class ScreenTest(unittest.TestCase):
    def test_a_sound_company_is_clean_and_roe_is_put_on_a_yearly_footing(self):
        out = q.screen("好公司", "家用电器", GOOD)
        self.assertEqual((out["level"], out["flags"]), ("clean", []))
        self.assertAlmostEqual(out["metrics"]["roe"], 0.12, places=3)   # 9% over three quarters is 12% a year
        self.assertAlmostEqual(out["metrics"]["cash"], 0.9)

    def test_red_lines_st_two_years_of_losses_investigation_and_extreme_debt(self):
        losing = [row("20241231", profit_dedt=-5e7), row("20251231", profit_dedt=-1e7), row("20260930")]
        out = q.screen("*ST 某某", "机械", losing, [{"topic": "立案调查", "date": "2026-05-01"}, {"topic": "立案调查", "date": "2026-06-01"}, {"topic": "警示函", "date": "2026-01-01"}])
        texts = [f["text"] for f in out["flags"] if f["level"] == "red"]
        self.assertEqual(out["level"], "red")
        self.assertTrue(any("ST" in t for t in texts) and any("连续两个年度" in t for t in texts))
        self.assertEqual(sum("立案调查" in t for t in texts), 1)       # the same topic is said once
        self.assertTrue(any(f["level"] == "attention" and "警示函" in f["text"] for f in out["flags"]))
        heavy = q.screen("某公司", "机械", [row("20251231", debt_to_assets=93.0)])
        self.assertEqual([f["level"] for f in heavy["flags"]], ["red"])

    def test_attention_items_and_what_banks_and_property_firms_are_excused(self):
        out = q.screen("某公司", "机械", [row("20241231", ocf_to_or=-2.0), row("20251231", profit_dedt=-1e6, ocf_to_or=-1.0), row("20260930", roe=1.0, tr_yoy=-5.0, debt_to_assets=80.0)])
        text = " ".join(f["text"] for f in out["flags"])
        for part in ("最近一个年度扣非净利润为负", "经营现金流连续两个年度为负", "资产负债率 80%", "盈利很弱"):
            self.assertIn(part, text)
        self.assertEqual(out["level"], "attention")
        bank = q.screen("某银行", "银行", [row("20251231", debt_to_assets=92.0)])
        self.assertEqual(bank["level"], "clean")                       # a bank's leverage is the business
        self.assertEqual(q.screen("无数据", "机械", [])["level"], "unknown")


class RankTest(unittest.TestCase):
    def test_only_clean_profitable_cash_generating_companies_are_ranked_by_quality(self):
        def make(code, name, **kw):
            return {"ts_code": code, "name": name, "total_mv_yi": 100, "result": q.screen(name, "机械", [row("20251231", **kw)])}
        rows = [
            make("A", "高质量", roe=22.0, tr_yoy=30.0, debt_to_assets=20.0, ocf_to_profit=120.0),
            make("B", "一般", roe=9.0, tr_yoy=3.0, debt_to_assets=60.0, ocf_to_profit=70.0),
            make("C", "不赚钱", roe=3.0),
            make("D", "现金差", roe=15.0, ocf_to_profit=20.0),
            make("E", "亏损", profit_dedt=-1e8),
            make("F", "*ST 垃圾", roe=20.0),
        ]
        out = q.rank(rows, 3)
        self.assertEqual([r["name"] for r in out], ["高质量", "一般"])
        self.assertIn("ROE 22.0%", out[0]["why"])
        self.assertGreater(out[0]["score"], out[1]["score"])


class CandidatesTest(unittest.TestCase):
    def setUp(self):
        cache.clear()
        q._JOBS.clear()

    def test_it_screens_the_industry_pool_in_the_background_and_leaves_out_the_stock_itself_and_st(self):
        import time
        from types import SimpleNamespace

        stock = SimpleNamespace(ts_code="X.SZ", name="某某", industry="机械")
        members = [{"ts_code": c} for c in ("X.SZ", "A.SZ", "B.SZ", "S.SZ")]
        names = {"X.SZ": "某某", "A.SZ": "甲公司", "B.SZ": "乙公司", "S.SZ": "ST 丙"}
        fina = {"A.SZ": [row("20251231", roe=20.0)], "B.SZ": [row("20251231", roe=10.0)], "S.SZ": [row("20251231")]}
        sources = {"industry": "机械", "members": members, "market": {"A.SZ": {"total_mv": 2e6}, "B.SZ": {"total_mv": 1e6}}, "name_of": names.get, "fina": lambda code: fina[code]}
        self.assertEqual(q.candidates(stock, 3, sources=sources)["status"], "computing")
        for _ in range(50):
            time.sleep(0.05)
            out = q.candidates(stock, 3, sources=sources)
            if out["status"] == "ok":
                break
        self.assertEqual([i["name"] for i in out["items"]], ["甲公司", "乙公司"])
        self.assertEqual((out["checked"], out["industry"]), (2, "机械"))
        self.assertEqual(len(q.candidates(stock, 1)["items"]), 1)   # served from the cache, cut to the limit


class EndpointTest(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.register_blueprint(bp)
        self.client = app.test_client()

    def test_quality_and_peers_endpoints(self):
        from types import SimpleNamespace
        stock = SimpleNamespace(ts_code="X.SZ", name="某某", industry="机械")
        with patch("app.tools.stock.resolve_stock", return_value=stock), patch("app.services.quality_screen.check", return_value={"ts_code": "X.SZ", "level": "clean", "flags": []}):
            body = self.client.get("/api/v1/shared/quality/X.SZ").get_json()
        self.assertEqual((body["status"], body["level"]), ("ok", "clean"))
        with patch("app.tools.stock.resolve_stock", return_value=None):
            self.assertEqual(self.client.get("/api/v1/shared/quality/NOPE").status_code, 404)
        with patch("app.tools.stock.resolve_stock", return_value=stock), patch("app.services.quality_screen.candidates", return_value={"status": "computing", "items": []}) as call:
            self.assertEqual(self.client.get("/api/v1/shared/quality-peers/X.SZ?limit=2").get_json()["status"], "computing")
        call.assert_called_once_with(stock, 2)
        with patch("app.tools.stock.resolve_stock", return_value=stock):
            self.assertEqual(self.client.get("/api/v1/shared/quality-peers/X.SZ?limit=x").status_code, 400)


if __name__ == "__main__":
    unittest.main()
