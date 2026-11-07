import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class NewsExtractSwitchTest(unittest.TestCase):
    def test_server_can_turn_model_extraction_off(self):
        from app.services import news_detail

        with patch.dict(os.environ, {"NEWS_EXTRACT": "0"}), patch.object(news_detail.config, "LLM_API_KEY", "k"):
            self.assertIsNone(news_detail._writer(None, True))
        with patch.dict(os.environ, {"NEWS_EXTRACT": "1"}), patch.object(news_detail.config, "LLM_API_KEY", ""):
            self.assertIsNone(news_detail._writer(None, True))     # 没有钥匙本来就不调
        sentinel = object()
        with patch.dict(os.environ, {"NEWS_EXTRACT": "0"}):
            self.assertIs(news_detail._writer(sentinel, True), sentinel)   # 显式传入的模型（测试）不受影响


if __name__ == "__main__":
    unittest.main()
