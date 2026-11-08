import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import fundamental_reply, industry_chain as chain
from app.tools import cache
from app.tools.stock import Stock

STOCK = Stock(ts_code="002050.SZ", code="002050", name="三花智控", quote_id="0.002050", em_code="002050")

CORE = {
    "ssbk": [
        {"BOARD_NAME": "家电零部件Ⅲ", "BOARD_RANK": 3, "IS_PRECISE": None},
        {"BOARD_NAME": "家用电器", "BOARD_RANK": 1, "IS_PRECISE": "0"},
        {"BOARD_NAME": "家电零部件Ⅱ", "BOARD_RANK": 2, "IS_PRECISE": None},
        {"BOARD_NAME": "浙江板块", "BOARD_RANK": 4, "IS_PRECISE": "0"},
        {"BOARD_NAME": "机器人执行器", "BOARD_RANK": 23, "IS_PRECISE": "1"},
        {"BOARD_NAME": "汽车热管理", "BOARD_RANK": 27, "IS_PRECISE": 1},
        {"BOARD_NAME": "深股通", "BOARD_RANK": 15, "IS_PRECISE": "0"},
    ],
    "hxtc": [
        {"KEY_CLASSIF": "经营范围", "KEYWORD": "经营范围", "MAINPOINT_CONTENT": "家用电器研发，客户包括任何人。"},
        {"KEY_CLASSIF": "主营业务", "KEYWORD": "制冷空调电器零部件业务", "MAINPOINT_CONTENT": "公司提供阀、换热器、泵等产品。有关产品广泛应用于家用空调、小家电设备、商用空调与制冷等领域。公司能够快速响应客户需求变化，不断推出新产品。"},
        {"KEY_CLASSIF": "主营业务", "KEYWORD": "汽车零部件业务", "MAINPOINT_CONTENT": "公司是全球领先的热管理系统零部件供应商，主要原材料为铜和铝，向上游采购比例较高。"},
        {"KEY_CLASSIF": "核心竞争力", "KEYWORD": "客户", "MAINPOINT_CONTENT": "公司与开利、博西家电、大金、格力等主要客户合作，获得了广泛认可。在供应商管理环节建立了质量制度。"},
        {"KEY_CLASSIF": "行业背景", "KEYWORD": "制冷空调电器零部件行业", "MAINPOINT_CONTENT": "2026年上半年家用空调总产销同比下降6.2%。"},
    ],
}
BIZ = {"zygcfx": [
    {"REPORT_DATE": "2025-12-31 00:00:00", "MAINOP_TYPE": "2", "ITEM_NAME": "旧一期的产品", "MBI_RATIO": 1, "GROSS_RPOFIT_RATIO": 0.3},
    {"REPORT_DATE": "2026-06-30 00:00:00", "MAINOP_TYPE": "2", "ITEM_NAME": "汽车零部件", "MBI_RATIO": 0.381961, "GROSS_RPOFIT_RATIO": 0.275573},
    {"REPORT_DATE": "2026-06-30 00:00:00", "MAINOP_TYPE": "2", "ITEM_NAME": "制冷空调电器零部件", "MBI_RATIO": 0.618039, "GROSS_RPOFIT_RATIO": 0.28374},
    {"REPORT_DATE": "2026-06-30 00:00:00", "MAINOP_TYPE": "2", "ITEM_NAME": "合计", "MBI_RATIO": 1, "GROSS_RPOFIT_RATIO": 0.28},
    {"REPORT_DATE": "2026-06-30 00:00:00", "MAINOP_TYPE": "3", "ITEM_NAME": "国内销售", "MBI_RATIO": 0.582695},
    {"REPORT_DATE": "2026-06-30 00:00:00", "MAINOP_TYPE": "3", "ITEM_NAME": "国外销售", "MBI_RATIO": 0.417305},
]}
IND = {"czxbj": [{"CORRE_SECURITY_NAME": "三花智控"}, {"CORRE_SECURITY_NAME": "海立股份"}, {"CORRE_SECURITY_NAME": "海立B股"}, {"CORRE_SECURITY_NAME": "行业中值"}, {"CORRE_SECURITY_NAME": "禾盛新材"}]}


