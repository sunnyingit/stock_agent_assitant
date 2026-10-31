import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agents.display import cap_points, headline


class DisplayTest(unittest.TestCase):
    def test_keeps_one_opposing_point_inside_three(self):
        picked = cap_points([
            {"lean": "偏空", "title": "甲"},
            {"lean": "偏空", "title": "乙"},
            {"lean": "偏空", "title": "丙"},
            {"lean": "偏多", "title": "丁"},
        ])
        self.assertEqual([item["title"] for item in picked], ["甲", "乙", "丁"])

    def test_headline_uses_the_requested_label(self):
        text = headline("偏空", "新闻面偏多：收盘价被报道", "资金面")
        self.assertEqual(text, "资金面偏空：收盘价被报道")
        self.assertLessEqual(len(text), 25)
