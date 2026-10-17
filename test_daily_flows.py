"""Daily-flow regressions using mocked actions and the supplied screenshots.

Run with the runtime Python that provides av and cv2. No device is contacted.
"""
import unittest
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import cv2
import numpy as np

from tasks import GameTasks
from vision import Match, TemplateVision
from game_auto_v1_17 import RecoveryBot, run_live


ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "runtime/debug/resident_gift_fix"


def hit(name, center=(363, 800)):
    x, y = center
    return Match(name, .99, (x-20, y-15, x+20, y+15), center)


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.fr = np.zeros((1600, 726, 3), dtype=np.uint8)
        self.bot = SimpleNamespace(
            frame=Mock(return_value=self.fr),
            vision=SimpleNamespace(match=Mock(return_value=None)),
            actions=Mock(), log=Mock(), fail_snapshot=Mock(),
            is_main=Mock(return_value=False), wait_main=Mock(return_value=True),
            wait_template=Mock(return_value=hit("page")),
            wait_template_any=Mock(return_value=hit("page")),
        )
        self.tasks = GameTasks.__new__(GameTasks)
        self.tasks.bot = self.bot
        sleep = patch("tasks.time.sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

    def test_resident_continues_past_choice_and_transition(self):
        stages = ["choice", "transition", "continue", "continue", "main", "main"]
        state = {"index": -1}

        def frame():
            state["index"] = min(state["index"]+1, len(stages)-1)
            return self.fr

        def match(fr, name):
            stage = stages[state["index"]]
            if (name == "page_resident_event" and stage == "choice") or (
                name == "resident_continue" and stage == "continue"
            ):
                return hit(name)
            return None

        self.bot.frame.side_effect = frame
        self.bot.vision.match.side_effect = match
        self.bot.is_main.side_effect = lambda fr: stages[state["index"]] == "main"
        self.assertTrue(self.tasks._finish_resident_event())
        self.assertEqual(self.bot.actions.tap_frame.call_count, 1)
        self.assertEqual(self.bot.actions.tap_match.call_count, 2)
        self.assertEqual(state["index"], 5)

    def test_resident_failed_tap_is_not_completion(self):
        self.bot.vision.match.side_effect = lambda fr, n: hit(n) if n == "resident_continue" else None
        self.bot.actions.tap_match.return_value = False
        self.assertFalse(self.tasks._finish_resident_event())

    def test_resident_ignores_unrelated_floating_bubble(self):
        self.tasks._ensure_city = Mock(return_value=True)
        self.tasks._resident_left_entry = Mock(side_effect=[hit("resident_event_entry"), None, None])
        self.tasks._finish_resident_event = Mock(return_value=True)
        self.bot.vision.match.side_effect = lambda fr, name: hit("resident_event_bubble") if name == "resident_event_bubble" else None
        self.assertTrue(self.tasks.resident_event())
        self.bot.actions.tap_match.assert_called_once()
        self.assertEqual(self.bot.actions.tap_match.call_args.args[0].name, "resident_event_entry")
        self.tasks._finish_resident_event.assert_called_once()

    def test_resident_rechecks_and_processes_each_remaining_entry(self):
        self.tasks._ensure_city = Mock(return_value=True)
        self.tasks._resident_left_entry = Mock(side_effect=[hit("resident_event_entry"), hit("resident_event_entry"), None, None])
        self.tasks._finish_resident_event = Mock(return_value=True)
        self.bot.vision.match.return_value = None
        self.assertTrue(self.tasks.resident_event())
        self.assertEqual(self.tasks._finish_resident_event.call_count, 2)
        self.assertEqual(self.bot.actions.tap_match.call_count, 2)
        self.assertEqual(self.tasks._ensure_city.call_count, 3)

    def test_resident_left_entry_uses_real_map_image(self):
        vision = TemplateVision(ROOT)
        present = cv2.imread(str(FIXTURES / "user_main.png"))
        lower_score = cv2.imread(str(ROOT / "runtime/debug/20261012_202641_171856_profile_page_missing.png"))
        absent = cv2.imread(str(FIXTURES / "user_queue.png"))
        self.assertIsNotNone(present)
        self.assertIsNotNone(lower_score)
        self.assertIsNotNone(absent)
        self.assertIsNotNone(self.tasks._resident_left_entry)
        self.bot.vision = vision
        self.assertIsNotNone(self.tasks._resident_left_entry(lower_score))
        self.assertIsNone(self.tasks._resident_left_entry(absent))
        self.tasks._resident_left_entry = Mock(side_effect=[hit("resident_event_entry"), None, None])
        self.bot.frame.return_value = present
        self.tasks._ensure_city = Mock(return_value=True)
        self.tasks._finish_resident_event = Mock(return_value=True)
        self.assertTrue(self.tasks.resident_event())
        self.bot.actions.tap_match.assert_called_once()
        self.assertEqual(self.bot.actions.tap_match.call_args.args[0].name, "resident_event_entry")

    def test_default_config_includes_resident_event(self):
        config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        self.assertIn("resident_event", config["task_order"])

    def test_gift_claim_closes_reward_and_verifies_gift_gone(self):
        state = {"page": "gift"}
        self.bot.is_main.side_effect = lambda *args: state["page"] != "reward"
        self.bot.vision.match.side_effect = lambda fr, n: hit(n) if (
            (state["page"] == "gift" and n == "top_gift_inner") or
            (state["page"] == "reward" and n == "reward_continue")
        ) else None
        def tap(m, *args):
            state["page"] = "reward" if m.name == "top_gift_inner" else "main"
            return True
        self.bot.actions.tap_match.side_effect = tap
        self.bot.wait_template.return_value = hit("reward_continue")
        self.assertTrue(self.tasks.handle_main_extras())
        self.assertEqual(state["page"], "main")
        self.assertEqual(self.bot.actions.tap_match.call_count, 2)

    def test_gift_that_never_disappears_is_not_reported_collected(self):
        self.bot.is_main.return_value = True
        self.bot.vision.match.side_effect = lambda fr, n: hit(n) if n == "top_gift_inner" else None
        self.bot.wait_template.return_value = None
        self.assertFalse(self.tasks.handle_main_extras())
        self.assertEqual(self.bot.actions.tap_match.call_count, 3)
        self.bot.fail_snapshot.assert_called_with("main_gift_not_collected")

    def test_beacon_assist_rechecks_until_blue_disappears(self):
        state = {"clicks": 0}
        self.bot.actions.tap_match.side_effect = lambda *args: state.update(clicks=state["clicks"]+1) or True
        self.bot.vision.match.side_effect = lambda fr, n: (
            hit(n) if n == "page_beacon" or (n == "beacon_assist_blue" and state["clicks"] < 2) else None
        )
        self.assertTrue(self.tasks._beacon_assist_once())
        self.assertEqual(state["clicks"], 2)

    def test_beacon_failed_assist_is_failure(self):
        self.bot.vision.match.side_effect = lambda fr, n: hit(n)
        self.bot.actions.tap_match.return_value = False
        self.assertFalse(self.tasks._beacon_assist_once())

    def test_beacon_claims_available_rewards_before_remaining_events(self):
        # Claim first, then recheck in place without leaving the task page.
        state = {"visit": 0}
        claims = []
        self.tasks._enter_beacon_from_main = Mock(return_value=True)
        self.tasks._beacon_safe_assist_only_check = Mock(return_value=False)
        self.tasks._beacon_assist_once = Mock(return_value=True)
        self.tasks._beacon_compare_page = Mock()
        self.tasks._beacon_in_progress_events = Mock(return_value=[])
        self.tasks._beacon_all_events = Mock(side_effect=lambda fr: [{}]*7 if state["visit"] == 0 else [])
        self.tasks._beacon_unfinished_events = Mock(return_value=[])
        self.tasks._beacon_green_check_count = Mock(side_effect=lambda fr: 1 if state["visit"] == 0 else (7 if state["visit"] < 3 else 0))
        self.tasks._beacon_revisit = Mock(side_effect=lambda **kw: state.update(visit=state["visit"]+1) or True)
        self.tasks._close_beacon_reward = Mock(return_value=True)
        self.tasks._beacon_wait_page = Mock(side_effect=lambda **kw: state.update(visit=1) or self.fr)
        self.tasks._tap_game_back = Mock(return_value=True)
        self.bot.vision.match.side_effect = lambda fr, n: hit(n) if (
            n == "page_beacon" or
            (n == "beacon_claim_green" and state["visit"] == 0) or
            (n == "beacon_refresh_label" and state["visit"] >= 1)
        ) else None
        self.bot.actions.tap_match.side_effect = lambda *args: claims.append(state["visit"]) or True
        self.assertTrue(self.tasks.beacon())
        self.assertEqual(claims, [0])
        self.assertEqual(state["visit"], 1)
        self.assertEqual(self.tasks._beacon_assist_once.call_count, 2)
        self.tasks._beacon_revisit.assert_not_called()
        self.bot.wait_main.assert_called_once_with(5)

    def test_beacon_resource_uses_new_popup_and_march_templates(self):
        self.bot.wait_template_any.side_effect = [
            hit("world_resource_popup_alt"),
            hit("resource_collect_button_alt"),
            hit("page_resource_march_alt"),
            hit("beacon_depart_text"),
        ]
        self.bot.wait_template.return_value = hit("resource_depart_button")
        self.assertTrue(self.tasks._beacon_dispatch_resource())
        self.assertEqual(self.bot.actions.tap_match.call_count, 2)
        self.bot.wait_main.assert_called_once_with(6)

    def test_beacon_battle_starts_then_exits(self):
        self.bot.wait_template.side_effect = [
            hit("beacon_bandit_attack_button"),
            hit("beacon_battle_start_button"),
            hit("beacon_battle_timer"),
        ]
        self.tasks._tap_game_back = Mock(return_value=True)
        self.assertTrue(self.tasks._beacon_dispatch_battle())
        self.assertEqual(self.bot.actions.tap_match.call_count, 2)
        self.tasks._tap_game_back.assert_called_once()
        self.bot.wait_main.assert_called_once_with(6)

    def test_mount_entry_positions_are_persisted_and_compared(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            self.tasks.mount_entry_log_path = str(Path(directory) / "coords.jsonl")
            self.tasks._record_mount_entry_position(hit("mount_entry_bubble", (300, 800)), self.fr, 2)
            self.tasks._record_mount_entry_position(hit("mount_entry_bubble", (320, 820)), self.fr, 3)
            rows = [json.loads(line) for line in Path(self.tasks.mount_entry_log_path).read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[-1]["center"], [320, 820])
            self.assertEqual(rows[-1]["normalized"], [round(320/726, 5), round(820/1600, 5)])
            self.assertTrue(any("normalized_delta" in str(call.args) for call in self.bot.log.call_args_list))

    def test_generic_beacon_reward_is_dismissed_before_page_success(self):
        state = {"clicked": False}
        self.bot.actions.tap_match.side_effect = lambda *args: state.update(clicked=True) or True
        self.bot.vision.match.side_effect = lambda fr, n: hit(n) if (
            (n == "reward_continue" and not state["clicked"]) or
            (n == "page_beacon" and state["clicked"])
        ) else None
        self.assertTrue(self.tasks._close_beacon_reward())
        self.assertTrue(state["clicked"])

    def prepare_training(self, after):
        self.bot.vision.match.side_effect = lambda fr, n, **kw: hit(n, (60, 620)) if n == "queue_training_header" else None
        pending = [(.95, 310, 820)]
        self.tasks._training_claim_candidates = Mock(side_effect=[pending] + after)
        self.tasks._training_claim_target = Mock(return_value=(360, 633))
        self.tasks._open_queue_panel = Mock(return_value=True)

    def test_training_claim_verifies_original_row_is_clear(self):
        self.prepare_training([[], []])
        self.assertTrue(self.tasks._training_claim_once())
        self.assertEqual(self.bot.actions.tap_frame.call_count, 2)
        target = self.bot.actions.tap_frame.call_args.args[:2]
        self.assertEqual(target, (360, 633))
        self.tasks._open_queue_panel.assert_called_once()

    def test_training_still_pending_is_not_claimed(self):
        self.prepare_training([[ (.95, 310, 820) ]] * 6)
        self.assertFalse(self.tasks._training_claim_once())
        self.assertEqual(self.tasks._open_queue_panel.call_count, 3)
        self.bot.fail_snapshot.assert_called_with("training_claim_not_confirmed")

    def test_training_no_pending_is_distinct_from_failed_claim(self):
        self.tasks._training_claim_candidates = Mock(return_value=[])
        self.assertIsNone(self.tasks._training_claim_once())
        self.bot.actions.tap_frame.assert_not_called()

    def prepare_mount(self, waiting=False, available=True):
        self.tasks._open_mount_visit = Mock(return_value=True)
        self.tasks._tap_game_back = Mock(return_value=True)
        self.bot.vision.match.side_effect = lambda fr, n: hit(n) if (
            n == "page_mount_visit" or (n == "mount_waiting" and waiting) or
            (n == "mount_invite_button" and not waiting) or
            (n == "mount_invite_one" and available)
        ) else None

    def test_mount_waiting_exits_without_reinviting(self):
        self.prepare_mount(waiting=True)
        self.assertTrue(self.tasks.mount_visit())
        self.bot.actions.tap_match.assert_not_called()
        self.tasks._tap_game_back.assert_called_once()

    def test_mount_invites_once_and_confirms_waiting(self):
        self.prepare_mount()
        self.assertTrue(self.tasks.mount_visit())
        self.bot.actions.tap_match.assert_called_once()
        self.bot.wait_template.assert_called_with("mount_waiting", 5)

    def test_mount_visible_invite_does_not_require_readable_count(self):
        self.prepare_mount(available=False)
        self.assertTrue(self.tasks.mount_visit())
        self.bot.actions.tap_match.assert_called_once()
        self.bot.wait_template.assert_called_with("mount_waiting", 5)

    def test_mount_does_not_report_unconfirmed_invitation(self):
        self.prepare_mount()
        self.bot.wait_template.return_value = None
        self.assertFalse(self.tasks.mount_visit())

    def test_all_tasks_check_gifts_between_modules_and_revisit_mount(self):
        bot = Mock()
        tasks = Mock()
        with patch("game_auto_v1_17.RecoveryTasks", return_value=tasks):
            self.assertEqual(run_live(bot, "all"), 0)
        self.assertEqual(tasks.mount_visit.call_count, 2)
        self.assertGreaterEqual(tasks.handle_main_extras.call_count, 15)


class ScreenshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.vision = TemplateVision(ROOT)
        cls.tasks = GameTasks.__new__(GameTasks)
        cls.tasks.bot = SimpleNamespace(vision=cls.vision, log=lambda _: None)

    def read(self, name):
        path = FIXTURES / name
        if not path.exists():
            self.skipTest("Local screenshot fixture unavailable")
        return cv2.imread(str(path))

    def test_beacon_has_seven_events_and_one_check_not_trees(self):
        fr = self.read("user_beacon.png")
        self.assertEqual(self.tasks._beacon_green_check_count(fr), 1)
        self.assertEqual(len(self.tasks._beacon_all_events(fr)), 7)
        unfinished = self.tasks._beacon_unfinished_events(fr)
        self.assertEqual(len(unfinished), 6)
        self.assertEqual(sum(event["type"] in {"combat", "bandit_raid", "bandit_suppression"} for event in unfinished), 1)

    def test_beacon_red_clock_pins_stay_actionable(self):
        path = ROOT / "runtime/debug/beacon_expiring_tasks_user.png"
        if not path.exists():
            self.skipTest("User countdown screenshot not present")
        fr = cv2.resize(cv2.imread(str(path)), (726, 1600))
        todo = self.tasks._beacon_unfinished_events(fr)
        # Five red-clock tasks plus the confirmed same-type blue pick pin.
        self.assertEqual(len(todo), 6)
        self.assertEqual(len(self.tasks._beacon_in_progress_events(fr)), 0)
        self.assertTrue(any(not self.tasks._beacon_has_expiry_clock(fr, e["x"], e["y"])
                            for e in todo))

    def test_gift_matches_all_supplied_city_scenes(self):
        for name in ("user_main.png", "user_queue.png", "user_training_hint.png", "user_mount_map.png"):
            fr = self.read(name)
            with self.subTest(name=name):
                self.assertTrue(any(self.vision.match(fr, n) for n in (
                    "top_gift_inner", "top_gift_inner_alt"
                )))

    def test_queue_targets_training_row_and_actual_bubble(self):
        fr = self.read("user_queue.png")
        candidates = self.tasks._training_claim_candidates(fr)
        self.assertEqual(len(candidates), 1)
        self.assertLess(abs(candidates[0][2]-819), 5)
        point = self.tasks._training_claim_target(self.read("user_training_hint.png"))
        self.assertTrue(330 < point[0] < 400 and 600 < point[1] < 675)

    def test_mount_ready_and_waiting_are_distinct(self):
        ready, waiting = self.read("user_mount_ready.png"), self.read("user_mount_wait.png")
        self.assertIsNotNone(self.vision.match(ready, "mount_invite_button"))
        self.assertIsNone(self.vision.match(ready, "mount_waiting"))
        self.assertIsNotNone(self.vision.match(waiting, "mount_waiting"))
        self.assertIsNone(self.vision.match(waiting, "mount_invite_button"))
        self.assertIsNotNone(self.vision.match(ready, "mount_invite_one"))

    def test_recovery_recognizes_resident_result_and_mount(self):
        bot = RecoveryBot.__new__(RecoveryBot)
        bot.vision = self.vision
        path = ROOT / "runtime/debug/20261012_182703_938236_unknown_start_page.png"
        if path.exists():
            self.assertEqual(bot.current_page(cv2.imread(str(path)))[0], "resident_continue")
        self.assertEqual(bot.current_page(self.read("user_mount_ready.png"))[0], "mount_visit")
        self.assertEqual(bot.current_page(self.read("user_main.png"))[0], "main")


if __name__ == "__main__":
    unittest.main()
