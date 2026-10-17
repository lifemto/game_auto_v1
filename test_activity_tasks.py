"""Activity regression tests. No phone actions or network calls."""
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import cv2
import numpy as np

from activity_tasks import read_ratios
from game_auto_v1_17 import RecoveryBot
from tasks import GameTasks
from vision import Match, TemplateVision


ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "runtime/debug/chest_activity"


def hit(name, center=(360,800)):
    x, y = center
    return Match(name, .99, (x-10,y-10,x+10,y+10), center)


class ActivityFlowTests(unittest.TestCase):
    def setUp(self):
        self.fr = np.zeros((1600,726,3), np.uint8)
        self.bot = SimpleNamespace(
            frame=Mock(return_value=self.fr), vision=SimpleNamespace(match=Mock(return_value=None)),
            actions=Mock(), log=Mock(), fail_snapshot=Mock(),
            wait_template=Mock(return_value=hit("page")),
            wait_template_any=Mock(return_value=hit("page")),
            wait_template_gone=Mock(return_value=True),
            wait_main=Mock(return_value=True), ensure_main=Mock(return_value=True),
            is_main=Mock(return_value=True), runtime_options={"chest_count": 2},
        )
        self.tasks = GameTasks.__new__(GameTasks)
        self.tasks.bot = self.bot
        sleep = patch("activity_tasks.time.sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

    def test_ratios_reduce_excess_then_fill_to_exact_goal(self):
        values = [66,4,30]
        history = []
        self.bot.vision.match.return_value = hit("page_chest_ratio")
        self.tasks._activity_tap = Mock(return_value=True)
        def tap(x, y, *args):
            minus, plus = [86,312,539], [187,414,640]
            if x in minus:
                values[minus.index(x)] -= 1
            elif x in plus:
                self.assertLess(sum(values), 100)
                values[plus.index(x)] += 1
            else:
                self.fail(f"Unexpected button {x}")
            history.append(tuple(values))
            return True
        self.bot.actions.tap_frame.side_effect = tap
        with patch("activity_tasks.read_ratios", side_effect=lambda fr: tuple(values)):
            self.assertTrue(self.tasks._chest_set_ratios())
        self.assertEqual(values, [5,5,90])
        self.assertTrue(all(sum(v) <= 100 and min(v) >= 0 for v in history))
        self.tasks._activity_tap.assert_called_once()

    def test_unchanging_ratio_does_not_confirm_or_depart(self):
        self.bot.vision.match.return_value = hit("page_chest_ratio")
        self.tasks._activity_tap = Mock()
        with patch("activity_tasks.read_ratios", return_value=(66,4,30)):
            self.assertFalse(self.tasks._chest_set_ratios())
        self.tasks._activity_tap.assert_not_called()

    def test_ratio_requires_second_matching_read(self):
        self.tasks._activity_tap = Mock()
        with patch("activity_tasks.read_ratios", side_effect=[(5,5,90), (6,4,90)]):
            self.assertFalse(self.tasks._chest_set_ratios())
        self.tasks._activity_tap.assert_not_called()

    def test_strength_comes_from_boost_description(self):
        for faction in ("蜀", "吴", "魏"):
            with self.subTest(faction=faction), patch("activity_tasks.read_text", return_value={
                "text": f"{faction} 国 阵 营 伤 害 大 幅 提 升"
            }):
                self.assertEqual(self.tasks._chest_strengthened_faction(self.fr), faction)

    def test_unknown_boost_is_not_assumed_from_enemy_title(self):
        with patch("activity_tasks.read_text", return_value={"text": "群雄混战魏国篇"}):
            self.assertIsNone(self.tasks._chest_strengthened_faction(self.fr))

    def test_wrong_selected_faction_stops_before_equipment(self):
        self.tasks._tap_rel = Mock(return_value=True)
        self.tasks._chest_selected_faction = Mock(return_value="吴")
        self.tasks._activity_tap = Mock()
        self.assertFalse(self.tasks._chest_prepare_formation("蜀"))
        self.tasks._activity_tap.assert_not_called()

    def prepare_battles(self, remaining=5):
        self.tasks._open_chest_activity = Mock(return_value=True)
        self.tasks._chest_strengthened_faction = Mock(return_value="蜀")
        self.tasks._chest_remaining_attacks = Mock(return_value=remaining)
        self.tasks._chest_prepare_formation = Mock(return_value=True)
        self.tasks._activity_tap = Mock(return_value=True)
        self.tasks._chest_wait_for_next = Mock(return_value=True)

    def test_two_attacks_prepare_each_time_and_wait_between(self):
        self.prepare_battles()
        self.assertTrue(self.tasks.chest_battles())
        self.assertEqual(self.tasks._chest_prepare_formation.call_count, 2)
        self.tasks._chest_wait_for_next.assert_called_once()
        names = [call.args[0] for call in self.tasks._activity_tap.call_args_list]
        self.assertEqual(names, ["chest_go","chest_attack","chest_depart"]*2)

    def test_only_one_remaining_does_not_start_second_attack(self):
        self.prepare_battles(remaining=1)
        self.assertTrue(self.tasks.chest_battles())
        self.assertEqual(self.tasks._chest_prepare_formation.call_count, 1)
        self.tasks._chest_wait_for_next.assert_not_called()

    def test_zero_remaining_never_attacks(self):
        self.prepare_battles(remaining=0)
        self.assertTrue(self.tasks.chest_battles())
        self.tasks._chest_prepare_formation.assert_not_called()
        self.assertEqual([c.args[0] for c in self.tasks._activity_tap.call_args_list], ["chest_go"])

    def test_bad_formation_never_departs(self):
        self.prepare_battles()
        self.tasks._chest_prepare_formation.return_value = False
        self.assertFalse(self.tasks.chest_battles())
        self.assertNotIn("chest_depart", [c.args[0] for c in self.tasks._activity_tap.call_args_list])

    def test_wait_is_at_least_sixty_seconds_and_uses_short_sleeps(self):
        now = [100.0]
        self.tasks.handle_main_extras = Mock(return_value=True)
        def sleep(seconds):
            self.assertLessEqual(seconds, 5)
            now[0] += seconds
        with patch("activity_tasks.time.monotonic", side_effect=lambda: now[0]), \
             patch("activity_tasks.time.sleep", side_effect=sleep):
            self.assertTrue(self.tasks._chest_wait_for_next(100))
        self.assertGreaterEqual(now[0], 160)

    def setup_limits(self, values, checks):
        rows = [{"text": f"{used}/{maximum}", "words": [{"y": y}]} for used, maximum, y in values]
        positions = [(82, y-1) for _, _, y in values]
        def candidates(frame, name, *args, **kwargs):
            if name == "auto_join_checked":
                return list(checks)
            return [(.99, x, y) for x, y in positions
                    if not any(abs(m[1]-x)<5 and abs(m[2]-y)<5 for m in checks)]
        self.tasks._multi_template_candidates = Mock(side_effect=candidates)
        def tap(x, y, *args):
            checks[:] = [m for m in checks if m[1:] != (x,y)]
            return True
        self.bot.actions.tap_frame.side_effect = tap
        return {"lines": rows}

    def test_rally_unchecks_50_of_50_and_preserves_19_of_50(self):
        checks = [(.99,82,791), (.99,82,862)]
        data = self.setup_limits([(50,50,792), (19,50,863)], checks)
        with patch("activity_tasks.read_text", return_value=data):
            self.assertTrue(self.tasks._auto_rally_reward_limits())
        self.assertEqual(checks, [(.99,82,862)])
        self.bot.actions.tap_frame.assert_called_once()

    def test_rally_does_not_recheck_already_unchecked_full_type(self):
        checks = [(.99,82,862)]
        data = self.setup_limits([(50,50,792), (19,50,863)], checks)
        with patch("activity_tasks.read_text", return_value=data):
            self.assertTrue(self.tasks._auto_rally_reward_limits())
        self.bot.actions.tap_frame.assert_not_called()

    def test_rally_does_not_start_with_unreadable_counts(self):
        with patch("activity_tasks.read_text", return_value={"lines": []}):
            self.assertFalse(self.tasks._auto_rally_reward_limits())
        self.bot.actions.tap_frame.assert_not_called()

    def test_congratulations_prefers_all_and_supports_single_thumb(self):
        for multiple in (True, False):
            with self.subTest(multiple=multiple):
                state = {"open": True}
                self.bot.actions.reset_mock()
                self.bot.vision.match.side_effect = lambda fr, name: hit(name) if state["open"] and (
                    name in {"page_congrats", "congrats_thumb"} or (name == "congrats_all" and multiple)
                ) else None
                self.bot.actions.tap_match.side_effect = lambda *a: state.update(open=False) or True
                self.assertTrue(self.tasks._handle_congratulations(already_open=True))
                self.bot.actions.tap_match.assert_called_once()
                expected = "congrats_all" if multiple else "congrats_thumb"
                self.assertEqual(self.bot.actions.tap_match.call_args.args[0].name, expected)


class ActivityScreenshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.vision = TemplateVision(ROOT)
        cls.tasks = GameTasks.__new__(GameTasks)
        cls.tasks.bot = SimpleNamespace(vision=cls.vision)

    def read(self, name):
        path = FIXTURES / f"{name}.png"
        if not path.exists():
            self.skipTest("Local user screenshot not present")
        return cv2.imread(str(path))

    def test_windows_ocr_reads_actual_ratios_and_remaining_count(self):
        self.assertEqual(read_ratios(self.read("ratio")), (66,4,30))
        self.assertEqual(self.tasks._chest_remaining_attacks(self.read("target")), 5)

    def test_strength_is_shu_while_activity_enemy_is_wei(self):
        self.assertEqual(self.tasks._chest_strengthened_faction(self.read("clash")), "蜀")

    def test_both_formation_colors_and_recovery_pages(self):
        bot = RecoveryBot.__new__(RecoveryBot)
        bot.vision = self.vision
        for name, expected in (("wu","chest_march"), ("shu","chest_march"),
                               ("ratio","chest_ratio"), ("target","chest_target"),
                               ("congrats","congrats"), ("clash","activity")):
            with self.subTest(name=name):
                self.assertEqual(bot.current_page(self.read(name))[0], expected)
        self.assertEqual(self.tasks._chest_selected_faction(self.read("wu")), "吴")
        self.assertEqual(self.tasks._chest_selected_faction(self.read("shu")), "蜀")

    def test_activities_share_tab_without_enemy_suffix(self):
        for name in ("invasion", "clash"):
            self.assertIsNotNone(self.vision.match(self.read(name), "chest_activity_tab"))

    def test_congratulations_assets_are_distinct_from_green_help_hand(self):
        fr = self.read("thumb")
        m = self.vision.match(fr, "congrats_entry")
        self.assertIsNotNone(m)
        self.assertGreater(m.center[0], 500)
        popup = self.read("congrats")
        self.assertIsNotNone(self.vision.match(popup, "page_congrats"))
        self.assertIsNotNone(self.vision.match(popup, "congrats_all"))
        self.assertIsNotNone(self.vision.match(popup, "congrats_thumb"))


if __name__ == "__main__":
    unittest.main()
