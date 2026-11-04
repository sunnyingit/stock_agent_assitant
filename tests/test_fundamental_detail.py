import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.fundamental_detail import build_detail
from app.services.fundamental_detail.detail import public
from app.services.fundamental_detail.copy import write_text
from app.services.fundamental_detail.detail import _display_name, _history
from app.services.fundamental_detail.load import _thin_profit
from app.services.fundamental_detail.rules import (
    choose_metric,
    counter_text,
    five_year_start,
    industry_level,
    is_st_name,
    peer_median,
    period_code,
    pledge_fraction,
    pledge_view,
    quality_level,
    weakest_item,
    weekly_points,
)
from app.tools.stock import stock_from_code


class FundamentalRuleTest(unittest.TestCase):
    def test_low_roe_switches_to_book_value(self):
        metric, value, text, reason = choose_metric("元器件", 118.68, 5.21, 9.31, 149, 0.028, 2.41)
        self.assertEqual(metric, "pb")
        self.assertEqual(value, 5.21)
        self.assertEqual(text, "市净率")
        self.assertEqual(reason, "利润处在低位，市盈率失真")

    def test_profit_drop_also_switches(self):
        metric, _, text, reason = choose_metric("元器件", 20, 2, 3, 20, 0.12, -0.6)
        self.assertEqual(metric, "pb")
        self.assertEqual(text, "市净率")
        self.assertIn("失真", reason)

    def test_healthy_earnings_keep_deducted_pe(self):
        metric, value, text, reason = choose_metric("白酒", 20, 5, 8, 22, 0.2, 0.1)
        self.assertEqual((metric, value, text), ("pe_ttm_deducted", 22, "市盈率（TTM，扣非）"))
        self.assertIsNone(reason)

    def test_pledge_ratio_uses_percent_points(self):
        self.assertAlmostEqual(pledge_fraction(0.80), 0.008)
        view = pledge_view(0.008, None)
        self.assertEqual(view["detail_text"], "质押占总股本 0.80%")
        self.assertFalse(view["risk_hit"])
        self.assertEqual(view["impact"], "none")

    def test_large_pledge_is_a_position_risk_not_a_holder_trade(self):
        view = pledge_view(0.01, 0.8)
        self.assertEqual(view["impact"], "none")
        self.assertIsNone(view["float_ratio"])
        self.assertEqual(view["detail_text"], "控股股东所持股份 80% 已质押")
        self.assertTrue(view["risk_hit"])

    def test_reference_metric_is_left_out_of_the_score(self):
        items = [
            {"key": "profitability", "peer_pctile": 0.39, "value": 0.028, "counts": True},
            {"key": "growth", "peer_pctile": 0.22, "counts": True},
            {"key": "solvency", "peer_pctile": 0.78, "value": 0.19, "counts": True},
            {"key": "cash", "peer_pctile": 0.94, "counts": False, "note_text": "利润偏低，比值仅供参考"},
            {"key": "payout", "peer_pctile": 0.14, "counts": True},
        ]
        self.assertEqual(quality_level(items, 0), "weak")
        text = counter_text(items, distorted=True, pe_value=149, metric="pb", cash_beats_profit=True)
        self.assertNotIn("现金流", text)

    def test_close_weakness_prefers_growth_over_payout(self):
        close = weakest_item([
            {"key": "growth", "peer_pctile": 0.21, "counts": True},
            {"key": "payout", "peer_pctile": 0.20, "counts": True},
        ])
        self.assertEqual(close["key"], "growth")
        apart = weakest_item([
            {"key": "growth", "peer_pctile": 0.22, "counts": True},
            {"key": "payout", "peer_pctile": 0.14, "counts": True},
        ])
        self.assertEqual(apart["key"], "payout")

    def test_st_names_are_not_peers(self):
        self.assertTrue(is_st_name("ST惠伦"))
        self.assertTrue(is_st_name("*ST惠伦"))
        self.assertFalse(is_st_name("晶赛科技"))

    def test_third_quarter_period_code(self):
        self.assertEqual(period_code("20260930"), "2026Q3")

    def test_weekly_series_keeps_high_multiples_and_breaks_on_gaps(self):
        points = weekly_points([
            (date(2026, 1, 5), 50),
            (date(2026, 1, 6), 320),
            (date(2026, 1, 12), None),
            (date(2026, 1, 13), -1),
        ], date(2026, 1, 1), date(2026, 1, 31))
        self.assertEqual(points[0]["value"], 320)
        self.assertIsNone(points[1]["value"])

    def test_history_window_is_five_years(self):
        self.assertEqual(five_year_start(date(2026, 10, 27)), date(2021, 10, 27))

    def test_level_three_needs_five_peers(self):
        self.assertEqual(industry_level(18), "l3")
        self.assertEqual(industry_level(4), "l2")

    def test_history_does_not_drop_nulls_or_high_pe(self):
        points = _history([
            {"date": "2026-02-25", "value": 50},
            {"date": "2026-03-06", "value": 320},
            {"date": "2026-07-17", "value": 80},
        ])
        self.assertEqual(points[0]["value"], 50.0)
        self.assertEqual(points[1]["value"], 320.0)
        self.assertEqual(points[-1]["value"], 80.0)
        self.assertTrue(any(item["value"] is None for item in points[2:-1]))

    def test_peer_median_excludes_self(self):
        mid = peer_median([
            {"value": 5.21, "is_self": True},
            {"value": 4.0, "is_self": False},
            {"value": 6.0, "is_self": False},
        ])
        self.assertEqual(mid, 5.0)

    def test_cash_basis_switches_only_when_margin_is_thin(self):
        self.assertTrue(_thin_profit(0.02, 0.1))
        self.assertFalse(_thin_profit(0.095, 0.1))
        self.assertFalse(_thin_profit(0.02, None))

    def test_code_name_is_replaced(self):
        stock = stock_from_code("603738", "SH")
        self.assertEqual(_display_name(stock, {"stock_name": "泰晶科技"}), "泰晶科技")

    def test_missing_name_is_not_cached(self):
        from app.tools import cache
        from app.tools.stock import resolve_stock

        cache.clear()
        with patch("app.tools.stock._listed_stocks", side_effect=RuntimeError("down")):
            first = resolve_stock("603738.SH")
        self.assertEqual(first.name, "603738")
        rows = [{
            "ts_code": "603738.SH",
            "symbol": "603738",
            "name": "泰晶科技",
            "industry": "元器件",
        }]
        with patch("app.tools.stock._listed_stocks", return_value=rows):
            second = resolve_stock("603738.SH")
        self.assertEqual(second.name, "泰晶科技")


