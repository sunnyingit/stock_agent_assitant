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
        self.assertEqual(out, {"source": "model", "status": "support", "status_text": "偏多", "text": "资金整体在净流入。"})

    def test_none_when_the_model_did_not_write_it(self):
        self.assertIsNone(headline(_review("template")))
        self.assertIsNone(headline(None))
        self.assertIsNone(headline({"items": []}))

    def test_status_text_uses_the_shared_vocabulary(self):
        for status, text in (("support", "偏多"), ("warn", "偏空"), ("neutral", "不明")):
            review = {"items": [{"key": "q10", "source": "model", "status": status, "status_text": "随便", "answer": "x"}]}
            self.assertEqual(headline(review)["status_text"], text)
        self.assertIsNone(headline({"items": [{"key": "q10", "source": "model", "status": "missing", "answer": "x"}]}))


if __name__ == "__main__":
    unittest.main()
