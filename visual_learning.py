"""Shared, outcome-gated template learning. No learning from ADB delivery alone."""
import json
import time
import hashlib
import re

import cv2
from vision import Match


class VisualLearning:
    def __init__(self, bot):
        self.bot = bot
        self.pending = None
        self.failures = {}
        self.reported_failures = set()
        self.directory = bot.output_dir / "visual_learning"

    def before_tap(self, x, y, shape, label=""):
        self.pending = None
        vision = self.bot.vision
        frame = vision.recent_frame
        candidates = [m for m in vision.recent_matches.values()
                      if m.bbox[0] <= x < m.bbox[2] and m.bbox[1] <= y < m.bbox[3]
                      and not m.name.startswith("page_")] if frame is not None and frame.shape == shape else []
        fresh = self.bot.frame()
        if fresh is None or fresh.shape != shape:
            return x, y
        # Coordinate fallbacks must not bypass a rejected template location.
        h, w = shape[:2]
        for rejected in vision.rejected.values():
            for until, cx, cy, appearance in rejected:
                if until <= time.monotonic() or abs(cx*w-x) > 12*w/726 or abs(cy*h-y) > 12*h/1600:
                    continue
                ax, ay = round(cx*w-appearance.shape[1]/2), round(cy*h-appearance.shape[0]/2)
                patch = fresh[max(0,ay):ay+appearance.shape[0], max(0,ax):ax+appearance.shape[1]]
                if patch.shape == appearance.shape and cv2.norm(patch, appearance, cv2.NORM_L1)/appearance.size < 8:
                    self.bot.log(f"[LEARN] 暂时排除连续失败的同一位置 ({x},{y})，等待重新定位")
                    return None
        if not candidates:
            if not label:
                return x, y
            key = re.sub(r"#\d+", "", label)
            name = "roi_" + hashlib.sha256(key.encode()).hexdigest()[:20]
            known = vision.match(fresh, name) if name in vision.templates else None
            if known and abs(known.center[0]-x) <= 24*shape[1]/726 and abs(known.center[1]-y) <= 24*shape[0]/1600:
                x, y = known.center
                match = known
            else:
                h, w = shape[:2]
                rx, ry = max(4, round(24*w/726)), max(4, round(18*h/1600))
                match = Match(name, 1., (max(0,x-rx), max(0,y-ry), min(w,x+rx), min(h,y+ry)), (x,y))
            self.pending = (fresh.copy(), match, time.monotonic())
            return x, y
        match = min(candidates, key=lambda m: (m.bbox[2]-m.bbox[0]) * (m.bbox[3]-m.bbox[1]))
        # Revalidate the selected crop on a fresh frame, including callers
        # that waited before tapping. Never learn an old coordinate's contents.
        current = vision.match(fresh, match.name)
        if current is None or abs(current.center[0]-match.center[0]) > 5 or abs(current.center[1]-match.center[1]) > 5:
            return x, y
        self.pending = (fresh.copy(), current, time.monotonic())
        return x, y

    def delivered(self, ok):
        if not ok:
            self.pending = None

    def outcome(self, frame, target=None, gone=None, failed=False):
        pending = self.pending
        if pending is None or self.bot.dry_run:
            return
        before, match, started = pending
        if time.monotonic() - started > 30:
            self.pending = None
            return
        vision = self.bot.vision
        if failed:
            if started in self.reported_failures:
                return
            self.reported_failures.add(started)
            self._stats(match, False)
            # Persist evidence, not a permanent ban: failure may be latency.
            self._audit(before, match, "unconfirmed", target or gone)
            if frame is not None and frame.shape == before.shape:
                x1, y1, x2, y2 = match.bbox
                a, b = before[y1:y2, x1:x2], frame[y1:y2, x1:x2]
                if a.size and cv2.norm(a, b, cv2.NORM_L1) / a.size < 8:
                    key = (match.name, round(match.center[0]/20), round(match.center[1]/20))
                    self.failures[key] = self.failures.get(key, 0) + 1
                    if self.failures[key] >= 2:
                        h, w = before.shape[:2]
                        vision.rejected.setdefault(match.name, []).append(
                            (time.monotonic()+20, match.center[0]/w, match.center[1]/h, a.copy()))
                        self.bot.log(f"[LEARN] {match.name} 连续无变化，当前位置避让20秒")
            return
        # Expected destination must be newly present. A pre-existing HUD
        # match is not evidence that an action succeeded.
        confirmed = (frame is not None and target is not None and target != match.name
                     and vision.match(frame, target) is not None
                     and vision.match(before, target) is None)
        if gone is not None:
            confirmed = gone == match.name and frame is not None and vision.match(frame, gone) is None
        if not confirmed:
            return
        # A second screenshot must agree before it can teach future runs.
        follow = self.bot.frame()
        if follow is None:
            return
        if target is not None and vision.match(follow, target) is None:
            return
        if gone is not None and vision.match(follow, gone) is not None:
            return
        self.pending = None
        self._stats(match, True)
        vision.rejected.pop(match.name, None)
        self.failures = {k:v for k,v in self.failures.items() if k[0] != match.name}
        learned = self._learn(before, match)
        self._audit(before, match, "confirmed", target or gone)
        self.bot.log(f"[LEARN] {match.name} 后置条件={target or gone} 新模板={learned}")

    def verified(self, evidence):
        """Caller has explicitly checked a non-template postcondition (OCR)."""
        if self.pending is None or self.bot.dry_run:
            return
        before, match, started = self.pending
        self.pending = None
        if time.monotonic()-started > 30:
            return
        self._stats(match, True)
        self.bot.vision.rejected.pop(match.name, None)
        self.failures = {k:v for k,v in self.failures.items() if k[0] != match.name}
        learned = self._learn(before, match)
        self._audit(before, match, "confirmed", evidence)
        self.bot.log(f"[LEARN] {match.name} 验证={evidence} 新模板={learned}")

    def finish(self, ticket, evidence, success=True, frame=None):
        """Resolve the original action even if verification required navigation."""
        if ticket is None:
            return
        current = self.pending
        self.pending = ticket
        try:
            if success:
                self.verified(evidence)
            else:
                self.outcome(frame, target=evidence, failed=True)
        finally:
            self.pending = None if current is ticket else current

    def _learn(self, frame, match):
        try:
            if match.name.startswith("roi_"):
                return self.bot.vision.record_region(frame, match)
            return self.bot.vision.record_confirmed(match.name, frame, match)
        except (OSError, cv2.error) as exc:
            self.bot.log(f"[LEARN] 保存模板失败: {exc}")
            return False

    def _stats(self, match, success):
        try:
            self.bot.vision.record_outcome(match, success)
        except OSError as exc:
            self.bot.log(f"[LEARN] 保存匹配可靠性统计失败: {exc}")

    def _audit(self, frame, match, result, expected):
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            stamp = time.time_ns()
            path = self.directory / f"{stamp}_{match.name}_{result}.png"
            cv2.imwrite(str(path), frame)
            with (self.directory / "outcomes.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(dict(time=stamp, template=match.name,
                    result=result, expected=expected, score=match.score,
                    bbox=match.bbox, roi=self.bot.vision.meta.get(match.name, {}).get("search_roi_raw", match.bbox),
                    screenshot=path.name), ensure_ascii=False)+"\n")
        except OSError as exc:
            self.bot.log(f"[LEARN] 写入学习记录失败: {exc}")
