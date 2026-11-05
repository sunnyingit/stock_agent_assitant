import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import announcements as A


class ClassifyTest(unittest.TestCase):
    def check(self, title, importance, topic=None, hint=None):
        got = A.classify(title)
        self.assertEqual(got["importance"], importance, title)
        if topic is not None:
            self.assertEqual(got["topic"], topic, title)
        if hint is not None:
            self.assertEqual(got["direction_hint"], hint, title)

    def test_p0_risk_and_governance(self):
        self.check("关于收到中国证监会立案告知书的公告", "P0", "立案调查", "利空")
        self.check("关于收到上海证券交易所监管函的公告", "P0", "监管函")
        self.check("关于收到深圳证券交易所问询函的公告", "P0", "交易所问询")
        self.check("2025年度业绩预亏的公告", "P0", "业绩预亏", "利空")
        self.check("关于公司股票被实施退市风险警示的公告", "P0")
        self.check("关于筹划重大资产重组的停牌公告", "P0")
        self.check("关于公司股票复牌的公告", "P0", "复牌", "中性")
        self.check("关于公司实际控制人变更的公告", "P0", "控制权变更", "中性")

    def test_p1_results_capital_and_business(self):
        self.check("2025年度业绩预增公告", "P1", "业绩预增", "利好")
        self.check("2025年年度报告", "P1", "定期报告")
        self.check("关于以集中竞价交易方式回购股份的公告", "P1", "股份回购", "利好")
        self.check("关于控股股东增持公司股份的公告", "P1", "股东增持", "利好")
        self.check("股东减持股份计划公告", "P1", "股东减持", "利空")
        self.check("关于签订重大合同的公告", "P1", "重大合同", "利好")
        self.check("关于2025年度利润分配预案的公告", "P1", "分红送转", "利好")
        self.check("关于涉及诉讼的公告", "P1", "诉讼", "利空")
        self.check("2025年度业绩预减公告", "P1", "业绩预减", "利空")

    def test_p2_routine(self):
        self.check("第五届董事会第十二次会议决议公告", "P2", "董事会决议")
        self.check("关于召开2025年第一次临时股东大会的通知", "P2", "股东大会")
        self.check("独立董事关于相关事项的独立意见", "P2", "独立董事意见")
        self.check("关于日常关联交易预计的公告", "P2", "日常关联交易")

    def test_unknown_titles_are_routine_not_alarming(self):
        self.check("关于变更公司注册地址的公告", "P2", "", "中性")

    def test_p0_wins_over_p1_when_both_match(self):
        self.check("关于收到立案告知书暨控股股东减持的公告", "P0")

    def test_terminated_buyback_is_not_good_news(self):
        self.check("关于终止回购股份的公告", "P1", "股份回购", "中性")

    def test_whitespace_does_not_hide_a_keyword(self):
        self.check("关于收到 监管函 的公告", "P0")


class EnrichTest(unittest.TestCase):
    RAW = [
        {"source": "exchange", "source_name": "交易所公告", "title": "关于回购股份的公告", "url": "u1", "time": "2026-11-05 18:30:00"},
        {"source": "exchange", "source_name": "交易所公告", "title": "关于回购股份的公告", "url": "u1", "time": "2026-11-05 18:30:00"},
        {"source": "exchange", "source_name": "交易所公告", "title": "董事会决议公告", "url": "u2", "time": "2026-11-05 17:00:00"},
        {"source": "exchange", "title": "", "url": "u3", "time": "2026-11-05 19:00:00"},
    ]

    def test_dedupes_sorts_and_keeps_stable_ids(self):
        items = A.enrich("600036.SH", self.RAW)
        self.assertEqual([i["title"] for i in items], ["董事会决议公告", "关于回购股份的公告"])
        self.assertEqual(items[1]["item_id"], A.enrich("600036.SH", self.RAW[:1])[0]["item_id"])
        self.assertEqual((items[1]["source_grade"], items[1]["importance"]), ("A", "P1"))

    def test_id_differs_when_the_publish_time_differs(self):
        a = A.item_id("x", "u", "标题", "2026-11-05 10:00:00")
        b = A.item_id("x", "u", "标题", "2026-11-05 11:00:00")
        self.assertNotEqual(a, b)


if __name__ == "__main__":
    unittest.main()
