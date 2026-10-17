"""Offline video-frame checks for the Lv Bu flow; never contacts the phone."""
import unittest
from pathlib import Path
from types import SimpleNamespace

import cv2

from tasks import GameTasks
from game_auto_v1_17 import RecoveryBot
from lubu_tasks import army_capacity
from vision import TemplateVision


ROOT = Path(__file__).parent
FIXTURES = ROOT / "runtime/debug/lubu_video/fixtures"


class LubuVideoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tasks = GameTasks.__new__(GameTasks)
        cls.tasks.bot = SimpleNamespace(vision=TemplateVision(ROOT))

    def frame(self, second):
        result = cv2.imread(str(FIXTURES / f"{second:04}.png"))
        self.assertIsNotNone(result)
        return result

    def test_rally_list_excludes_inactive_buttons_and_wrong_activity(self):
        frame = self.frame(33)
        self.assertTrue(self.tasks._lubu_match(frame, "lubu_war"))
        candidates = self.tasks._lubu_candidates(frame)
        self.assertEqual(len(candidates), 1)
        self.assertAlmostEqual(candidates[0][1], 1158, delta=10)

    def test_video_queue_counts(self):
        expected = {3:(0,6), 75:(6,6), 1050:(4,6), 1076:(4,6)}
        for second, counts in expected.items():
            with self.subTest(second=second):
                self.assertEqual(self.tasks._lubu_count(self.frame(second)), counts)

    def test_video_ratio_and_expired_activity(self):
        ratios, _ = self.tasks._lubu_ratios(self.frame(12))
        self.assertEqual(ratios, (10,10,80))
        self.assertTrue(self.tasks._lubu_match(self.frame(1076), "lubu_cooldown"))
        self.assertFalse(self.tasks._lubu_match(self.frame(1050), "lubu_cooldown"))

    def test_formation_page_anchors_across_video_variants(self):
        for second in (6,14,36):
            with self.subTest(second=second):
                frame = self.frame(second)
                self.assertTrue(self.tasks._lubu_match(frame,"lubu_march_controls"))
                if second != 14:  # the click animation covers the depart label
                    self.assertTrue(self.tasks._lubu_match(frame,"lubu_depart"))
                equipment = self.tasks._lubu_match(frame,"lubu_equipment") or self.tasks._lubu_match(frame,"chest_equipment_blue")
                self.assertIsNotNone(equipment)

    def test_typed_join_button_is_found_in_target_card(self):
        candidates = self.tasks._lubu_candidates(self.frame(24))
        self.assertEqual(len(candidates), 2)
        self.assertGreater(candidates[0][1], 450)

    def test_join_timer_and_army_capacity(self):
        frame = self.frame(24)
        x,y = self.tasks._lubu_candidates(frame)[0]
        self.assertEqual(self.tasks._lubu_join_seconds(frame,x,y),39)
        self.assertEqual(army_capacity("队容量：巧4510/巧154510"),(4510,154510))
        self.assertIsNone(army_capacity("队容量未知"))

    def test_lubu_recovery_classifies_its_own_pages(self):
        bot = RecoveryBot.__new__(RecoveryBot)
        bot.vision = self.tasks.bot.vision
        expected = {3:"lubu_target", 5:"lubu_start", 6:"lubu_march",
                    12:"lubu_ratio", 18:"lubu_war", 36:"lubu_march"}
        for second,page in expected.items():
            with self.subTest(second=second):
                self.assertEqual(bot.current_page(self.frame(second))[0],page)


if __name__ == "__main__":
    unittest.main()
