"""行情镜头：开场方向跟驱动板走，弱势归属+个股抗跌不再直接偏空。"""

from __future__ import annotations

import unittest

from app.services.sector_detail.rules import pick_tape_lens, tape_verdict


def _body(lean: str, stage: str, role: str, stock_vs_sector: float | None = 0.0, level: int = 1):
    return {
        "lean": lean,
        "level": level,
        "certainty": "low" if lean == "bearish" else "medium",
        "certainty_reasons": [f"stage={stage}"],
        "stage": stage,
        "role": role,
        "gaps": {"stock_vs_sector": stock_vs_sector, "stock_vs_index": 0.1, "sector_vs_index": -0.05},
        "tag_text": f"板块{stage}，本股{role}",
        "counter_text": "",
        "sustain_items": [],
    }


class TapeVerdictTests(unittest.TestCase):
    def test_lens_follows_higher_driver_weight(self):
        # 归属主板块阶段不明、次板块主升且驱动占比更高 → 镜头取次板块偏多（非阶段对立）
        primary = _body("unclear", "none", "core")
        secondary = _body("bullish", "main", "leader", 0.08)
        self.assertEqual(pick_tape_lens(primary, secondary, {"primary": 0.3, "secondary": 0.7}), "secondary")
        out = tape_verdict(primary, secondary, {"primary": 0.3, "secondary": 0.7})
        self.assertEqual(out["lens_role"], "secondary")
        self.assertEqual(out["lean"], "bullish")
        self.assertEqual(out["vs_belong"], "背离")

    def test_opposite_stages_force_neutral(self):
        primary = _body("bullish", "main", "core")
        secondary = _body("bearish", "weak", "follower")
        out = tape_verdict(primary, secondary, {"primary": 0.6, "secondary": 0.4})
        self.assertEqual(out["lean"], "neutral")
        self.assertTrue(out["divergent"])
        self.assertEqual(out["vs_belong"], "对立")

    def test_weak_board_strong_stock_not_bearish(self):
        primary = _body("bearish", "weak", "resilient", stock_vs_sector=0.20)
        out = tape_verdict(primary, None, None)
        self.assertEqual(out["lean"], "neutral")
        self.assertEqual(out["vs_belong"], "背离")
        self.assertIn("不构成支撑", out["certainty_reasons"][0])

    def test_weak_follower_still_bearish(self):
        primary = _body("bearish", "weak", "follower", stock_vs_sector=-0.02)
        out = tape_verdict(primary, None, None)
        self.assertEqual(out["lean"], "bearish")
        self.assertEqual(out["vs_belong"], "同向")


if __name__ == "__main__":
    unittest.main()