class FormatTest(unittest.TestCase):
    def setUp(self):
        self.out = chain.format_chain(CORE, BIZ, IND, "三花智控")
        self.facts = {f["id"]: f["text"] for f in self.out["facts"]}

    def test_industry_levels_stop_at_the_region_board_and_concepts_are_the_precise_ones_only(self):
        self.assertEqual(self.out["chain"]["industry"], ["家用电器", "家电零部件Ⅱ", "家电零部件Ⅲ"])
        self.assertEqual(self.out["chain"]["concepts"], ["机器人执行器", "汽车热管理"])
        self.assertIn("家用电器 › 家电零部件Ⅱ › 家电零部件Ⅲ", self.facts["fd.chain.position"])

    def test_products_come_from_the_latest_period_sorted_without_totals(self):
        self.assertEqual(self.facts["fd.chain.products"], "主营按产品（2026-06-30）：制冷空调电器零部件 占 61.8%、毛利率 28.4%；汽车零部件 占 38.2%、毛利率 27.6%")
        self.assertEqual(self.facts["fd.chain.regions"], "销售区域（2026-06-30）：国内销售 占 58.3%；国外销售 占 41.7%")

    def test_downstream_and_upstream_are_the_companys_own_sentences_and_each_sentence_goes_one_way(self):
        down = [v for k, v in self.facts.items() if k.startswith("fd.chain.downstream")]
        up = [v for k, v in self.facts.items() if k.startswith("fd.chain.upstream")]
        self.assertEqual(len(down), 2)
        self.assertTrue(down[0].endswith("有关产品广泛应用于家用空调、小家电设备、商用空调与制冷等领域"))
        self.assertIn("开利、博西家电、大金、格力等主要客户合作", down[1])
        self.assertEqual(len(up), 1)
        self.assertIn("主要原材料为铜和铝", up[0])
        everything = " ".join(down + up)
        self.assertNotIn("快速响应客户需求", everything)   # not a statement about who the customers are
        self.assertNotIn("供应商管理", everything)         # a quality system, not a supplier
        self.assertNotIn("客户包括任何人", everything)     # the business scope is not read

    def test_industry_background_peers_and_the_caveat(self):
        self.assertIn("下降6.2%", self.facts["fd.chain.demand.1"])
        self.assertEqual(self.facts["fd.chain.peers"], "同业可比公司：海立股份、禾盛新材")
        self.assertTrue(self.out["text"].endswith("同业只是同行，不是上下游。"))
        self.assertEqual(self.out["unavailable"], [])

    def test_what_the_company_did_not_write_is_said_not_filled_in(self):
        out = chain.format_chain({"ssbk": [], "hxtc": [{"KEY_CLASSIF": "主营业务", "KEYWORD": "x", "MAINPOINT_CONTENT": "公司做一些事情，没有提到别的。"}]}, {}, {}, "某公司")
        self.assertEqual(out["unavailable"], ["没有取到所属行业", "没有取到主营按产品的构成", "公司披露里没有写明下游客户或应用", "公司披露里没有写明上游原材料或供应商"])
        self.assertFalse([k for k in (f["id"] for f in out["facts"]) if "upstream" in k or "downstream" in k])


class BuildTest(unittest.TestCase):
    def setUp(self):
        cache.clear()

    def test_secucode_and_hk(self):
        self.assertEqual(chain.secucode(STOCK), "SZ002050")
        hk = Stock(ts_code="00700.HK", code="00700", name="腾讯", quote_id="", em_code="")
        doc = chain.build(hk)
        self.assertEqual((doc["facts"], doc["unavailable"]), ([], ["没有取到"]))

    def test_one_page_failing_does_not_lose_the_others_and_pages_are_cached(self):
        calls = []

        def fake(url, params=None, headers=None):
            calls.append(url.rsplit("/", 2)[-2])
            if "IndustryAnalysis" in url:
                raise OSError("连不上")
            return CORE if "CoreConception" in url else BIZ

        with patch.object(chain, "get_json", fake):
            doc = chain.build(STOCK)
            chain.build(STOCK)
        self.assertEqual(doc["part"], "chain")
        self.assertIn("fd.chain.position", [f["id"] for f in doc["facts"]])
        self.assertNotIn("fd.chain.peers", [f["id"] for f in doc["facts"]])
        self.assertEqual(calls.count("CoreConception"), 1)  # second build came from the cache

    def test_it_is_one_of_the_fundamental_reply_parts(self):
        self.assertIn("chain", fundamental_reply.PARTS)
        with patch.object(chain, "build", return_value={"part": "chain", "facts": []}) as call:
            self.assertEqual(fundamental_reply.build(STOCK, "chain")["part"], "chain")
        call.assert_called_once()


if __name__ == "__main__":
    unittest.main()
