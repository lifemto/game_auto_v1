"""fht.mp4 的识别和操作顺序回归；只重放视频帧，不操作手机。"""
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import cv2

from extract_beacon_video import read_frame
from game_auto_v1_17 import RecoveryBot
from tasks import GameTasks
from vision import TemplateVision


ROOT = Path(__file__).resolve().parent


class BeaconVideoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (ROOT / "fht.mp4").exists():
            raise unittest.SkipTest("User demonstration fht.mp4 unavailable")
        cls.vision = TemplateVision(ROOT)
        cap = cv2.VideoCapture(str(ROOT / "fht.mp4"))
        try:
            cls.frames = {t: read_frame(cap, t) for t in
                          (0, 1.6, 3, 4.5, 5.5, 7, 10, 20, 22, 23.5, 25.5, 27.5, 29.5)}
        finally:
            cap.release()

    def setUp(self):
        self.bot = RecoveryBot.__new__(RecoveryBot)
        self.bot.vision = self.vision
        self.bot.log = Mock()
        self.bot.fail_snapshot = Mock()
        self.bot.stream = SimpleNamespace(error=None)
        self.tasks = GameTasks.__new__(GameTasks)
        self.tasks.bot = self.bot

    def test_initial_three_resources_and_one_scroll(self):
        events = self.tasks._beacon_unfinished_events(self.frames[0])
        self.assertEqual([e["type"] for e in events],
                         ["resource", "resource", "resource", "excavation"])

    def test_dispatched_blue_and_yellow_nodes_are_not_dispatched_again(self):
        for seconds, resources in ((0, 3), (7, 2), (20, 0)):
            with self.subTest(seconds=seconds):
                fr = self.frames[seconds]
                events = self.tasks._beacon_unfinished_events(fr)
                self.assertEqual(sum(e["type"] == "resource" for e in events), resources)
                self.assertEqual(sum(e["type"] == "excavation" for e in events), 1)
                self.assertEqual(len(self.tasks._beacon_all_events(fr)), 4)

    def test_live_go_missing_screenshot_has_two_active_resources_and_a_dig_site(self):
        fr = cv2.imread(str(ROOT / "runtime/debug/20261012_214739_897996_beacon_event_go_missing.png"))
        self.assertIsNotNone(fr)
        events = self.tasks._beacon_unfinished_events(fr)
        self.assertEqual(sum(e["type"] == "resource" for e in events), 2)
        self.assertEqual(sum(e["type"] == "excavation" for e in events), 1)
        resource_detail = self.vision.match(fr, "beacon_resource_detail")
        go = self.vision.match(fr, "beacon_go_button")
        self.assertGreater(resource_detail.score, .9)
        self.assertGreater(go.score, .9)
        self.assertLess(abs(go.center[0]-456), 200)

    def test_visible_forms_take_priority_over_underlying_world(self):
        expected = {
            0: {"beacon"},
            3: {"beacon_resource_popup"},
            4.5: {"beacon_resource_march", "beacon_march_form", "chest_march"},
            23.5: {"beacon_excavation_popup"},
            25.5: {"beacon_march_form", "chest_march"},
            27.5: {"main"},
            29.5: {"beacon_excavation_popup"},
        }
        for seconds, pages in expected.items():
            with self.subTest(seconds=seconds):
                self.assertIn(self.bot.current_page(self.frames[seconds])[0], pages)

    def test_depart_text_accepts_different_march_times(self):
        for seconds in (4.5, 10, 25.5):
            with self.subTest(seconds=seconds):
                self.assertIsNotNone(self.vision.match(self.frames[seconds], "beacon_depart_text"))

    def test_excavation_popup_can_move_after_dispatch(self):
        for seconds in (23.5, 29.5):
            with self.subTest(seconds=seconds):
                self.assertIsNotNone(self.vision.match(self.frames[seconds], "beacon_excavation_popup"))
                self.assertIsNotNone(self.vision.match(self.frames[seconds], "beacon_excavation_button"))

    def test_new_anchors_do_not_match_beacon_or_world(self):
        for seconds in (0, 7, 20, 27.5):
            for name in ("beacon_excavation_popup", "beacon_excavation_button",
                         "beacon_depart_text", "beacon_march_controls"):
                with self.subTest(seconds=seconds, template=name):
                    self.assertIsNone(self.vision.match(self.frames[seconds], name))

    def replay_dispatch(self, event_type, times):
        state = {"step": 0}
        active_sec = {"value": times[0]}
        taps = []
        self.bot.frame = lambda: self.frames[active_sec["value"]]
        def wait_template(name, timeout):
            sec = active_sec["value"]
            if event_type == "excavation" and name == "beacon_march_controls":
                return self.vision.match(self.frames[times[3]], name)
            if state["step"] >= 2 and event_type == "resource" and name in (
                "world_resource_popup_alt", "world_resource_popup",
                "resource_collect_button_alt",
                "resource_collect_button", "beacon_excavation_button",
                "page_resource_march_alt", "page_resource_march", "beacon_march_controls",
            ):
                sec = times[min(state["step"] + 1, len(times) - 1)]
            match = self.vision.match(self.frames[sec], name)
            if name in ("beacon_resource_detail", "beacon_excavation_detail") and match:
                return match
            if name == "beacon_excavation_popup" and event_type == "excavation" and active_sec["value"] == times[2]:
                return self.vision.match(self.frames[times[2]], name)
            if name == "beacon_excavation_button" and event_type == "excavation" and active_sec["value"] == times[2]:
                return self.vision.match(self.frames[times[2]], name)
            if name in ("resource_depart_button", "beacon_depart_text") and event_type == "excavation":
                return self.vision.match(self.frames[times[3]], name)
            return None
        self.bot.wait_template = Mock(side_effect=wait_template)

        def tap_frame(x, y, shape, label):
            self.assertLess(state["step"], 2)
            taps.append(("pin", x, y))
            if state["step"] == 0:
                state["step"] = 1
                active_sec["value"] = times[1]
            elif state["step"] == 1:
                # Typed detail card fallback taps its center, opening the world popup.
                state["step"] = 2 if event_type == "resource" else 3
                active_sec["value"] = times[2]
            return True

        def tap_match(match, shape, label):
            step = state["step"]
            if step in (2, 3) and event_type == "excavation":
                names = {"beacon_excavation_button"}
            elif step == 2:
                names = {"resource_collect_button", "resource_collect_button_alt"}
            else:
                self.assertEqual(step, 3 if event_type == "resource" else 4)
                names = {"resource_depart_button", "beacon_depart_text"}
            self.assertIn(match.name, names)
            taps.append(("button", match.name))
            state["step"] += 1
            active_sec["value"] = times[min(state["step"], len(times) - 1)]
            return True

        self.bot.actions = SimpleNamespace(tap_frame=tap_frame, tap_match=tap_match)
        event = next(e for e in self.tasks._beacon_unfinished_events(self.bot.frame())
                     if e["type"] == event_type)
        self.assertTrue(self.tasks._beacon_dispatch_event(event))
        self.assertEqual(state["step"], 4 if event_type == "resource" else 5)
        self.assertEqual(taps[0][0], "pin")
        self.assertEqual(taps[1][0], "pin")
        self.bot.fail_snapshot.assert_not_called()

    def test_resource_dispatch_replays_demonstrated_order(self):
        self.replay_dispatch("resource", [0, 1.6, 3, 4.5, 5.5])

    def test_excavation_dispatch_replays_demonstrated_order(self):
        self.replay_dispatch("excavation", [20, 22, 23.5, 25.5, 27.5])

    def test_excavation_recovery_closes_popup_without_departing(self):
        self.bot.frame = lambda: self.frames[29.5]
        self.bot.actions = Mock()
        self.bot.actions.tap_frame.return_value = True
        self.assertEqual(self.bot.recover_one_step(), (True, "acted"))
        self.bot.actions.tap_frame.assert_called_once()
        self.bot.actions.tap_match.assert_not_called()
        x, y = self.bot.actions.tap_frame.call_args.args[:2]
        self.assertEqual((x, y), (363, 1152))

    def test_refresh_countdown_is_complete_without_claim_or_assist(self):
        fr = cv2.imread(str(ROOT / "runtime/debug/beacon_video/beacon_complete_sample.png"))
        self.assertIsNotNone(fr)
        self.bot.frame = Mock(return_value=fr)
        self.bot.actions = Mock()
        self.bot.wait_main = Mock(return_value=True)
        self.tasks._enter_beacon_from_main = Mock(return_value=True)
        self.tasks._beacon_safe_assist_only_check = Mock(return_value=False)
        self.tasks._beacon_assist_once = Mock(return_value=True)
        self.tasks._beacon_revisit = Mock(return_value=True)
        self.tasks._tap_game_back = Mock(return_value=True)
        self.assertIsNotNone(self.vision.match(fr, "beacon_final_refresh"))
        self.assertEqual(self.tasks._beacon_all_events(fr), [])
        self.assertEqual(self.tasks._beacon_unfinished_events(fr), [])
        self.assertIsNone(self.vision.match(fr, "beacon_claim_green"))
        self.assertIsNone(self.vision.match(fr, "beacon_assist_blue"))
        self.assertTrue(self.tasks.beacon())
        self.assertEqual(self.tasks._beacon_revisit.call_count, 1)
        self.bot.actions.tap_match.assert_not_called()
        self.tasks._tap_game_back.assert_called_once()
        self.bot.wait_main.assert_called_once_with(5)


if __name__ == "__main__":
    unittest.main()
