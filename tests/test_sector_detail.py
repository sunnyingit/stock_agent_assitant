import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.sector_detail import build_detail, failed_payload
from app.services.sector_detail.detail import public
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
        "type": extra.pop("type", "industry"),
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


def _snapshot(sectors: list[dict], primary: str | None = None, secondary: str | None = None, driver: dict | None = None, **extra) -> dict:
    as_of = sectors[0]["days"][-1]["date"]
    candidates = [
        {"code": item["code"], "name": item["name"], "type": item["type"], "belongs": True, "basis": "business", "score": 0.8,
         "business": {"share": 0.5, "items": []}, "linkage": {"corr60": 0.8}, "position": {"rank": 3, "total": 40, "top_pct": 0.075},
         "role": "primary" if item["code"] == primary else "secondary" if item["code"] == secondary else None}
        for item in sectors
    ]
    for item, cand in zip(sectors, candidates):
        item["evidence"] = cand
    payload = {
        "as_of": as_of,
        "listed_days": 400,
        "intraday": False,
        "sectors": sectors,
        "affiliation": {
            "version": "sector-aff-1", "primary": primary, "secondary": secondary, "merged": [], "candidates": candidates,
            "driver": driver or {"type": "single", "weights": {"primary": 1.0, "secondary": 0.0}, "r2": 0.6, "primary_share": 0.6, "secondary_share": None, "days": 60},
            "stock_vs_index": 0.01,
        },
        "source_text": "测试快照",
    }
    payload.update(extra)
    return payload


