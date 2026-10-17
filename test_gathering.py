"""Offline regressions; never connects to or clicks a device."""
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import cv2

from tasks import GameTasks


ROOT = Path(__file__).parent / "runtime/debug/gather_steps"


class GatheringTests(unittest.TestCase):
    def setUp(self):
        self.tasks = GameTasks.__new__(GameTasks)
        self.bot = SimpleNamespace(frame=Mock(), log=Mock(), fail_snapshot=Mock(), actions=Mock())
        self.tasks.bot = self.bot
        self.tasks._ensure_world = Mock(return_value=True)

    def test_supplied_queue_screens(self):
        expected = {
            "entry": (3, ["采集中"]*3),
            "sent": (4, ["采集中"]*3+["行军中"]),
            "full": (5, ["采集中"]*3+["行军中", "返回中"]),
            "returned": (4, ["返回中"]+["采集中"]*3),
        }
        for name, (count, statuses) in expected.items():
            with self.subTest(name=name):
                self.bot.frame.return_value = cv2.imread(str(ROOT / (name+".png")))
                _, used, capacity, rows = self.tasks._march_state()
                self.assertEqual((used, capacity), (count, 5))
                self.assertEqual([r[0] for r in rows], statuses)

    def test_five_of_five_stops_before_search(self):
        self.tasks._march_state = Mock(return_value=(None, 5, 5, []))
        self.tasks._open_search = Mock()
        self.assertEqual(self.tasks._dispatch_one_resource("farm", 5), "full")
        self.tasks._open_search.assert_not_called()

    def test_result_level_and_availability(self):
        fr = cv2.imread(str(ROOT / "detail.png"))
        self.assertTrue(self.tasks._gather_result_available(fr, 5))
        self.assertFalse(self.tasks._gather_result_available(fr, 8))
        self.tasks._gather_text = Mock(return_value="等级5采集者其他玩家")
        self.assertFalse(self.tasks._gather_result_available(fr, 5))

    def test_unknown_queue_stops(self):
        self.tasks._march_state = Mock(side_effect=ValueError("unknown"))
        self.assertFalse(self.tasks._dispatch_one_resource("farm", 5))
        self.bot.actions.tap_frame.assert_not_called()

    def test_purchase_closes_without_buying(self):
        self.bot.frame.return_value = cv2.imread(str(ROOT / "purchase.png"))
        self.assertTrue(self.tasks._gather_purchase_close())
        self.bot.actions.tap_frame.assert_called_once()
        self.assertGreater(self.bot.actions.tap_frame.call_args.args[0], 400)

    @patch("gathering.time.sleep")
    def test_recall_confirm_then_skip_returning(self, sleep):
        fr = cv2.imread(str(ROOT / "returned.png"))
        self.bot.frame.return_value = cv2.imread(str(ROOT / "confirm.png"))
        self.tasks._march_state = Mock(side_effect=[
            (fr, 1, 5, [("采集中", 266)]),
            (fr, 1, 5, [("返回中", 266)]),
            (fr, 1, 5, [("返回中", 266)]),
        ])
        self.assertTrue(self.tasks.gather_recall())
        self.assertEqual(self.bot.actions.tap_frame.call_count, 2)

    @patch("gathering.time.sleep")
    def test_unchanged_recall_does_not_retry_click(self, sleep):
        fr = cv2.imread(str(ROOT / "returned.png"))
        self.bot.frame.return_value = cv2.imread(str(ROOT / "confirm.png"))
        self.tasks._march_state = Mock(return_value=(fr, 1, 5, [("采集中", 266)]))
        self.assertFalse(self.tasks.gather_recall())
        self.assertEqual(self.bot.actions.tap_frame.call_count, 2)

    def test_no_gathering_does_not_click(self):
        self.tasks._march_state = Mock(return_value=(None, 2, 5, [("返回中", 266), ("行军中", 306)]))
        self.assertTrue(self.tasks.gather_recall())
        self.bot.actions.tap_frame.assert_not_called()


if __name__ == "__main__":
    unittest.main()
