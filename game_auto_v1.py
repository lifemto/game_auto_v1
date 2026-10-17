# -*- coding: utf-8 -*-
import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

import cv2

from engine_stream import (
    H264Stream,
    find_file,
    get_devices,
)
from vision import TemplateVision
from actions import DeviceActions
from visual_learning import VisualLearning
from tasks import GameTasks


PROJECT_DIR = Path(__file__).resolve().parent


class AutoBot:
    def __init__(self, config, dry_run=False):
        self.config = config
        self.dry_run = dry_run

        self.output_dir = PROJECT_DIR / "runtime"
        self.debug_dir = self.output_dir / "debug"
        self.output_dir.mkdir(exist_ok=True)
        self.debug_dir.mkdir(exist_ok=True)

        self.log_file = open(
            self.output_dir / "automation.log",
            "a",
            encoding="utf-8",
            buffering=1,
        )

        self.adb = find_file(
            [
                config["adb_path"],
                r"C:\scrcpy\adb.exe",
                PROJECT_DIR / "adb.exe",
            ],
            "adb",
        )
        self.server = find_file(
            [
                config["scrcpy_server_path"],
                r"C:\scrcpy\scrcpy-server-v4.1",
            ]
        )

        if not self.adb:
            raise RuntimeError("找不到 adb.exe")
        if not self.server:
            raise RuntimeError("找不到 scrcpy-server")

        devices = get_devices(self.adb)
        if not devices:
            raise RuntimeError("没有 ADB device 状态设备")

        wanted = config.get("device_serial", "")
        self.serial = wanted if wanted in devices else devices[0]

        self.vision = TemplateVision(PROJECT_DIR)

        self.actions = DeviceActions(
            self.adb,
            self.serial,
            dry_run=dry_run,
            logger=self.log,
        )
        self.learning = VisualLearning(self)
        self.actions.learner = self.learning

        self.stream = H264Stream(
            adb_path=self.adb,
            server_path=self.server,
            serial=self.serial,
            max_fps=config.get("max_fps", 60),
            max_size=config.get("max_size", 1600),
            bitrate=config.get("video_bit_rate", 8_000_000),
            log_path=self.output_dir / "scrcpy_server.log",
        )

        self.daily_state_path = self.output_dir / "daily_state.json"
        self.daily_state = self._load_daily_state()

    def log(self, msg):
        stamp = dt.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        line = f"[{stamp}] {msg}"
        print(line, flush=True)
        try:
            self.log_file.write(line + "\n")
        except Exception:
            pass

    def _load_daily_state(self):
        today = dt.date.today().isoformat()

        if self.daily_state_path.exists():
            try:
                data = json.loads(
                    self.daily_state_path.read_text(encoding="utf-8")
                )
            except Exception:
                data = {}
        else:
            data = {}

        if data.get("date") != today:
            data = {
                "date": today,
                "free_recruit_count": 0,
            }

        return data

    def save_daily_state(self):
        self.daily_state_path.write_text(
            json.dumps(self.daily_state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def frame(self):
        return self.stream.get_frame(copy=True)

    def learning_outcome(self, frame, **kwargs):
        learner = getattr(self, "learning", None)
        if learner:
            learner.outcome(frame, **kwargs)

    def wait_template(self, name, timeout, poll=0.06):
        deadline = time.time() + timeout

        while time.time() < deadline:
            fr = self.frame()

            if fr is not None:
                m = self.vision.match(fr, name)
                if m:
                    self.log(f"[MATCH] {name} score={m.score:.3f}")
                    self.learning_outcome(fr, target=name)
                    return m

            if self.stream.error:
                raise RuntimeError(self.stream.error)

            time.sleep(poll)

        self.learning_outcome(self.frame(), target=name, failed=True)
        return None

    def wait_template_any(self, names, timeout, poll=0.06):
        """Wait until any template variant matches the current screen."""
        deadline = time.time() + timeout

        while time.time() < deadline:
            fr = self.frame()
            if fr is not None:
                for name in names:
                    m = self.vision.match(fr, name)
                    if m:
                        self.log(f"[MATCH] {name} score={m.score:.3f}")
                        self.learning_outcome(fr, target=name)
                        return m

            if self.stream.error:
                raise RuntimeError(self.stream.error)
            time.sleep(poll)

        self.learning_outcome(self.frame(), target="|".join(names), failed=True)
        return None

    def wait_template_gone(self, name, timeout, poll=0.08):
        deadline = time.time() + timeout

        while time.time() < deadline:
            fr = self.frame()

            if fr is not None and not self.vision.match(fr, name):
                self.learning_outcome(fr, gone=name)
                return True

            time.sleep(poll)

        self.learning_outcome(self.frame(), gone=name, failed=True)
        return False

    def tap_template(self, name, timeout, label=None):
        m = self.wait_template(name, timeout)
        if not m:
            return False

        fr = self.frame()
        if fr is None:
            return False
        m = self.vision.match(fr, name)
        if m is None:
            return False
        return self.actions.tap_match(m, fr.shape, label or name)

    def is_main(self, frame=None):
        fr = self.frame() if frame is None else frame
        if fr is None:
            return False
        return self.vision.match(fr, "main_alliance") is not None

    def wait_main(self, timeout=6):
        deadline = time.time() + timeout

        while time.time() < deadline:
            fr = self.frame()

            if fr is not None:
                # 顺手处理普通“点击继续”奖励页，避免被挡住
                cont = self.vision.match(fr, "reward_continue")
                if cont:
                    self.actions.tap_match(cont, fr.shape, "奖励-点击继续")
                    time.sleep(0.35)
                    continue

                if self.is_main(fr):
                    self.learning_outcome(fr, target="main_alliance")
                    return True

            time.sleep(0.08)

        return False

    def ensure_main(self):
        if self.is_main():
            return True

        # 遇到未知子页最多退5层
        for _ in range(5):
            fr = self.frame()

            if fr is None:
                time.sleep(0.2)
                continue

            cont = self.vision.match(fr, "reward_continue")
            if cont:
                self.actions.tap_match(cont, fr.shape, "奖励-点击继续")
                time.sleep(0.4)

                if self.wait_main(2):
                    return True

            if self.is_main():
                return True

            self.actions.back()
            time.sleep(0.5)

            if self.wait_main(1.2):
                return True

        self.fail_snapshot("ensure_main_failed")
        return False

    def fail_snapshot(self, reason):
        fr = self.frame()
        self.learning_outcome(fr, target=reason, failed=True)

        if fr is None:
            return

        ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        p = self.debug_dir / f"{ts}_{reason}.png"
        cv2.imwrite(str(p), fr)
        self.log(f"[DEBUG] 已保存失败现场：{p.name}")

    def maybe_learn_package(self):
        state_path = self.output_dir / "game_package.json"

        if state_path.exists():
            return

        if not self.is_main():
            return

        pkg = self.actions.foreground_package()

        if pkg:
            state_path.write_text(
                json.dumps({"package": pkg}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self.log(f"[INFO] 已自动记住游戏包名：{pkg}")

    def load_learned_package(self):
        p = self.output_dir / "game_package.json"

        if not p.exists():
            return None

        try:
            return json.loads(p.read_text(encoding="utf-8")).get("package")
        except Exception:
            return None

    def close(self):
        try:
            self.stream.stop()
        except Exception:
            pass

        try:
            self.log_file.close()
        except Exception:
            pass


def load_config():
    return json.loads(
        (PROJECT_DIR / "config.json").read_text(encoding="utf-8")
    )


def parse_args():
    ap = argparse.ArgumentParser(
        description="游戏日常自动化 v1：H.264实时识别 + ADB操作"
    )
    ap.add_argument(
        "--live",
        action="store_true",
        help="真正执行点击。不加此参数时为DRY-RUN。",
    )
    ap.add_argument(
        "--launch",
        action="store_true",
        help="若已经学到游戏包名，则先自动启动游戏。",
    )
    return ap.parse_args()


def main():
    args = parse_args()
    config = load_config()
    bot = None

    try:
        bot = AutoBot(config, dry_run=not args.live)

        bot.log("=" * 72)
        bot.log("游戏自动化 v1")
        bot.log(f"设备：{bot.serial}")
        bot.log(f"模式：{'LIVE实际点击' if args.live else 'DRY-RUN只识别不点'}")
        bot.log(
            f"视频：H.264 max_fps={config['max_fps']} "
            f"max_size={config['max_size']}"
        )
        bot.log("=" * 72)

        if args.launch:
            pkg = bot.load_learned_package()

            if pkg:
                bot.actions.launch_package(pkg)
                time.sleep(2)
            else:
                bot.log(
                    "[INFO] 尚未学到游戏包名。首次运行请先手动打开游戏，"
                    "脚本识别到主界面后会自动记住。"
                )

        bot.log("[INIT] 启动 H.264 实时视频流...")
        bot.stream.start()

        fr = bot.frame()
        bot.log(f"[OK] H.264 首帧：{fr.shape[1]}x{fr.shape[0]}")

        tasks = GameTasks(bot)

        if not tasks.startup_to_main():
            return 2

        bot.maybe_learn_package()

        tasks.handle_main_extras()

        order = config.get(
            "task_order",
            ["alliance", "crusade", "recruit", "resident_event"],
        )

        for name in order:
            fn = getattr(tasks, name, None)

            if not fn:
                bot.log(f"[WARN] 未知任务：{name}")
                continue

            try:
                if bot.is_main() and not tasks.handle_main_extras():
                    if not bot.ensure_main():
                        continue
                fn()
            except Exception as e:
                bot.log(f"[ERROR] 任务 {name} 异常：{repr(e)}")
                bot.fail_snapshot(f"task_{name}_exception")
                bot.ensure_main()

        bot.ensure_main()
        tasks.handle_main_extras()

        bot.log("\n[DONE] 第一版流程执行完毕。")
        bot.log(
            "如果某一步识别失败，把 runtime 文件夹压缩发给我，"
            "里面有 automation.log 和失败现场截图。"
        )

        return 0

    except KeyboardInterrupt:
        print("\n用户中止。")
        return 130

    except Exception as e:
        print(f"\n[FATAL] {type(e).__name__}: {e}")
        if bot:
            bot.fail_snapshot("fatal")
        return 1

    finally:
        if bot:
            bot.close()


if __name__ == "__main__":
    raise SystemExit(main())
