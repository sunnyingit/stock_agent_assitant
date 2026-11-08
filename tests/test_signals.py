import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import signals


def bars(closes, last_high=None):
    rows = [{"trade_date": f"2026{(i // 28) + 1:02d}{(i % 28) + 1:02d}", "close": c, "high": c} for i, c in enumerate(closes)]
    if last_high is not None:
        rows[-1]["high"] = last_high
    return rows


class SignalsTest(unittest.TestCase):
    def test_board_limits(self):
        self.assertEqual(signals.limit_pct("600036.SH"), 0.10)
        self.assertEqual(signals.limit_pct("300750.SZ"), 0.20)
        self.assertEqual(signals.limit_pct("688981.SH"), 0.20)
        self.assertEqual(signals.limit_pct("830799.BJ"), 0.30)
        self.assertEqual(signals.limit_pct("600001.SH", "*ST 某某"), 0.05)

    def test_limit_up_down_and_broken_board(self):
        flat = [10.0] * 5
        self.assertEqual(signals.compute("600001.SH", "甲", bars(flat + [11.0]))["limit"]["kind"], "limit_up")
        self.assertEqual(signals.compute("600001.SH", "甲", bars(flat + [9.0]))["limit"]["kind"], "limit_down")
        # 盘中摸到 11.00，收在 10.6：炸板
        self.assertEqual(signals.compute("600001.SH", "甲", bars(flat + [10.6], last_high=11.0))["limit"]["kind"], "limit_open")
        self.assertIsNone(signals.compute("600001.SH", "甲", bars(flat + [10.5], last_high=10.6))["limit"])
        # 创业板涨 10% 不算涨停
        self.assertIsNone(signals.compute("300001.SZ", "乙", bars(flat + [11.0]))["limit"])

    def test_extremes_prefer_the_longer_window_and_need_enough_history(self):
        rising = [10 + i * 0.1 for i in range(70)]
        out = signals.compute("600001.SH", "甲", bars(rising))
        self.assertEqual((out["extreme"]["kind"], out["extreme"]["window_days"]), ("high", 60))
        short = signals.compute("600001.SH", "甲", bars([10.0] * 30 + [11.0, 12.0]))
        self.assertEqual(short["extreme"]["window_days"], 20)
        falling = [20 - i * 0.1 for i in range(25)]
        self.assertEqual(signals.compute("600001.SH", "甲", bars(falling))["extreme"]["kind"], "low")
        self.assertIsNone(signals.compute("600001.SH", "甲", bars([10.0, 10.1, 10.2]))["extreme"])
        self.assertEqual(signals.compute("600001.SH", "甲", bars([10.0]))["trade_date"], "")


if __name__ == "__main__":
    unittest.main()
