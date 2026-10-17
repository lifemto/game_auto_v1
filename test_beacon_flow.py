"""烽火台导航回归：无需 ADB、视频流或 PyAV。"""
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import cv2
import numpy as np

from tasks import GameTasks
from vision import Match, TemplateVision


ROOT = Path(__file__).resolve().parent


def hit(name, center=(285, 1530)):
    x, y = center
    return Match(name, .99, (x-25, y-30, x+25, y+30), center)


class BeaconFlowTests(unittest.TestCase):
    def setUp(self):
        self.clock = 0.0
        self.stage = "blue"
        self.frames = {name: np.zeros((1600, 726, 3), np.uint8)
                       for name in ("blue", "grey", "main", "unknown", "reward")}
        self.names = {id(frame): name for name, frame in self.frames.items()}
        self.bot = SimpleNamespace(
            frame=Mock(side_effect=lambda: self.frames[self.stage]),
            vision=SimpleNamespace(match=Mock(side_effect=self.match)),
            actions=SimpleNamespace(tap_match=Mock(return_value=True),
                                    tap_frame=Mock(return_value=True)),
            is_main=Mock(side_effect=lambda fr: self.names[id(fr)] == "main"),
            log=Mock(), fail_snapshot=Mock(), ensure_main=Mock(return_value=True),
            wait_template=Mock(return_value=hit("beacon_go_button")),
            wait_template_any=Mock(return_value=hit("page_beacon")),
            wait_main=Mock(return_value=True),
        )
        self.tasks = GameTasks.__new__(GameTasks)
        self.tasks.bot = self.bot
        for target, effect in (("tasks.time.sleep", self.sleep),
                               ("tasks.time.monotonic", lambda: self.clock)):
            patcher = patch(target, side_effect=effect)
            patcher.start()
            self.addCleanup(patcher.stop)

    def sleep(self, seconds):
        self.clock += seconds

    def match(self, frame, name):
        stage = self.names[id(frame)]
        visible = {
            "blue": {"page_beacon", "beacon_assist_blue"},
            "grey": {"page_beacon", "beacon_assist_grey"},
            "main": {"main_alliance"},
            "unknown": set(),
            "reward": {"reward_continue", "page_beacon"},
        }
        return hit(name) if name in visible[stage] else None

    def test_assist_returns_to_map_then_reenters_before_success(self):
        def tap(*args):
            self.stage = "main"
            return True

        def enter():
            self.stage = "grey"
            return True

        self.bot.actions.tap_match.side_effect = tap
        self.tasks._enter_beacon_from_main = Mock(side_effect=enter)
        self.assertTrue(self.tasks._beacon_assist_once())
        self.tasks._enter_beacon_from_main.assert_called_once()
        self.bot.actions.tap_match.assert_called_once()
        self.assertEqual(self.stage, "grey")

    def test_reentry_rechecks_blue_instead_of_assuming_finished(self):
        visits = []

        def tap(*args):
            self.stage = "main"
            return True

        def enter():
            visits.append(1)
            self.stage = "blue" if len(visits) == 1 else "grey"
            return True

        self.bot.actions.tap_match.side_effect = tap
        self.tasks._enter_beacon_from_main = Mock(side_effect=enter)
        self.assertTrue(self.tasks._beacon_assist_once())
        self.assertEqual(len(visits), 2)
        self.assertEqual(self.bot.actions.tap_match.call_count, 2)

    def test_loading_and_missing_frame_after_assist_are_retried(self):
        self.bot.frame.side_effect = [self.frames["blue"], None,
                                     self.frames["unknown"], self.frames["grey"]]
        self.assertTrue(self.tasks._beacon_assist_once())
        self.bot.actions.tap_match.assert_called_once()
        self.bot.fail_snapshot.assert_not_called()

    def test_unknown_page_is_not_success_or_a_navigation_target(self):
        def tap(*args):
            self.stage = "unknown"
            return True

        self.bot.actions.tap_match.side_effect = tap
        self.tasks._enter_beacon_from_main = Mock()
        self.assertFalse(self.tasks._beacon_assist_once())
        self.tasks._enter_beacon_from_main.assert_not_called()
        self.bot.fail_snapshot.assert_called_with("beacon_assist_page_missing")

    def test_map_overlay_is_not_treated_as_main(self):
        self.stage = "main"
        self.bot.current_page = Mock(return_value=("beacon_resource_popup", None))
        self.tasks._enter_beacon_from_main = Mock()
        self.assertIsNone(self.tasks._beacon_wait_page(after_assist=True))
        self.tasks._enter_beacon_from_main.assert_not_called()

    def test_failed_reentry_is_failure(self):
        def tap(*args):
            self.stage = "main"
            return True

        self.bot.actions.tap_match.side_effect = tap
        self.tasks._enter_beacon_from_main = Mock(return_value=False)
        self.assertFalse(self.tasks._beacon_assist_once())

    def test_reward_overlay_is_closed_before_rechecking_assist(self):
        def tap(match, *args):
            self.stage = "reward" if match.name == "beacon_assist_blue" else "grey"
            return True

        self.bot.actions.tap_match.side_effect = tap
        self.assertTrue(self.tasks._beacon_assist_once())
        self.assertEqual([call.args[0].name for call in
                          self.bot.actions.tap_match.call_args_list],
                         ["beacon_assist_blue", "reward_continue"])

    def test_fourth_click_must_still_confirm_beacon_page(self):
        def tap(*args):
            if self.bot.actions.tap_match.call_count == 4:
                self.stage = "unknown"
            return True

        self.bot.actions.tap_match.side_effect = tap
        self.assertFalse(self.tasks._beacon_assist_once())
        self.assertEqual(self.bot.actions.tap_match.call_count, 4)
        self.bot.fail_snapshot.assert_called_with("beacon_assist_page_missing")

    def test_blue_that_never_clears_stops_after_four_clicks(self):
        self.assertFalse(self.tasks._beacon_assist_once())
        self.assertEqual(self.bot.actions.tap_match.call_count, 4)
        self.bot.fail_snapshot.assert_called_with("beacon_assist_stuck")

    def test_failed_assist_click_saves_diagnostic(self):
        self.bot.actions.tap_match.return_value = False
        self.assertFalse(self.tasks._beacon_assist_once())
        self.bot.fail_snapshot.assert_called_with("beacon_assist_click_failed")

    def test_entering_from_beacon_does_not_exit_it(self):
        self.assertTrue(self.tasks._enter_beacon_from_main())
        self.bot.ensure_main.assert_not_called()
        self.bot.actions.tap_match.assert_not_called()

    def test_missing_entry_frame_fails_without_click(self):
        self.stage = "main"
        self.bot.frame.side_effect = [self.frames["main"], None, None, None, None]
        self.assertFalse(self.tasks._enter_beacon_from_main())
        self.bot.actions.tap_frame.assert_not_called()
        self.bot.fail_snapshot.assert_called_with("beacon_entry_frame_missing")

    def test_entry_missed_click_relocates_on_new_frame(self):
        self.stage = "main"
        self.bot.wait_template_any.side_effect = [None, hit("page_beacon")]
        self.assertTrue(self.tasks._enter_beacon_from_main())
        self.assertEqual(self.bot.actions.tap_frame.call_count, 2)
        first, second = self.bot.actions.tap_frame.call_args_list
        self.assertNotEqual(first.args[:2], second.args[:2])
        self.assertEqual(self.bot.ensure_main.call_count, 1)

    def test_entry_unknown_overlay_prevents_second_click(self):
        self.stage = "main"
        def tap(*args):
            self.stage = "unknown"
            return True
        self.bot.actions.tap_frame.side_effect = tap
        self.bot.wait_template_any.return_value = None
        self.assertFalse(self.tasks._enter_beacon_from_main())
        self.bot.actions.tap_frame.assert_called_once()
        self.bot.fail_snapshot.assert_called_with("beacon_entry_page_changed")

    def test_entry_prefers_stronger_current_template(self):
        self.stage = "main"
        original = self.match
        def match(frame, name):
            if name in ("main_beacon_entry", "main_beacon_entry_wechat"):
                m = hit(name, (60, 1257) if name == "main_beacon_entry" else (47, 1270))
                m.score = .787 if name == "main_beacon_entry" else .855
                return m
            return original(frame, name)
        self.bot.vision.match.side_effect = match
        self.assertTrue(self.tasks._enter_beacon_from_main())
        self.assertEqual(self.bot.actions.tap_frame.call_args.args[:2], (47, 1270))

    def test_event_selection_click_failure_does_not_continue(self):
        self.bot.actions.tap_frame.return_value = False
        self.assertFalse(self.tasks._beacon_select_event_and_go(
            dict(type="resource", x=200, y=600, score=.99)))
        self.bot.wait_template_any.assert_not_called()

    def test_event_go_click_failure_is_not_dispatch_success(self):
        self.bot.wait_template_any.return_value = hit("beacon_go_button", (200, 600))
        self.bot.actions.tap_match.return_value = False
        self.assertFalse(self.tasks._beacon_select_event_and_go(
            dict(type="resource", x=200, y=600, score=.99)))
        self.bot.fail_snapshot.assert_called_with("beacon_event_go_click_failed")

    def test_selected_resource_detail_uses_original_event_position(self):
        self.bot.wait_template.side_effect = [
            hit("beacon_resource_detail"), hit("beacon_go_button", (619, 880))]
        self.assertTrue(self.tasks._beacon_select_event_and_go(
            dict(type="resource", x=619, y=824, score=.99)))
        self.bot.actions.tap_frame.assert_called_once()
        self.bot.actions.tap_match.assert_called_once()

    def test_go_button_for_another_event_is_not_clicked(self):
        self.bot.wait_template.side_effect = [
            hit("beacon_resource_detail"), hit("beacon_go_button", (619, 824))]
        self.assertFalse(self.tasks._beacon_select_event_and_go(
            dict(type="resource", x=200, y=600, score=.99)))
        self.bot.actions.tap_match.assert_not_called()

    def test_excavation_dispatches_then_returns_to_world(self):
        self.bot.wait_template.side_effect = [hit("beacon_excavation_popup"),
                                             hit("beacon_excavation_button"),
                                             hit("beacon_march_controls")]
        self.bot.wait_template_any.return_value = hit("beacon_depart_text")
        self.assertTrue(self.tasks._beacon_dispatch_excavation())
        self.assertEqual([c.args[0].name for c in self.bot.actions.tap_match.call_args_list],
                         ["beacon_excavation_button", "beacon_depart_text"])
        self.bot.wait_main.assert_called_once_with(6)

    def test_excavation_missing_popup_does_not_click(self):
        self.bot.wait_template.return_value = None
        self.assertFalse(self.tasks._beacon_dispatch_excavation())
        self.bot.actions.tap_match.assert_not_called()

    def test_excavation_requires_march_page_before_departing(self):
        self.bot.wait_template.side_effect = [hit("beacon_excavation_popup"),
                                             hit("beacon_excavation_button"), None]
        self.assertFalse(self.tasks._beacon_dispatch_excavation())
        self.bot.actions.tap_match.assert_called_once()
        self.bot.wait_template_any.assert_not_called()

    def test_failed_excavation_click_does_not_depart(self):
        self.bot.wait_template.side_effect = [hit("beacon_excavation_popup"),
                                             hit("beacon_excavation_button")]
        self.bot.actions.tap_match.return_value = False
        self.assertFalse(self.tasks._beacon_dispatch_excavation())
        self.bot.wait_template_any.assert_not_called()

    def test_departure_is_not_success_if_target_popup_remains(self):
        self.bot.wait_template_any.return_value = hit("beacon_depart_text")
        self.bot.vision.match.side_effect = lambda fr, n: (
            hit(n) if n == "beacon_excavation_popup" else None)
        self.assertFalse(self.tasks._beacon_depart_march("秘藏挖掘", "beacon_excavation"))
        self.bot.fail_snapshot.assert_called_with("beacon_excavation_depart_unconfirmed")

    def test_scroll_event_uses_excavation_handler(self):
        self.tasks._beacon_select_event_and_go = Mock(return_value=True)
        self.tasks._beacon_dispatch_excavation = Mock(return_value=True)
        self.tasks._beacon_dispatch_rally = Mock()
        self.assertTrue(self.tasks._beacon_dispatch_event({"type": "excavation"}))
        self.tasks._beacon_dispatch_excavation.assert_called_once()
        self.tasks._beacon_dispatch_rally.assert_not_called()

    def test_unknown_future_type_does_not_select_or_dispatch(self):
        self.tasks._beacon_select_event_and_go = Mock()
        self.assertFalse(self.tasks._beacon_dispatch_event({"type": "new_type"}))
        self.tasks._beacon_select_event_and_go.assert_not_called()
        self.bot.fail_snapshot.assert_called_with("beacon_unsupported_event")


class BeaconScreenshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.vision = TemplateVision(ROOT)
        cls.tasks = GameTasks.__new__(GameTasks)
        cls.tasks.bot = SimpleNamespace(vision=cls.vision)

    def read(self, relative):
        frame = cv2.imread(str(ROOT / "runtime/debug" / relative))
        if frame is None:
            self.skipTest("Local screenshot fixture unavailable")
        return frame

    def test_historical_assist_failure_is_world_map(self):
        fr = self.read("20261012_171345_678814_beacon_entry_not_found.png")
        self.assertIsNone(self.tasks._beacon_page_match(fr))
        self.assertIsNotNone(self.vision.match(fr, "main_alliance"))
        self.assertIsNone(self.vision.match(fr, "reward_continue"))

    def test_activity_blocking_beacon_is_recognized_below_host_header(self):
        fr = self.read("20261012_214432_734800_unknown_start_page.png")
        match = self.vision.match(fr, "page_activity")
        self.assertIsNotNone(match)
        self.assertGreater(match.center[1], 185)
        self.assertGreater(match.score, .95)

    def test_expanded_activity_title_region_rejects_other_pages(self):
        for name in ("resident_gift_fix/user_beacon.png",
                     "20261012_171345_678814_beacon_entry_not_found.png",
                     "20261012_182642_292976_beacon_final_reward_missing.png"):
            with self.subTest(screenshot=name):
                self.assertIsNone(self.vision.match(self.read(name), "page_activity"))

    def test_historical_reward_uses_generic_continue(self):
        fr = self.read("20261012_182642_292976_beacon_final_reward_missing.png")
        self.assertIsNotNone(self.vision.match(fr, "reward_continue"))
        self.assertIsNone(self.tasks._beacon_page_match(fr))

    def test_event_detection_still_counts_seven_events_and_one_check(self):
        fr = self.read("resident_gift_fix/user_beacon.png")
        self.assertIsNotNone(self.tasks._beacon_page_match(fr))
        self.assertEqual(self.tasks._beacon_green_check_count(fr), 1)
        self.assertEqual(len(self.tasks._beacon_all_events(fr)), 7)
        unfinished = self.tasks._beacon_unfinished_events(fr)
        self.assertEqual(len(unfinished), 6)
        self.assertEqual(sum(e["type"] == "combat" for e in unfinished), 1)


if __name__ == "__main__":
    unittest.main()