def anchor_snapshot() -> dict:
    """扣非市盈率 20 倍、收盘价 40 元（扣非每股收益 2 元）的最小快照。"""
    return {
        "close": 40.0, "stock_name": "泰晶科技", "price_date": "2026-10-27", "list_date": "20170101", "report_end": "20260630",
        "industry": {"code": "850823.SI", "name": "被动元件", "peer_count": 6, "peers_excluded": 0, "status": "ok"},
        "metric": "pe_ttm_deducted", "metric_text": "市盈率（TTM，扣非）", "value": 20.0, "distorted": False, "pe_reference": 20.0, "cash_beats_profit": False,
        "history": [{"date": f"2025-{month:02d}-05", "value": 14.0 + month} for month in range(1, 13)] + [{"date": "2026-10-27", "value": 20.0}],
        "peers": [{"ts_code": f"00000{i}.SZ", "name": f"同行{i}", "value": 15.0 + i, "is_self": False} for i in range(6)]
        + [{"ts_code": "603738.SH", "name": "泰晶科技", "value": 20.0, "is_self": True}],
        "quality": {"profitability": 0.12, "growth": 0.1, "solvency": 0.3, "cash": 0.9, "payout": 0.3},
        "quality_notes": {}, "peer_quality": {}, "ocf_negative_years": 0, "holder_events": [], "trade_dates": [], "table": {}, "quotes": {},
    }


