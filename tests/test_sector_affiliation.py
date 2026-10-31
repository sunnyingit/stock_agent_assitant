import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.sector_detail import affiliation as aff


def _days(count: int = 80) -> list[str]:
    out, cursor = [], date(2026, 6, 1)
    while len(out) < count:
        if cursor.weekday() < 5:
            out.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return out


def _wave(dates: list[str], period: int, amp: float = 0.01, shift: int = 0) -> dict:
    """确定性的锯齿波，不同周期之间几乎不相关。"""
    return {day: amp * (((index + shift) % period) - (period - 1) / 2) / period * 2 for index, day in enumerate(dates)}


def _mix(parts: list[tuple[dict, float]], dates: list[str]) -> dict:
    return {day: sum(weight * series[day] for series, weight in parts) for day in dates}


class NameMatchTest(unittest.TestCase):
    def test_the_specific_word_decides_not_the_broad_one(self):
        self.assertTrue(aff.name_match("存储半导体", "存储芯片"))
        self.assertFalse(aff.name_match("存储半导体", "第三代半导体"))   # 只共有"半导体"
        self.assertFalse(aff.name_match("计量智能终端", "智能电网"))   # 只共有"智能"
        self.assertTrue(aff.name_match("半导体", "半导体"))            # 两边都只有大类词，比原名
        self.assertFalse(aff.name_match("半导体", "存储芯片"))

    def test_totals_do_not_count_as_product_lines(self):
        lines = [("高端制造", 57.0), ("存储半导体", 26.0), ("计量智能终端", 16.0), ("其他业务", 1.0), ("产品", 100.0)]
        found = aff.business_share(lines, "存储芯片")
        self.assertAlmostEqual(found["share"], 0.26 / 1.0, places=2)
        self.assertEqual(found["items"], ["存储半导体"])
        self.assertEqual(aff.business_share([("产品", 10.0)], "存储芯片")["share"], None)
        self.assertEqual([row["name"] for row in aff.business_lines(lines)], ["高端制造", "存储半导体", "计量智能终端"])

    def test_official_industry_gets_the_classification_share(self):
        official = aff.official_business({"share": 0.0, "items": []})
        self.assertEqual(official["share"], aff.OFFICIAL_SHARE)
        self.assertEqual(official["basis"], "申万按主营收入归类")
        self.assertEqual(aff.official_business({"share": 0.7, "items": ["x"]})["share"], 0.7)

    def test_market_tags_are_not_concepts(self):
        self.assertFalse(aff.usable_concept("融资融券", 1500))
        self.assertFalse(aff.usable_concept("中证500成份股", 500))
        self.assertFalse(aff.usable_concept("存储芯片", 3))          # 太小
        self.assertFalse(aff.usable_concept("存储芯片", 2000))       # 太大
        self.assertTrue(aff.usable_concept("存储芯片", 120))


class JudgeTest(unittest.TestCase):
    def cand(self, share=None, corr=None, top=None, member=True):
        return {"member": member, "business": {"share": share}, "linkage": {"corr60": corr}, "position": {"top_pct": top}}

    def test_belongs_needs_business_or_linkage_plus_standing(self):
        self.assertEqual(aff.judge(self.cand(share=0.26)), (True, "business"))
        self.assertEqual(aff.judge(self.cand(share=0.05, corr=0.8, top=0.1)), (True, "linkage"))
        self.assertEqual(aff.judge(self.cand(share=0.0, corr=0.9, top=0.5)), (False, "联动强但不是核心成员"))
        self.assertEqual(aff.judge(self.cand(share=0.0, corr=0.3, top=0.1)), (False, "仅挂名"))
        self.assertEqual(aff.judge(self.cand(share=0.9, member=False)), (False, "不是该板块成分"))
        self.assertEqual(aff.judge(self.cand(corr=0.9)), (False, "联动强但不是核心成员"))   # 没有排名：不能靠联动算属于

    def test_position_and_rank(self):
        caps = {"A": 5.0, "B": 9.0, "C": 7.0, "D": 1.0}
        self.assertEqual(aff.rank_by_value("A", ["A", "B", "C", "D"], caps), (3, 4))
        self.assertEqual(aff.rank_by_value("Z", ["A", "B", "C", "D"], caps), (None, None))
        self.assertEqual(aff.rank_by_value("A", ["A", "B"], caps), (None, None))   # 成分里有市值的不到 3 只
        self.assertEqual(aff.position(10, 100), {"rank": 10, "total": 100, "top_pct": 0.1})


