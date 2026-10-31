import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config
from app.tools import cache
from app.tools.http import get_session


class HttpSessionTest(unittest.TestCase):
    def test_retry_and_timeout_come_from_config(self):
        session = get_session()
        retry = session.get_adapter("https://").max_retries
        self.assertEqual(retry.total, config.HTTP_RETRIES)
        self.assertEqual(retry.backoff_factor, config.HTTP_BACKOFF)
        self.assertEqual(config.HTTP_TIMEOUT[0], 3.05)
        self.assertIn(503, retry.status_forcelist)


class CacheTest(unittest.TestCase):
    def setUp(self):
        cache.clear()

    def test_set_and_get(self):
        cache.set("k", {"n": 1}, 10)
        self.assertEqual(cache.get("k"), {"n": 1})

    def test_expires(self):
        cache.set("k", "v", 0.01)
        time.sleep(0.02)
        self.assertIsNone(cache.get("k"))


if __name__ == "__main__":
    unittest.main()
