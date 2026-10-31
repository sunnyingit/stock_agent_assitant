import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.fundamental import wan_to_yi


class FundamentalUnitTest(unittest.TestCase):
    def test_market_value_uses_wan(self):
        self.assertEqual(wan_to_yi(168758515.97), 16875.85)
        self.assertIsNone(wan_to_yi(None))


if __name__ == "__main__":
    unittest.main()