class LinkTest(unittest.TestCase):
    def test_correlation_beta_and_capture(self):
        dates = _days()
        sector = _wave(dates, 7)
        stock = {day: value * 1.5 for day, value in sector.items()}
        out = aff.link(stock, sector, dates)
        self.assertAlmostEqual(out["corr60"], 1.0, places=2)
        self.assertAlmostEqual(out["beta60"], 1.5, places=2)
        self.assertAlmostEqual(out["up_capture"], 1.5, places=2)
        self.assertEqual(out["days"], 60)

    def test_too_few_days_gives_nothing(self):
        dates = _days(30)
        out = aff.link(_wave(dates, 7), _wave(dates, 7), dates)
        self.assertIsNone(out["corr60"])


class ChooseTest(unittest.TestCase):
    def setUp(self):
        self.dates = _days()
        self.a = _wave(self.dates, 7)
        self.b = _wave(self.dates, 11, shift=3)
        self.twin = {day: value * 1.02 for day, value in self.a.items()}
        self.stock = _mix([(self.a, 0.6), (self.b, 0.5)], self.dates)

    def cand(self, code, returns, score, members=()):
        return {"code": code, "name": code, "belongs": True, "score": score, "returns": returns, "members": set(members)}

    def test_twin_is_merged_and_a_different_sector_becomes_secondary(self):
        picked = aff.choose([self.cand("A", self.a, 0.9), self.cand("TWIN", self.twin, 0.8), self.cand("B", self.b, 0.5)], self.dates, self.stock)
        self.assertEqual(picked["primary"]["code"], "A")
        self.assertEqual(picked["secondary"]["code"], "B")
        self.assertEqual([row["code"] for row in picked["merged"]], ["TWIN"])

    def test_overlapping_members_are_merged_even_if_the_index_differs(self):
        picked = aff.choose([self.cand("A", self.a, 0.9, "wxyz"), self.cand("B", self.b, 0.5, "wxyq")], self.dates, self.stock)
        self.assertIsNone(picked["secondary"])
        self.assertEqual(picked["merged"][0]["code"], "B")

    def test_secondary_must_add_explanatory_power(self):
        noise = _wave(self.dates, 13, shift=5)
        only_a = {day: value for day, value in self.a.items()}
        picked = aff.choose([self.cand("A", self.a, 0.9), self.cand("N", noise, 0.7)], self.dates, only_a)
        self.assertIsNone(picked["secondary"])            # 噪声板块没有增量，不凑数

    def test_nothing_belongs_means_no_primary(self):
        loose = self.cand("A", self.a, 0.9)
        loose["belongs"] = False
        self.assertEqual(aff.choose([loose], self.dates, self.stock), {"primary": None, "secondary": None, "merged": []})

    def test_the_higher_score_is_primary_even_if_it_comes_second(self):
        picked = aff.choose([self.cand("B", self.b, 0.4), self.cand("A", self.a, 0.9)], self.dates, self.stock)
        self.assertEqual(picked["primary"]["code"], "A")


class AttributeTest(unittest.TestCase):
    def setUp(self):
        self.dates = _days()
        self.a = _wave(self.dates, 7)
        self.b = _wave(self.dates, 11, shift=3)

    def test_two_drivers_split_the_explained_part(self):
        stock = _mix([(self.a, 1.0), (self.b, 1.0)], self.dates)
        out = aff.attribute(stock, self.a, self.b, self.dates)
        self.assertEqual(out["type"], "dual")
        self.assertGreater(out["r2"], 0.95)
        self.assertAlmostEqual(out["primary_share"] + out["secondary_share"], out["r2"], places=3)    # 沙普利分解：两项相加等于合计
        self.assertAlmostEqual(out["weights"]["primary"] + out["weights"]["secondary"], 1.0, places=2)

    def test_one_dominant_driver_is_single(self):
        stock = _mix([(self.a, 1.0), (self.b, 0.05)], self.dates)
        out = aff.attribute(stock, self.a, self.b, self.dates)
        self.assertEqual(out["type"], "single")
        self.assertGreater(out["weights"]["primary"], 0.9)

    def test_unrelated_stock_is_independent(self):
        stock = _wave(self.dates, 17, shift=2)
        out = aff.attribute(stock, self.a, self.b, self.dates)
        self.assertEqual(out["type"], "independent")

    def test_without_a_secondary_the_primary_carries_everything(self):
        out = aff.attribute({day: value for day, value in self.a.items()}, self.a, None, self.dates)
        self.assertEqual(out["type"], "single")
        self.assertEqual(out["weights"], {"primary": 1.0, "secondary": 0.0})
        self.assertIsNone(out["secondary_share"])

    def test_short_history_is_unknown(self):
        dates = _days(20)
        out = aff.attribute(_wave(dates, 7), _wave(dates, 7), None, dates)
        self.assertEqual(out["type"], "unknown")


if __name__ == "__main__":
    unittest.main()
