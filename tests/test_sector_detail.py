import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.sector_detail import build_detail, failed_payload
from app.services.sector_detail.rules import DISCLAIMER, confirm, raw_stage
from app.tools.stock import stock_from_code


def _dates(count: int) -> list[str]:
    days = []
    cursor = date(2026, 8, 3)
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return days


def _days(count: int = 25, late: int = 3, rank: int = 7, breadth: float = 0.62, quiet_rank: int = 20, quiet_breadth: float = 0.40, limit_up: int = 1) -> list[dict]:
    dates = _dates(count)
    rows = []
    for index, day in enumerate(dates):
        heated = index >= count - late
        rows.append({
            "date": day,
            "stock": 0.01 if index % 2 == 0 else -0.004,
            "sector": 0.01 if index % 2 == 0 else -0.004,
            "index": 0.0004,
            "breadth": breadth if heated else quiet_breadth,
            "limit_up": limit_up,
            "max_streak": 1,
            "broken_rate": 0.0,
            "rank": rank if heated else quiet_rank,
            "rank_total": 31,
        })
    return rows


def _members() -> list[dict]:
    return [
        {"ts_code": "603738.SH", "name": "泰晶科技", "ret_20d": 0.20},
        {"ts_code": "000001.SZ", "name": "甲", "ret_20d": 0.05},
        {"ts_code": "000002.SZ", "name": "乙", "ret_20d": 0.04},
        {"ts_code": "000003.SZ", "name": "丙", "ret_20d": 0.03},
        {"ts_code": "000004.SZ", "name": "丁", "ret_20d": 0.01},
    ]


def _sector(code: str, name: str, corr: float, days: list[dict] | None = None, **extra) -> dict:
    row = {
        "code": code,
        "name": name,
        "type": "industry",
        "corr_20d": corr,
        "days": days if days is not None else _days(),
        "members": _members(),
        "flow_rate": 0.02,
        "turnover_share": {"value": 0.08, "vs_last_week": "up", "pctile_1y": 0.55},
        "round_median": extra.pop("round_median", 2),
        "catalyst": None,
    }
    row.update(extra)
    return row


def _snapshot(sectors: list[dict], **extra) -> dict:
    as_of = sectors[0]["days"][-1]["date"]
    payload = {
        "as_of": as_of,
        "listed_days": 400,
        "intraday": False,
        "sectors": sectors,
        "source_text": "测试快照",
    }
    payload.update(extra)
    return payload


class SectorRuleTest(unittest.TestCase):
    def test_ebb_beats_diverge(self):
        day = {
            "rank_pct": 0.80,
            "breadth": 0.40,
            "limit_up": 0,
            "sector_5d": -0.04,
            "index_5d": 0.01,
        }
        prior = {"rank_pct": 0.20, "breadth": 0.70}
        self.assertEqual(raw_stage(day, prior, None), "ebb")

    def test_diverge_beats_main(self):
        day = {"rank_pct": 0.20, "breadth": 0.60, "limit_up": 1, "sector_5d": 0.02, "index_5d": 0.01}
        prior = {"rank_pct": 0.20, "breadth": 0.80}
        self.assertEqual(raw_stage(day, prior, 0.5), "diverge")

    def test_three_days_confirm_two_days_do_not(self):
        stages, runs = confirm(["none"] * 5 + ["main", "main"], 3)
        self.assertEqual(stages[-1], "none")
        stages, runs = confirm(["none"] * 5 + ["main", "main", "main"], 3)
        self.assertEqual(stages[-1], "main")
        self.assertEqual(runs[-1], 3)


