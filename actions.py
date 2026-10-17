# -*- coding: utf-8 -*-
import re
import subprocess
import time


class DeviceActions:
    def __init__(self, adb_path, serial, dry_run=False, logger=print):
        self.adb = adb_path
        self.serial = serial
        self.dry_run = dry_run
        self.log = logger
        self.device_w, self.device_h = self._get_device_size()

    def _run(self, args, timeout=8):
        cmd = [self.adb, "-s", self.serial] + list(args)
        return subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=timeout,
            check=False,
        )

    def _get_device_size(self):
        p = self._run(["shell", "wm", "size"])
        text = p.stdout + "\n" + p.stderr

        override = re.search(r"Override size:\s*(\d+)x(\d+)", text)
        physical = re.search(r"Physical size:\s*(\d+)x(\d+)", text)

        m = override or physical
        if not m:
            self.log("[WARN] 无法读取 wm size，使用 1080x2376")
            return 1080, 2376

        return int(m.group(1)), int(m.group(2))

    def frame_to_device(self, x, y, frame_shape):
        fh, fw = frame_shape[:2]
        dx = int(round(x * self.device_w / fw))
        dy = int(round(y * self.device_h / fh))
        dx = max(0, min(self.device_w - 1, dx))
        dy = max(0, min(self.device_h - 1, dy))
        return dx, dy

    def tap_frame(self, x, y, frame_shape, label=""):
        learner = getattr(self, "learner", None)
        if learner:
            located = learner.before_tap(x, y, frame_shape, label)
            if located is None:
                return False
            x, y = located
        dx, dy = self.frame_to_device(x, y, frame_shape)
        self.log(f"[CLICK] {label} frame=({x},{y}) device=({dx},{dy})")

        if self.dry_run:
            self.log(f"[CLICK_RESULT] {label} dry-run=true")
            return True

        started = time.perf_counter()
        try:
            p = self._run(["shell", "input", "tap", str(dx), str(dy)])
        except subprocess.TimeoutExpired as exc:
            if learner:
                learner.delivered(False)
            elapsed = time.perf_counter() - started
            self.log(
                f"[CLICK_RESULT] {label} timeout after {elapsed:.2f}s "
                f"(limit={exc.timeout}s)"
            )
            return False

        elapsed = time.perf_counter() - started
        stderr = (p.stderr or "").strip().replace("\r", " ").replace("\n", " ")
        detail = f" stderr={stderr[:300]}" if stderr else ""
        self.log(
            f"[CLICK_RESULT] {label} rc={p.returncode} "
            f"elapsed={elapsed:.2f}s{detail}"
        )
        if learner:
            learner.delivered(p.returncode == 0)
        return p.returncode == 0

    def tap_match(self, match, frame_shape, label=None):
        return self.tap_frame(
            match.center[0],
            match.center[1],
            frame_shape,
            label or match.name,
        )

    def swipe_frame(
        self,
        x1,
        y1,
        x2,
        y2,
        frame_shape,
        duration_ms=300,
        label="",
    ):
        if getattr(self, "learner", None):
            self.learner.pending = None
        dx1, dy1 = self.frame_to_device(x1, y1, frame_shape)
        dx2, dy2 = self.frame_to_device(x2, y2, frame_shape)

        self.log(
            f"[SWIPE] {label} "
            f"frame=({x1},{y1})->({x2},{y2}) "
            f"device=({dx1},{dy1})->({dx2},{dy2}) "
            f"duration={duration_ms}ms"
        )

        if self.dry_run:
            return True

        p = self._run(
            [
                "shell",
                "input",
                "swipe",
                str(dx1),
                str(dy1),
                str(dx2),
                str(dy2),
                str(int(duration_ms)),
            ]
        )
        return p.returncode == 0

    def back(self):
        self.log("[KEY] BACK")
        if self.dry_run:
            return True
        p = self._run(["shell", "input", "keyevent", "4"])
        return p.returncode == 0

    def home(self):
        """Leave an app for a deliberate platform switch; caller verifies Home."""
        self.log("[KEY] HOME (切换微信/抖音端)")
        if self.dry_run:
            return True
        p = self._run(["shell", "input", "keyevent", "3"])
        return p.returncode == 0

    def down_frame(self, x, y, frame_shape, label=""):
        dx, dy = self.frame_to_device(x, y, frame_shape)
        self.log(f"[DOWN] {label} device=({dx},{dy})")

        if self.dry_run:
            return True

        p = self._run(
            ["shell", "input", "motionevent", "DOWN", str(dx), str(dy)]
        )
        return p.returncode == 0

    def move_frame(self, x, y, frame_shape, label=""):
        """移动已按下的触点；调用方必须在 finally 中发送 UP。"""
        dx, dy = self.frame_to_device(x, y, frame_shape)
        self.log(f"[MOVE] {label} device=({dx},{dy})")
        if self.dry_run:
            return True
        p = self._run(
            ["shell", "input", "motionevent", "MOVE", str(dx), str(dy)]
        )
        return p.returncode == 0

    def up_frame(self, x, y, frame_shape, label=""):
        dx, dy = self.frame_to_device(x, y, frame_shape)
        self.log(f"[UP] {label} device=({dx},{dy})")

        if self.dry_run:
            return True

        p = self._run(
            ["shell", "input", "motionevent", "UP", str(dx), str(dy)]
        )
        return p.returncode == 0

    def fallback_tap_burst(self, x, y, frame_shape, count=12, gap=0.11):
        self.log(f"[FALLBACK] 连点 {count} 次")
        for _ in range(count):
            self.tap_frame(x, y, frame_shape, label="捐助fallback")
            time.sleep(gap)

    def foreground_package(self):
        cmds = [
            ["shell", "dumpsys", "window", "windows"],
            ["shell", "dumpsys", "activity", "activities"],
        ]

        patterns = [
            r"mCurrentFocus=Window\{[^ ]+ [^ ]+ ([A-Za-z0-9._]+)/",
            r"mResumedActivity:.*? ([A-Za-z0-9._]+)/",
            r"mFocusedApp=.*? ([A-Za-z0-9._]+)/",
        ]

        for c in cmds:
            p = self._run(c, timeout=12)
            text = p.stdout + "\n" + p.stderr
            for pat in patterns:
                m = re.search(pat, text)
                if m:
                    return m.group(1)

        return None

    def launch_package(self, package):
        self.log(f"[LAUNCH] {package}")
        if self.dry_run:
            return True
        p = self._run(
            [
                "shell", "monkey",
                "-p", package,
                "-c", "android.intent.category.LAUNCHER",
                "1",
            ],
            timeout=12,
        )
        return p.returncode == 0
