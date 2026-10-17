import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np

from vision import TemplateVision
from visual_learning import VisualLearning


class VisualLearningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        rng = np.random.default_rng(91)
        self.source = rng.integers(40, 210, (24, 24, 3), dtype=np.uint8)
        self.dest = rng.integers(40, 210, (24, 24, 3), dtype=np.uint8)
        meta = {}
        for name, image in (("new_task_entry", self.source), ("new_task_result", self.dest)):
            cv2.imwrite(str(self.root / (name+'.png')), image)
            meta[name] = dict(file=name+'.png', search_roi_raw=[0, 0, 180, 180], threshold=.8)
        (self.root/'template_meta.json').write_text(json.dumps(meta))
        self.vision = TemplateVision(self.root)
        self.before = np.zeros((1600, 726, 3), np.uint8)
        sample = np.clip(self.source.astype(float)+rng.normal(0, 15, self.source.shape), 0, 255).astype(np.uint8)
        self.before[40:64, 40:64] = sample
        self.after = np.zeros_like(self.before)
        self.after[100:124, 100:124] = self.dest
        self.current = self.before
        self.bot = SimpleNamespace(vision=self.vision, output_dir=self.root/'runtime',
                                   dry_run=False, frame=lambda:self.current.copy(), log=lambda _:None)
        self.learner = VisualLearning(self.bot)

    def tap(self):
        self.current = self.before
        match = self.vision.match(self.before, 'new_task_entry')
        self.assertIsNotNone(match)
        self.learner.before_tap(*match.center, self.before.shape)
        self.assertIsNotNone(self.learner.pending)
        self.learner.delivered(True)

    def test_success_learns_any_task_and_reloads(self):
        self.tap()
        self.current = self.after
        self.learner.outcome(self.after, target='new_task_result')
        self.assertEqual(len(self.vision.learned_templates['new_task_entry']), 1)
        reloaded = TemplateVision(self.root)
        self.assertEqual(len(reloaded.learned_templates['new_task_entry']), 1)

    def test_delivery_and_preexisting_target_do_not_teach(self):
        self.before[100:124, 100:124] = self.dest
        self.tap()
        self.learner.outcome(self.before, target='new_task_result')
        self.assertFalse(self.vision.learned_templates)

    def test_timeout_then_delayed_success_can_teach(self):
        self.tap()
        self.learner.outcome(self.before, target='new_task_result', failed=True)
        self.assertFalse(self.vision.learned_templates)
        self.current = self.after
        self.learner.outcome(self.after, target='new_task_result')
        self.assertIn('new_task_entry', self.vision.learned_templates)

    def test_repeated_no_effect_avoids_same_position(self):
        for _ in range(2):
            self.tap()
            self.learner.outcome(self.before, target='new_task_result', failed=True)
        self.assertIsNone(self.vision.match(self.before, 'new_task_entry'))
        self.assertIsNone(self.learner.before_tap(52, 52, self.before.shape, 'coordinate fallback'))
        # Moving the target is not suppressed by the old position.
        moved = np.zeros_like(self.before)
        moved[100:124, 100:124] = self.source
        self.assertIsNotNone(self.vision.match(moved, 'new_task_entry'))

    def test_dry_run_does_not_learn(self):
        self.bot.dry_run = True
        self.tap()
        self.current = self.after
        self.learner.outcome(self.after, target='new_task_result')
        self.assertFalse(self.vision.learned_templates)

    def test_unstable_destination_does_not_teach(self):
        self.tap()
        self.current = self.before
        self.learner.outcome(self.after, target='new_task_result')
        self.assertFalse(self.vision.learned_templates)

    def test_coordinate_roi_learns_only_after_verified_result(self):
        # No template match preceded this OCR/coordinate action.
        self.learner.before_tap(52, 52, self.before.shape, '训练-开始#1')
        name = self.learner.pending[1].name
        self.assertTrue(name.startswith('roi_'))
        self.assertNotIn(name, self.vision.templates)
        self.current = self.after
        self.learner.verified('OCR训练中及倒计时')
        reloaded = TemplateVision(self.root)
        self.assertIn(name, reloaded.templates)
        self.assertIsNotNone(reloaded.match(self.before, name))

    def test_failure_statistics_persist_without_global_ban(self):
        self.tap()
        self.learner.outcome(self.before, target='new_task_result', failed=True)
        reloaded = TemplateVision(self.root)
        counts = reloaded.stats['new_task_entry']
        self.assertEqual(sum(v.get('failed', 0) for v in counts.values()), 1)
        self.assertIsNotNone(reloaded.match(self.before, 'new_task_entry'))

    def test_one_attempt_is_not_counted_as_multiple_failures(self):
        self.tap()
        self.learner.outcome(self.before, target='new_task_result', failed=True)
        self.learner.outcome(self.before, target='next_probe', failed=True)
        counts = self.vision.stats['new_task_entry']
        self.assertEqual(sum(v.get('failed', 0) for v in counts.values()), 1)


if __name__ == '__main__':
    unittest.main()
