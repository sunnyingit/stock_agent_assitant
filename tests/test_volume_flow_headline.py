import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.volume_flow.headline import headline


def _review(source="model"):
    return {"items": [
        {"key": "q9", "source": "model", "status": "warn", "status_text": "需警惕", "answer": "最要紧的是顶背离。"},
        {"key": "q10", "source": source, "status": "support", "status_text": "偏正面", "answer": "资金整体在净流入。"},
    ]}


class HeadlineTest(unittest.TestCase):
    def test_model_summary_has_the_same_fields_as_verdict(self):
        out = headline(_review())
        self.assertEqual(out, {
            "source": "model", "status": "support", "status_text": "偏多", "text": "资金整体在净流入。", "facts": [],
        })

    def test_none_when_the_model_did_not_write_it(self):
        self.assertIsNone(headline(_review("template")))
        self.assertIsNone(headline(None))
        self.assertIsNone(headline({"items": []}))

    def test_template_summary_is_allowed_for_analyst_conclusion(self):
        out = headline(_review("template"), allow_template=True)
        self.assertEqual(out["source"], "rule")
        self.assertEqual(out["text"], "资金整体在净流入。")
        self.assertEqual(out["status_text"], "偏多")

    def test_status_text_uses_the_shared_vocabulary(self):
        for status, text in (("support", "偏多"), ("warn", "偏空"), ("neutral", "不明")):
            review = {"items": [{"key": "q10", "source": "model", "status": status, "status_text": "随便", "answer": "x"}]}
            self.assertEqual(headline(review)["status_text"], text)
        self.assertIsNone(headline({"items": [{"key": "q10", "source": "model", "status": "missing", "answer": "x"}]}))

    def test_cited_ids_are_copied_from_the_fact_sheet(self):
        review = _review()
        review["cited_fact_ids"] = ["fr.ret_20d", "fr.missing", "fr.big_net_20d"]
        review["facts"] = [
            {"id": "fr.ret_20d", "label": "近20日涨跌", "text": "-27.7%", "value": -0.277, "group": "价格"},
            {"id": "fr.big_net_20d", "label": "近20日大资金净额", "text": "净流出 12.88 亿元", "value": -1.288e9},
        ]
        facts = headline(review)["facts"]
        self.assertEqual([row["id"] for row in facts], ["fr.ret_20d", "fr.big_net_20d"])
        self.assertEqual(facts[0]["text"], "-27.7%")
        self.assertEqual(facts[0]["value"], -0.277)


if __name__ == "__main__":
    unittest.main()