class PriceAnchorTest(unittest.TestCase):
    """估值锚点价格：默认关闭（资质待确认），打开后 = 自身历史 20/50/80 分位倍数 × 当前扣非每股收益。"""

    def bands(self):
        return {"p20": 20.0, "p50": 27.5, "p80": 38.0}

    def test_off_by_default_so_no_price_leaves_the_server(self):
        from app.services.fundamental_detail.detail import _anchors

        self.assertIsNone(_anchors("pe_ttm_deducted", 21.4, 31.0, self.bands()))

    def test_on_it_multiplies_the_historical_multiples_by_the_deducted_eps(self):
        from app.services.fundamental_detail.detail import ANCHOR_NOTE, _anchors

        with patch("app.config.ENABLE_PRICE_ANCHORS", True):
            anchors = _anchors("pe_ttm_deducted", 20.0, 40.0, self.bands())     # 价格 40，市盈率 20 → 扣非每股收益 2
            self.assertEqual(anchors["eps_ttm_deducted"], 2.0)
            self.assertEqual(anchors["low"], {"multiple": 20.0, "price": 40.0})
            self.assertEqual(anchors["mid"]["price"], 55.0)
            self.assertEqual(anchors["high"]["price"], 76.0)
            self.assertIn("不是目标价", anchors["note"])
            self.assertEqual(anchors["note"], ANCHOR_NOTE)

    def test_only_for_positive_deducted_pe_with_complete_history(self):
        from app.services.fundamental_detail.detail import _anchors

        with patch("app.config.ENABLE_PRICE_ANCHORS", True):
            self.assertIsNone(_anchors("pb", 2.0, 40.0, self.bands()))                      # 换了指标：不是扣非市盈率
            self.assertIsNone(_anchors("pe_ttm_deducted", -5.0, 40.0, self.bands()))        # 扣非为负
            self.assertIsNone(_anchors("pe_ttm_deducted", 20.0, None, self.bands()))        # 没有收盘价
            self.assertIsNone(_anchors("pe_ttm_deducted", 20.0, 40.0, {"p20": 20.0, "p50": None, "p80": 38.0}))

    def test_the_detail_carries_the_key_and_the_model_never_sees_prices(self):
        stock = stock_from_code("603738.SH")
        from app.fundamental_review import facts as fund_facts

        payload = build_detail(stock, anchor_snapshot(), today=date(2026, 10, 27))
        self.assertIn("anchors", payload["valuation"])
        self.assertIsNone(payload["valuation"]["anchors"])                                    # 默认关闭
        with patch("app.config.ENABLE_PRICE_ANCHORS", True):
            on = build_detail(stock, anchor_snapshot(), today=date(2026, 10, 27))
        self.assertEqual(on["valuation"]["anchors"]["eps_ttm_deducted"], 2.0)
        self.assertEqual(on["valuation"]["anchors"]["mid"]["price"], round(on["valuation"]["history"]["p50"] * 2.0, 2))
        facts, _d = fund_facts.build(on, {})
        anchor_prices = {on["valuation"]["anchors"][name]["price"] for name in ("low", "mid", "high")}
        self.assertFalse(any("anchor" in fact.id for fact in facts))                            # 体检的事实表里没有锚点，模型看不到
        self.assertFalse(any(fact.value in anchor_prices or str(fact.text) in {str(p) for p in anchor_prices} for fact in facts))


class FundamentalCopyTest(unittest.TestCase):
    def test_counter_comes_after_though(self):
        text = write_text({
            "speak": "市净率",
            "history_label": "近 5 年",
            "pctile_5y": 0.55,
            "value_text": "5.21",
            "gap_text": "只比同行高 11%",
            "support": "成长也偏弱",
            "counter": "利润处在低位，市盈率 149 倍失真，负债率 19%，现金流好于利润",
        })
        head, tail = text.split("不过", 1)
        self.assertIn("55% 分位", head)
        self.assertIn("成长也偏弱", head)
        self.assertNotIn("0.55", text)
        self.assertIn("利润处在低位", tail)
        self.assertNotIn("成长", tail)
        self.assertLessEqual(len(text), 90)


