"""商行无气泡入口回归测试；不连接手机、不执行兑换。"""
import unittest
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import cv2
import numpy as np

from actions import DeviceActions
from tasks import GameTasks
from vision import Match, TemplateVision


class MerchantGestureTests(unittest.TestCase):
    def setUp(self):
        self.frame = np.zeros((1600, 726, 3), dtype=np.uint8)
        self.match = Match("merchant_name", 0.95, (280, 850, 340, 885), (310, 868))
        self.bot = SimpleNamespace(
            frame=Mock(return_value=self.frame),
            vision=SimpleNamespace(match=Mock(return_value=self.match)),
            actions=Mock(), log=Mock(), wait_template=Mock(return_value=None),
        )
        self.tasks = GameTasks.__new__(GameTasks)
        self.tasks.bot = self.bot
        self.sleep = patch("tasks.time.sleep")
        self.sleep.start()
        self.addCleanup(self.sleep.stop)

    def drag(self):
        return self.tasks._merchant_drag_name(
            self.frame, (0.55, 0.72, 0.55, 0.69), "test"
        )

    def test_name_disappears_on_release_and_tap_happens_after_up(self):
        self.bot.vision.match.side_effect = [self.match, self.match, None]
        candidate = self.drag()
        self.assertEqual(candidate[0], self.match)
        self.bot.wait_template.return_value = self.match
        self.assertTrue(self.tasks._merchant_try_entry(*candidate))
        calls = [c[0] for c in self.bot.actions.mock_calls]
        self.assertLess(calls.index("up_frame"), calls.index("tap_match"))

    def test_moving_label_never_produces_stale_click(self):
        self.bot.vision.match.side_effect = [
            Match("merchant_name", .95, (0, 0, 30, 20), (100 + i * 12, 500))
            for i in range(6)
        ]
        self.assertIsNone(self.drag())
        self.bot.actions.up_frame.assert_called_once()
        self.bot.actions.tap_match.assert_not_called()

    def test_vision_exception_releases_touch(self):
        self.bot.vision.match.side_effect = ValueError("decode failed")
        with self.assertRaisesRegex(ValueError, "decode failed"):
            self.drag()
        self.bot.actions.up_frame.assert_called_once()

    def test_move_failure_releases_touch(self):
        self.bot.actions.move_frame.return_value = False
        with self.assertRaisesRegex(RuntimeError, "MOVE"):
            self.drag()
        self.bot.actions.up_frame.assert_called_once()

    def test_release_failure_stops_before_tap(self):
        self.bot.actions.up_frame.return_value = False
        with self.assertRaisesRegex(RuntimeError, "UP"):
            self.drag()
        self.bot.actions.tap_match.assert_not_called()

    def test_click_without_shop_page_is_not_success(self):
        self.assertFalse(self.tasks._merchant_try_entry(self.match, self.frame))
        self.assertEqual(self.bot.actions.tap_match.call_count, 2)
        self.assertTrue(all(c.args[0] == "page_merchant" for c in self.bot.wait_template.call_args_list))

    def test_small_reveal_finds_merchant_before_scanning_away(self):
        self.tasks._merchant_entry_match = Mock(return_value=None)
        self.tasks._merchant_drag_name = Mock(return_value=(self.match, self.frame))
        self.tasks._record_merchant_position = Mock()
        self.bot.wait_template.return_value = self.match
        self.assertTrue(self.tasks._find_merchant())
        self.tasks._merchant_drag_name.assert_called_once()
        p = self.tasks._merchant_drag_name.call_args.args[1]
        self.assertLess(abs(p[3] - p[1]), .05)

    def test_merchant_sweep_covers_all_four_directions(self):
        patterns = self.tasks._merchant_scan_patterns()
        self.assertTrue(any(x2 < x1 for x1, y1, x2, y2 in patterns))
        self.assertTrue(any(x2 > x1 for x1, y1, x2, y2 in patterns))
        self.assertTrue(any(y2 < y1 for x1, y1, x2, y2 in patterns))
        self.assertTrue(any(y2 > y1 for x1, y1, x2, y2 in patterns))

    def test_merchant_coordinates_are_saved_and_compared(self):
        with tempfile.TemporaryDirectory() as directory:
            self.tasks.merchant_entry_log_path = str(Path(directory) / "merchant.jsonl")
            self.tasks._record_merchant_position(self.match, self.frame, 2, "drag reveal")
            moved = Match("merchant_name", .94, (300, 870, 360, 905), (330, 888))
            self.tasks._record_merchant_position(moved, self.frame, 3, "visible bubble")
            rows = [json.loads(line) for line in Path(self.tasks.merchant_entry_log_path).read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[-1]["center"], [330, 888])
            self.assertTrue(any("delta=" in call.args[0] for call in self.bot.log.call_args_list))

    def test_move_uses_device_coordinates(self):
        actions = DeviceActions.__new__(DeviceActions)
        actions.device_w, actions.device_h = 1080, 2400
        actions.dry_run, actions.log = False, Mock()
        actions._run = Mock(return_value=SimpleNamespace(returncode=0))
        self.assertTrue(actions.move_frame(363, 800, self.frame.shape))
        actions._run.assert_called_once_with(
            ["shell", "input", "motionevent", "MOVE", "540", "1200"]
        )


class MerchantVideoTests(unittest.TestCase):
    def test_name_with_no_bubble_at_several_positions_and_scales(self):
        root = Path(__file__).resolve().parent
        video = root / "Video_2026-10-12_173345.mp4"
        if not video.exists():
            self.skipTest("用户录屏不在本地")
        vision = TemplateVision(root)
        cap = cv2.VideoCapture(str(video))
        try:
            for index in (0, 90, 120, 150, 190, 210):
                cap.set(cv2.CAP_PROP_POS_FRAMES, index)
                ok, frame = cap.read()
                self.assertTrue(ok)
                game = frame[21:998, :444]
                for width, height in ((444, 977), (726, 1600)):
                    fr = cv2.resize(game, (width, height))
                    with self.subTest(index=index, width=width):
                        m = vision.match(fr, "merchant_name")
                        if index < 120:
                            self.assertIsNone(m)
                        else:
                            self.assertIsNotNone(m)
                            self.assertIsNone(vision.match(fr, "merchant_icon_inner"))
                            self.assertIsNone(vision.match(fr, "merchant_bubble"))
                            self.assertLess(m.center[0], width * .55)
                            self.assertTrue(height * .45 < m.center[1] < height * .60)
        finally:
            cap.release()


if __name__ == "__main__":
    unittest.main()
