# -*- coding: utf-8 -*-
from dataclasses import dataclass
from pathlib import Path
import json
import time
import hashlib

import cv2
import numpy as np


BASE_W = 726
BASE_H = 1600


@dataclass
class Match:
    name: str
    score: float
    bbox: tuple
    center: tuple
    variant: str = ""

    @property
    def ok(self):
        return True


class TemplateVision:
    MAX_SAMPLES = 6

    def __init__(self, project_dir, meta_file="template_meta.json"):
        self.project_dir = Path(project_dir)
        self.meta = json.loads(
            (self.project_dir / meta_file).read_text(encoding="utf-8")
        )
        self.region_meta_path = self.project_dir / "runtime" / "learned_regions.json"
        try:
            regions = json.loads(self.region_meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            regions = {}
        for name, item in regions.items():
            if name.startswith("roi_") and (self.project_dir / item["file"]).is_file():
                self.meta[name] = item
        self.templates = {}
        self.learned_templates = {}
        self.learned_dir = self.project_dir / "runtime" / "learned_templates"
        self.recent_frame = None
        self.recent_matches = {}
        self.rejected = {}
        self.stats_path = self.project_dir / "runtime" / "template_outcomes.json"
        try:
            self.stats = json.loads(self.stats_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.stats = {}

        for name, item in self.meta.items():
            p = self.project_dir / item["file"]
            img = cv2.imread(str(p), cv2.IMREAD_COLOR)
            if img is None:
                raise FileNotFoundError(p)
            self.templates[name] = img

        for name in self.templates:
            samples = []
            for path in self._sample_paths(name)[-self.MAX_SAMPLES:]:
                img = cv2.imread(str(path), cv2.IMREAD_COLOR)
                if img is not None and img.shape == self.templates[name].shape:
                    samples.append(img)
            if samples:
                self.learned_templates[name] = samples

    def _sample_paths(self, name):
        # Names can be prefixes of other names (mount_name / mount_name_2).
        return sorted(p for p in self.learned_dir.glob(f"{name}_*.png")
                      if p.stem[len(name)+1:].isdigit())

    def match(self, frame, name, threshold=None):
        if frame is not self.recent_frame:
            self.recent_frame = frame
            self.recent_matches = {}
        item = self.meta[name]
        variants = (self.templates[name], *self.learned_templates.get(name, ()))

        fh, fw = frame.shape[:2]
        sx = fw / BASE_W
        sy = fh / BASE_H

        x1, y1, x2, y2 = item["search_roi_raw"]
        x1 = max(0, min(fw - 1, round(x1 * sx)))
        y1 = max(0, min(fh - 1, round(y1 * sy)))
        x2 = max(x1 + 1, min(fw, round(x2 * sx)))
        y2 = max(y1 + 1, min(fh, round(y2 * sy)))

        roi = frame[y1:y2, x1:x2]

        # Canonical template is 726x1600-derived; adapt if stream size differs.
        best = None
        best_rank = -2.
        # Small local scale tolerance for scrcpy scaling/interpolation differences.
        for base_t in variants:
            variant = hashlib.sha256(base_t.tobytes()).hexdigest()[:16]
            history = self.stats.get(name, {}).get(variant, {})
            penalty = min(.04, .01 * max(0, history.get("failed", 0)-history.get("confirmed", 0)))
            tw = max(4, round(base_t.shape[1] * sx))
            th = max(4, round(base_t.shape[0] * sy))
            t = cv2.resize(base_t, (tw, th), interpolation=cv2.INTER_AREA)
            for factor in (0.96, 0.98, 1.00, 1.02, 1.04):
                w = max(4, round(t.shape[1] * factor))
                h = max(4, round(t.shape[0] * factor))
                if w >= roi.shape[1] or h >= roi.shape[0]:
                    continue

                tt = cv2.resize(t, (w, h), interpolation=cv2.INTER_AREA)
                result = cv2.matchTemplate(roi, tt, cv2.TM_CCOEFF_NORMED)
                # A timeout is not a permanent negative template. Avoid only
                # the same appearance/location temporarily within this run.
                for until, cx, cy, appearance in self.rejected.get(name, []):
                    if until > time.monotonic():
                        px, py = round(cx * fw - x1 - w / 2), round(cy * fh - y1 - h / 2)
                        ax, ay = round(cx * fw - appearance.shape[1]/2), round(cy * fh - appearance.shape[0]/2)
                        current = frame[max(0,ay):ay+appearance.shape[0], max(0,ax):ax+appearance.shape[1]]
                        if current.shape != appearance.shape or cv2.norm(current, appearance, cv2.NORM_L1)/appearance.size >= 8:
                            continue
                        radius = max(3, round(12 * sx))
                        left, top = max(0, px-radius), max(0, py-radius)
                        right, bottom = min(result.shape[1], px+radius+1), min(result.shape[0], py+radius+1)
                        if left < right and top < bottom:
                            result[top:bottom, left:right] = -1
                _, max_val, _, max_loc = cv2.minMaxLoc(result)

                required = float(item["threshold"] if threshold is None else threshold)
                rank = float(max_val) - penalty
                if max_val >= required and (best is None or rank > best_rank):
                    best_rank = rank
                    bx1 = x1 + max_loc[0]
                    by1 = y1 + max_loc[1]
                    bx2 = bx1 + w
                    by2 = by1 + h
                    best = (
                        float(max_val),
                        (bx1, by1, bx2, by2),
                        ((bx1 + bx2) // 2, (by1 + by2) // 2),
                        variant,
                    )

        if best is None:
            return None

        thres = float(item["threshold"] if threshold is None else threshold)
        if best[0] < thres:
            return None

        match = Match(name=name, score=best[0], bbox=best[1], center=best[2], variant=best[3])
        if threshold is None or threshold >= 0:
            self.recent_matches[name] = match
        return match

    def record_outcome(self, match, success):
        """Persist reliability without permanently deleting a template on timeout."""
        if not match.variant:
            return
        counts = self.stats.setdefault(match.name, {}).setdefault(match.variant, {})
        key = "confirmed" if success else "failed"
        counts[key] = counts.get(key, 0) + 1
        self.stats_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.stats_path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.stats, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.stats_path)

    def record_confirmed(self, name, frame, match):
        """Save a new visual variant only after the caller verifies the next page."""
        if (name not in self.templates
                or frame is None or match is None or match.name != name):
            return False
        samples = self.learned_templates.get(name, [])
        x1, y1, x2, y2 = match.bbox
        h, w = frame.shape[:2]
        if not (0 <= x1 < x2 <= w and 0 <= y1 < y2 <= h):
            return False
        # wait_template returns a match from one video frame; callers may
        # capture a newer frame before recording it. Never learn that stale box.
        current = self.match(frame, name)
        if (current is None or
                abs(current.center[0] - match.center[0]) > 5 or
                abs(current.center[1] - match.center[1]) > 5):
            return False
        base = self.templates[name]
        sample = cv2.resize(
            frame[y1:y2, x1:x2], (base.shape[1], base.shape[0]),
            interpolation=cv2.INTER_AREA,
        )
        for known in (base, *samples):
            similarity = cv2.matchTemplate(
                known, sample, cv2.TM_CCOEFF_NORMED
            )[0, 0]
            if similarity >= .985:
                return False
        try:
            self.learned_dir.mkdir(parents=True, exist_ok=True)
            path = self.learned_dir / f"{name}_{time.time_ns()}.png"
            if not cv2.imwrite(str(path), sample):
                return False
        except (OSError, cv2.error):
            return False
        self.learned_templates.setdefault(name, []).append(sample)
        self.learned_templates[name] = self.learned_templates[name][-self.MAX_SAMPLES:]
        # Keep matching and restart behaviour consistent; bound disk growth.
        for old in self._sample_paths(name)[:-self.MAX_SAMPLES]:
            try:
                old.unlink()
            except OSError:
                pass
        return True

    def record_region(self, frame, match):
        """Bootstrap an OCR/coordinate action only after its postcondition passed."""
        if match.name in self.templates:
            return self.record_confirmed(match.name, frame, match)
        if not match.name.startswith("roi_"):
            return False
        x1, y1, x2, y2 = match.bbox
        h, w = frame.shape[:2]
        sample = frame[y1:y2, x1:x2]
        if sample.size == 0 or sample.std() < 12:
            return False
        sample = cv2.resize(sample, (max(4, round((x2-x1)*BASE_W/w)),
                                    max(4, round((y2-y1)*BASE_H/h))))
        folder = self.project_dir / "runtime" / "learned_regions"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{match.name}.png"
        if not cv2.imwrite(str(path), sample):
            return False
        # Coordinate-derived samples have a deliberately narrow search area.
        rect = [round(x1*BASE_W/w)-24, round(y1*BASE_H/h)-24,
                round(x2*BASE_W/w)+24, round(y2*BASE_H/h)+24]
        item = dict(file=str(path.relative_to(self.project_dir)),
                    search_roi_raw=rect, threshold=.92)
        regions = {k:v for k,v in self.meta.items() if k.startswith("roi_")}
        regions[match.name] = item
        temporary = self.region_meta_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(regions, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.region_meta_path)
        self.meta[match.name] = item
        self.templates[match.name] = sample
        return True

    def score(self, frame, name):
        m = self.match(frame, name, threshold=-1.0)
        return -1.0 if m is None else m.score

    def best_of(self, frame, names):
        candidates = []
        for n in names:
            m = self.match(frame, n, threshold=-1.0)
            if m is not None:
                candidates.append(m)
        if not candidates:
            return None
        return max(candidates, key=lambda x: x.score)

    def annotate(self, frame, matches):
        out = frame.copy()
        for m in matches:
            if not m:
                continue
            x1, y1, x2, y2 = m.bbox
            cv2.rectangle(out, (x1, y1), (x2, y2), (0, 255, 0), 2)
            cv2.putText(
                out,
                f"{m.name}:{m.score:.2f}",
                (x1, max(20, y1 - 6)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                (0, 255, 0),
                2,
                cv2.LINE_AA,
            )
        return out