class SectorDetailTest(unittest.TestCase):
    def _stock(self):
        return stock_from_code("603738", "SH", name="泰晶科技")

    def _main_snapshot(self):
        main = _sector("850823.SI", "被动元件", 0.82)
        other = _sector(
            "801080.SI",
            "电子",
            0.70,
            days=_days(rank=20, breadth=0.30, late=25),
            round_median=None,
        )
        return _snapshot([main, other])

    def test_main_stage_leader_is_level_3_after_one_warn(self):
        snapshot = self._main_snapshot()
        payload = build_detail(self._stock(), snapshot=snapshot, today=date.fromisoformat(snapshot["as_of"]))
        self.assertEqual(payload["meta"]["state"], "ok")
        self.assertEqual(payload["meta"]["rule_version"], "sector-2")
        self.assertTrue(payload["meta"]["viewing_main"])
        self.assertFalse(payload["sectors"]["independent"])
        self.assertEqual(payload["sectors"]["main"]["code"], "850823.SI")
        self.assertEqual(payload["stage"]["current"], "main")
        self.assertEqual(payload["stage"]["confirmed_days"], 3)
        self.assertEqual(
            [item["key"] for item in payload["stage"]["signals"]],
            ["breadth", "up_ratio_today", "limit_up", "max_streak", "broken_rate"],
        )
        self.assertIn("value_text", payload["stage"]["signals"][0])
        self.assertIn("change_text", payload["stage"]["signals"][0])
        self.assertEqual(payload["role"]["value"], "leader")
        self.assertEqual(payload["verdict"]["lean"], "bullish")
        self.assertEqual(payload["verdict"]["level"], 3)
        self.assertEqual(payload["verdict"]["certainty"], "medium")
        self.assertIn("本轮持续天数超过同类行情中位数", payload["verdict"]["certainty_reasons"])
        self.assertEqual(payload["verdict"]["tag_text"], "板块主升，本股领涨")
        self.assertEqual(payload["position_hint"], {"climax": False, "overdue": True})
        self.assertEqual(payload["disclaimer_text"], DISCLAIMER)
        self.assertNotIn("disclaimer", payload["sustain"])
        self.assertEqual(list(payload["sustain"]), ["items"])
        text = payload["verdict"]["ai_text"]
        self.assertIn("不过", text)
        self.assertLessEqual(len(text), 90)
        for word in ("可以关注", "买入", "卖出", "目标价"):
            self.assertNotIn(word, text)
        self.assertEqual(payload["watch_next"][0]["condition_id"], "sector_enter_climax")
        self.assertFalse(payload["watch_next"][0]["subscribed"])

    def test_other_sector_keeps_the_main_verdict(self):
        snapshot = self._main_snapshot()
        payload = build_detail(
            self._stock(),
            sector_code="801080.SI",
            snapshot=snapshot,
            today=date.fromisoformat(snapshot["as_of"]),
        )
        self.assertFalse(payload["meta"]["viewing_main"])
        self.assertEqual(payload["verdict"]["lean"], "bullish")
        self.assertEqual(payload["verdict"]["level"], 3)
        self.assertEqual(payload["verdict"]["tag_text"], "板块主升，本股领涨")
        self.assertIn("电子", payload["strength"]["chart_alt_text"])
        self.assertIn("沪深300", payload["strength"]["chart_alt_text"])
        self.assertEqual(payload["stage"]["current"], "none")
        viewing = [item for item in payload["sectors"]["candidates"] if item["is_viewing"]]
        self.assertEqual(viewing[0]["code"], "801080.SI")
        self.assertFalse(viewing[0]["is_main"])

    def test_low_correlation_is_independent_and_level_1(self):
        sectors = [
            _sector("850823.SI", "被动元件", 0.40),
            _sector("801080.SI", "电子", 0.30, days=_days()),
        ]
        payload = build_detail(
            self._stock(),
            snapshot=_snapshot(sectors),
            today=date.fromisoformat(_snapshot(sectors)["as_of"]),
        )
        self.assertTrue(payload["sectors"]["independent"])
        self.assertIsNone(payload["sectors"]["main"])
        self.assertFalse(payload["meta"]["viewing_main"])
        self.assertEqual(payload["verdict"]["level"], 1)
        self.assertEqual(payload["verdict"]["certainty"], "low")
        self.assertEqual(payload["verdict"]["tag_text"], "独立走势，板块结论不适用")
        self.assertIn("不过", payload["verdict"]["ai_text"])

    def test_short_history_is_insufficient(self):
        payload = build_detail(
            self._stock(),
            snapshot={"listed_days": 10, "sectors": [], "intraday": False},
            today=date(2026, 10, 27),
        )
        self.assertEqual(payload["meta"]["state"], "insufficient")
        self.assertIsNone(payload["verdict"])

    def test_hk_does_not_use_this_peer_set(self):
        stock = stock_from_code("00700", "HK", name="腾讯控股")
        payload = build_detail(stock, today=date(2026, 10, 27))
        self.assertEqual(payload["meta"]["state"], "insufficient")
        self.assertIn("港股", payload["meta"]["status_text"])

    def test_failed_payload_is_not_a_direction(self):
        payload = failed_payload(self._stock(), "上游超时", today=date(2026, 10, 27))
        self.assertEqual(payload["meta"]["state"], "failed")
        self.assertEqual(payload["meta"]["error"], "上游超时")
        self.assertIsNone(payload["verdict"])

    def test_intraday_status_uses_the_clock(self):
        snapshot = self._main_snapshot()
        snapshot["intraday"] = True
        snapshot["clock"] = "10:40"
        snapshot["status_date"] = "2026-10-27"
        payload = build_detail(self._stock(), snapshot=snapshot, today=date(2026, 10, 27))
        self.assertEqual(payload["meta"]["state"], "intraday")
        self.assertEqual(payload["meta"]["status_text"], "盘中 · 10-27 10:40")

    def test_close_correlation_prefers_industry(self):
        concept = _sector("885843.TI", "华为海思概念股", 0.91, type="concept")
        industry = _sector("850823.SI", "被动元件", 0.89)
        snapshot = _snapshot([concept, industry])
        payload = build_detail(self._stock(), snapshot=snapshot, today=date.fromisoformat(snapshot["as_of"]))
        self.assertEqual(payload["sectors"]["main"]["code"], "850823.SI")
        self.assertEqual(payload["sectors"]["main"]["name"], "被动元件")

    def test_wider_gap_keeps_the_concept(self):
        concept = _sector("885843.TI", "华为海思概念股", 0.96, type="concept")
        industry = _sector("850823.SI", "被动元件", 0.89)
        snapshot = _snapshot([concept, industry])
        payload = build_detail(self._stock(), snapshot=snapshot, today=date.fromisoformat(snapshot["as_of"]))
        self.assertEqual(payload["sectors"]["main"]["code"], "885843.TI")

    def test_weak_stage_names_sector_and_stock_gaps(self):
        days = _days(count=25, late=25, rank=29, breadth=0.05, quiet_rank=29, quiet_breadth=0.05, limit_up=0)
        for row in days:
            row["stock"] = -0.015
            row["sector"] = -0.008
            row["index"] = -0.004
            row["broken_rate"] = None
            row["up_ratio_today"] = 0.90
        members = [
            {"ts_code": "000001.SZ", "name": "甲", "ret_20d": 0.05},
            {"ts_code": "000002.SZ", "name": "乙", "ret_20d": 0.04},
            {"ts_code": "000003.SZ", "name": "丙", "ret_20d": 0.03},
            {"ts_code": "000004.SZ", "name": "丁", "ret_20d": 0.01},
            {"ts_code": "603738.SH", "name": "泰晶科技", "ret_20d": -0.30},
            {"ts_code": "600000.SH", "name": "ST惠伦", "ret_20d": 0.50},
        ]
        sector = _sector("850823.SI", "被动元件", 0.82, days=days, members=members, round_median=5)
        snapshot = _snapshot([sector])
        payload = build_detail(self._stock(), snapshot=snapshot, today=date.fromisoformat(snapshot["as_of"]))
        self.assertEqual(payload["stage"]["current"], "weak")
        self.assertEqual(payload["verdict"]["lean"], "bearish")
        self.assertEqual(payload["verdict"]["level"], 1)
        self.assertEqual(payload["verdict"]["certainty"], "low")
        self.assertEqual(payload["verdict"]["tag_text"], "板块弱势，本股跟风")
        self.assertEqual(payload["role"]["value"], "follower")
        text = payload["verdict"]["ai_text"]
        self.assertIn("可能接近尾声", payload["verdict"]["ai_text"])
        self.assertNotIn("本股近 20 日弱于大盘", payload["verdict"]["ai_text"])
        self.assertFalse(payload["position_hint"]["overdue"])
        self.assertEqual(payload["role"]["member_count"], 5)
        self.assertEqual(payload["role"]["top"][0]["role"], "resilient")
        self.assertEqual(payload["role"]["top"][0]["role_text"], "抗跌")
        self.assertNotIn("ST惠伦", [item["name"] for item in payload["role"]["top"]])
        self.assertEqual(payload["verdict"]["certainty_reasons"], ["弱势阶段，把握上限为低"])
        self.assertFalse(any("可能接近尾声" in item for item in payload["verdict"]["certainty_reasons"]))
        signals = {item["key"]: item for item in payload["stage"]["signals"]}
        self.assertEqual(signals["breadth"]["level_text"], "普遍在 20 日线下")
        self.assertIsNone(signals["up_ratio_today"]["change_text"])
        self.assertEqual(signals["broken_rate"]["value"], None)
        self.assertEqual(signals["broken_rate"]["level_text"], "—")
        self.assertEqual(signals["broken_rate"]["value_text"], "—")
        self.assertEqual(signals["up_ratio_today"]["level_text"], "当日上涨")
        self.assertNotEqual(signals["breadth"]["level_text"], "普涨")
        self.assertEqual(payload["watch_next"][0]["text"], "排名连续 3 天回到后 30% 之外")
        self.assertEqual(payload["watch_next"][0]["then_text"], "弱势结束，转为不明")
        self.assertEqual(payload["watch_next"][1]["text"], "排名连续 3 天回到前 30%")
        self.assertEqual(payload["watch_next"][1]["then_text"], "转为偏多")
        self.assertIn("%", payload["strength"]["chart_alt_text"])

    def test_weak_duration_states_the_median_when_not_longer(self):
        days = _days(count=25, late=25, rank=29, breadth=0.05, quiet_rank=29, quiet_breadth=0.05, limit_up=0)
        for row in days:
            row["stock"] = -0.015
            row["sector"] = -0.008
            row["index"] = -0.004
        days[-1]["sector"] = 0.034
        days[-1]["up_ratio_today"] = 1.0
        sector = _sector("850823.SI", "被动元件", 0.82, days=days, round_median=40)
        snapshot = _snapshot([sector])
        payload = build_detail(self._stock(), snapshot=snapshot, today=date.fromisoformat(snapshot["as_of"]))
        text = payload["verdict"]["ai_text"]
        duration = next(item for item in payload["sustain"]["items"] if item["key"] == "duration")
        self.assertEqual(duration["status"], "support")
        self.assertIn("弱势可能还会继续", duration["text"])
        self.assertIn("今天板块全部上涨，反弹 3.4%，要看能不能持续", text)
        self.assertNotIn("还没超过", text)
        self.assertNotIn("可能接近尾声", text)
        self.assertEqual(payload["verdict"]["certainty_reasons"], ["弱势阶段，把握上限为低"])
        self.assertFalse(payload["position_hint"]["overdue"])


if __name__ == "__main__":
    unittest.main()