def _dual(weights: dict | None = None) -> dict:
    weights = weights or {"primary": 0.6, "secondary": 0.4}
    return {"type": "dual", "weights": weights, "r2": 0.7, "primary_share": 0.42, "secondary_share": 0.28, "days": 60}


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

    def _build(self, snapshot):
        return build_detail(self._stock(), snapshot=snapshot, today=date.fromisoformat(snapshot["as_of"]))

    def _main(self):
        return _sector("850823.SI", "被动元件", 0.82)

    def test_single_driver_main_stage_leader_is_level_3_after_one_warn(self):
        snapshot = _snapshot([self._main()], primary="850823.SI")
        payload = self._build(snapshot)
        self.assertEqual(payload["meta"]["state"], "ok")
        self.assertEqual(payload["meta"]["rule_version"], "sector-3")
        self.assertFalse(payload["sectors"]["independent"])
        primary = payload["sectors"]["primary"]
        self.assertEqual(primary["code"], "850823.SI")
        self.assertEqual(primary["role_in_stock"], "primary")
        self.assertIsNone(payload["sectors"]["secondary"])
        self.assertEqual(primary["stage"]["current"], "main")
        self.assertEqual(primary["stage"]["confirmed_days"], 3)
        self.assertEqual(
            [item["key"] for item in primary["stage"]["signals"]],
            ["breadth", "up_ratio_today", "limit_up", "max_streak", "broken_rate"],
        )
        self.assertEqual(primary["role"]["value"], "leader")
        self.assertEqual(primary["tag_text"], "板块主升，本股领涨")
        self.assertEqual(primary["position_hint"], {"climax": False, "overdue": True})
        self.assertEqual(list(primary["sustain"]), ["items"])
        self.assertEqual(primary["watch_next"][0]["condition_id"], "sector_enter_climax")
        self.assertEqual(payload["disclaimer_text"], DISCLAIMER)
        verdict = payload["verdict"]
        self.assertEqual((verdict["lean"], verdict["level"], verdict["certainty"]), ("bullish", 3, "medium"))
        self.assertTrue(any("本轮持续天数超过同类行情中位数（已 3 天，中位数 2 天）" in item for item in verdict["certainty_reasons"]))
        self.assertEqual((verdict["source"], verdict["status"], verdict["status_text"]), ("rule", "support", "偏多"))
        self.assertIn("不过", verdict["text"])
        self.assertLessEqual(len(verdict["text"]), 90)
        for word in ("可以关注", "买入", "卖出", "目标价"):
            self.assertNotIn(word, verdict["text"])

    def test_public_keeps_only_the_four_verdict_fields(self):
        payload = self._build(_snapshot([self._main()], primary="850823.SI"))
        shown = public(payload)
        self.assertEqual(sorted(shown["verdict"]), ["source", "status", "status_text", "text"])
        self.assertNotIn("inputs", shown)
        self.assertIn("inputs", payload)
        self.assertNotIn("ai_text", payload["verdict"])
        self.assertNotIn("ai_status", payload["verdict"])

    def test_dual_driver_blends_by_weight(self):
        primary = self._main()
        secondary = _sector("885843.TI", "华为海思概念股", 0.8, type="concept")
        snapshot = _snapshot([primary, secondary], primary="850823.SI", secondary="885843.TI", driver=_dual())
        payload = self._build(snapshot)
        self.assertEqual(payload["sectors"]["driver"]["type"], "dual")
        self.assertEqual(payload["sectors"]["driver"]["weights"], {"primary": 0.6, "secondary": 0.4})
        self.assertEqual(payload["sectors"]["secondary"]["driver_weight"], 0.4)
        self.assertEqual(payload["sectors"]["secondary"]["role_text"], "次板块")
        self.assertFalse(payload["sectors"]["divergence"]["active"])
        self.assertEqual(payload["verdict"]["lean"], "bullish")
        self.assertIn("两者对本股走势的解释约 60% 和 40%", payload["verdict"]["text"])
        self.assertEqual(sorted(payload["inputs"]), ["primary", "secondary"])

    def test_opposite_stages_are_a_divergence_not_an_average(self):
        weak_days = _days(count=25, late=25, rank=29, breadth=0.05, quiet_rank=29, quiet_breadth=0.05, limit_up=0)
        for row in weak_days:
            row["stock"], row["sector"], row["index"] = -0.015, -0.008, -0.004
        primary = self._main()
        secondary = _sector("885843.TI", "华为海思概念股", 0.8, days=weak_days, type="concept", round_median=5)
        snapshot = _snapshot([primary, secondary], primary="850823.SI", secondary="885843.TI", driver=_dual({"primary": 0.5, "secondary": 0.5}))
        payload = self._build(snapshot)
        self.assertTrue(payload["sectors"]["divergence"]["active"])
        self.assertEqual(payload["sectors"]["divergence"]["stages"], {"primary": "main", "secondary": "weak"})
        self.assertEqual(payload["verdict"]["lean"], "neutral")
        self.assertEqual(payload["verdict"]["certainty"], "low")
        self.assertIn("两个板块阶段对立", payload["verdict"]["certainty_reasons"])
        self.assertIn("方向不取平均", payload["verdict"]["text"])

    def test_independent_driver_gives_relative_verdict(self):
        snapshot = _snapshot([self._main()], primary="850823.SI", driver={"type": "independent", "weights": {"primary": 1.0, "secondary": 0.0}, "r2": 0.1, "days": 60})
        payload = self._build(snapshot)
        self.assertTrue(payload["sectors"]["independent"])
        self.assertEqual(payload["verdict"]["level"], 1)
        self.assertEqual(payload["verdict"]["certainty"], "low")
        self.assertIn("独立走势，板块结论不适用", payload["verdict"]["text"])
        self.assertIn("不过", payload["verdict"]["text"])

    def test_no_sector_belongs_is_independent(self):
        snapshot = _snapshot([self._main()])
        snapshot["sectors"] = []
        snapshot["affiliation"]["candidates"][0].update({"belongs": False, "basis": "仅挂名", "role": None})
        snapshot["as_of"] = "2026-10-27"
        payload = self._build(snapshot)
        self.assertEqual(payload["meta"]["state"], "ok")
        self.assertTrue(payload["sectors"]["independent"])
        self.assertIsNone(payload["sectors"]["primary"])
        self.assertEqual(payload["sectors"]["candidates"][0]["basis"], "仅挂名")
        self.assertEqual(payload["verdict"]["level"], 1)

    def test_short_history_is_insufficient(self):
        payload = build_detail(
            self._stock(),
            snapshot={"listed_days": 10, "sectors": [], "affiliation": None, "intraday": False},
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
        snapshot = _snapshot([self._main()], primary="850823.SI")
        snapshot["intraday"] = True
        snapshot["clock"] = "10:40"
        snapshot["status_date"] = "2026-10-27"
        payload = build_detail(self._stock(), snapshot=snapshot, today=date(2026, 10, 27))
        self.assertEqual(payload["meta"]["state"], "intraday")
        self.assertEqual(payload["meta"]["status_text"], "盘中 · 10-27 10:40")

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
        payload = self._build(_snapshot([sector], primary="850823.SI"))
        block = payload["sectors"]["primary"]
        self.assertEqual(block["stage"]["current"], "weak")
        self.assertEqual(payload["verdict"]["lean"], "bearish")
        self.assertEqual(payload["verdict"]["level"], 1)
        self.assertEqual(payload["verdict"]["certainty"], "low")
        self.assertEqual(block["tag_text"], "板块弱势，本股跟风")
        self.assertEqual(block["role"]["value"], "follower")
        self.assertIn("可能接近尾声", payload["verdict"]["text"])
        self.assertNotIn("本股近 20 日弱于大盘", payload["verdict"]["text"])
        self.assertFalse(block["position_hint"]["overdue"])
        self.assertEqual(block["role"]["member_count"], 5)
        self.assertEqual(block["role"]["top"][0]["role"], "resilient")
        self.assertNotIn("ST惠伦", [item["name"] for item in block["role"]["top"]])
        self.assertEqual(payload["verdict"]["certainty_reasons"], ["弱势阶段，把握上限为低"])
        signals = {item["key"]: item for item in block["stage"]["signals"]}
        self.assertEqual(signals["breadth"]["level_text"], "普遍在 20 日线下")
        self.assertIsNone(signals["up_ratio_today"]["change_text"])
        self.assertEqual(signals["broken_rate"]["value_text"], "—")
        self.assertEqual(block["watch_next"][0]["text"], "排名连续 3 天回到后 30% 之外")
        self.assertEqual(block["watch_next"][1]["then_text"], "转为偏多")

    def test_weak_duration_states_the_median_when_not_longer(self):
        days = _days(count=25, late=25, rank=29, breadth=0.05, quiet_rank=29, quiet_breadth=0.05, limit_up=0)
        for row in days:
            row["stock"] = -0.015
            row["sector"] = -0.008
            row["index"] = -0.004
        days[-1]["sector"] = 0.034
        days[-1]["up_ratio_today"] = 1.0
        sector = _sector("850823.SI", "被动元件", 0.82, days=days, round_median=40)
        payload = self._build(_snapshot([sector], primary="850823.SI"))
        text = payload["verdict"]["text"]
        duration = next(item for item in payload["sectors"]["primary"]["sustain"]["items"] if item["key"] == "duration")
        self.assertEqual(duration["status"], "support")
        self.assertIn("弱势可能还会继续", duration["text"])
        self.assertIn("今天板块全部上涨，反弹 3.4%，要看能不能持续", text)
        self.assertNotIn("还没超过", text)
        self.assertEqual(payload["verdict"]["certainty_reasons"], ["弱势阶段，把握上限为低"])
        self.assertFalse(payload["sectors"]["primary"]["position_hint"]["overdue"])


if __name__ == "__main__":
    unittest.main()