class FundamentalAssembleTest(unittest.TestCase):
    def test_payload_separates_pledge_name_and_pe_basis(self):
        stock = stock_from_code("603738", "SH")
        snapshot = {
            "stock_name": "泰晶科技",
            "price_date": "2026-10-27",
            "list_date": "20170101",
            "report_end": "20260630",
            "industry": {
                "code": "850823.SI",
                "name": "被动元件",
                "peer_count": 10,
                "peers_excluded": 2,
                "status": "ok",
            },
            "metric": "pb",
            "metric_text": "市净率",
            "metric_reason": "利润处在低位，市盈率失真",
            "value": 5.21,
            "distorted": True,
            "pe_reference": 149,
            "cash_beats_profit": True,
            "history": [
                {"date": "2024-01-05", "value": 4.2},
                {"date": "2026-10-27", "value": 5.21},
            ],
            "peers": [
                {"ts_code": "603738.SH", "name": "603738", "value": 5.21, "is_self": True},
                {"ts_code": "920981.BJ", "name": "晶赛科技", "value": 4.7, "is_self": False},
            ],
            "quality": {
                "profitability": 0.028,
                "growth": 0.01,
                "solvency": 0.19,
                "cash": 0.15,
                "payout": 0.3,
            },
            "quality_notes": {"cash": {
                "metric_text": "经营现金流/营收",
                "note_text": "利润偏低，已改用经营现金流/营收",
                "as_percent": True,
            }},
            "peer_quality": {
                "profitability": [0.08, 0.12],
                "growth": [0.2, 0.3],
                "solvency": [0.4, 0.5],
                "cash": [0.05, 0.08],
                "payout": [0.2, 0.4],
            },
            "ocf_negative_years": 0,
            "quotes": {"pe_ttm": 118.68, "pb": 5.21, "ps_ttm": 9.31},
            "table": {
                "pe_ttm": {"value": 149, "peer_median": 30.4, "label": "市盈率（TTM，扣非）"},
                "pb": {"value": 5.21, "peer_median": 4.71},
                "ps_ttm": {"value": 9.31, "peer_median": 4.29},
            },
            "holder_events": [
                {
                    "type": "pledge",
                    "title": "股权质押",
                    "impact": "none",
                    "detail_text": "控股股东所持股份 80% 已质押",
                    "float_ratio": None,
                    "risk_hit": True,
                    "counts": False,
                },
                {
                    "type": "holder_count",
                    "title": "股东户数",
                    "impact": "none",
                    "detail_text": "较上期 +91%，截至 06-30",
                },
            ],
            "trade_dates": [],
        }
        payload = build_detail(stock, snapshot, today=date(2026, 10, 27))
        self.assertEqual(payload["stock_name"], "泰晶科技")
        self.assertEqual(payload["meta"]["industry"]["name"], "被动元件")
        self.assertEqual(payload["meta"]["industry"]["peers_excluded"], 2)
        self.assertEqual(payload["meta"]["report_period"], "2026H1")
        self_row = next(row for row in payload["valuation"]["peers"] if row["is_self"])
        self.assertEqual(self_row["name"], "泰晶科技")
        holders = next(cell for cell in payload["cells"] if cell["key"] == "holders")
        self.assertEqual(holders["level"], "quiet")
        self.assertEqual(payload["position_input"]["risk_hit"], True)
        self.assertIsInstance(payload["position_input"]["risk_hit"], bool)
        self.assertEqual(payload["valuation"]["metric_text"], "市净率")
        self.assertEqual(payload["valuation"]["metric_reason"], "利润处在低位，市盈率失真")
        pledge = next(item for item in payload["holder_events"] if item["type"] == "pledge")
        self.assertEqual(pledge["impact"], "none")
        self.assertIsNone(pledge["float_ratio"])
        pe = next(row for row in payload["table"]["rows"] if row["key"] == "pe_ttm")
        pb = next(row for row in payload["table"]["rows"] if row["key"] == "pb")
        self.assertEqual(pe["label"], "市盈率（TTM，扣非）")
        self.assertNotEqual(pe["peer_median"], pb["peer_median"])
        cash = next(item for item in payload["quality"]["items"] if item["key"] == "cash")
        payout = next(item for item in payload["quality"]["items"] if item["key"] == "payout")
        self.assertEqual(cash["metric_text"], "经营现金流/营收")
        self.assertIsNotNone(cash["peer_pctile"])
        self.assertIsNotNone(payout["peer_pctile"])
        self.assertIn("不过", payload["verdict"]["text"])
        self.assertIn("% 分位", payload["verdict"]["text"])
        self.assertNotIn("chart_alt_text", payload["valuation"])
        valuation = next(cell for cell in payload["cells"] if cell["key"] == "valuation")
        self.assertIn("利润处在低位，市盈率失真", valuation["note_text"])
        # 完整结果给综合判断和 AI 体检读；对外响应只留和 headline 同一套的四个字段，中间量不对外
        self.assertEqual((payload["verdict"]["source"], payload["verdict"]["lean"] in {"bullish", "bearish", "neutral"}), ("rule", True))
        self.assertIn("inputs", payload)
        shown = public(payload)
        self.assertEqual(set(shown["verdict"]), {"source", "status", "status_text", "text"})
        self.assertNotIn("inputs", shown)
        self.assertIn("cells", shown)
        if (payload.get("inputs") or {}).get("business"):
            self.assertEqual(shown["business"]["main_business"], payload["inputs"]["business"]["main_business"])
        self.assertIsNone(public({"verdict": None, "meta": {}})["verdict"])


if __name__ == "__main__":
    unittest.main()
