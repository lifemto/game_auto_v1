# V16_DEBUG_CAPTURE
# -*- coding: utf-8 -*-
import time
import datetime
import json
import math
import os
import re
import subprocess

import cv2
import numpy as np
from vision import Match
from activity_tasks import ActivityTasks
from gathering import GatheringTasks
from lubu_tasks import LubuTasks
from minxin_tasks import MinxinTasks


class GameTasks(ActivityTasks, GatheringTasks, LubuTasks, MinxinTasks):
    def __init__(self, bot):
        self.bot = bot
        self.debug_click_enabled = True
        self.debug_click_dir = "debug_click"
        os.makedirs(self.debug_click_dir, exist_ok=True)

    def _record_confirmed_visual(self, name, frame, match):
        learn = getattr(self.bot.vision, "record_confirmed", None)
        if callable(learn) and learn(name, frame, match):
            self.bot.log(f"[VISION] Learned confirmed {name} appearance")

    def _is_profile_page(self, frame):
        if frame is None:
            return False
        return any(
            self.bot.vision.match(frame, name)
            for name in (
                "page_profile_wechat",
                "page_profile",
                "profile_stamina_plus",
            )
        )

    def _save_click_debug(self, frame, x, y, label):
        """保存点击可视化截图，用于调试点击准确性。"""
        try:
            if frame is None:
                return
            img = frame.copy()
            cv2.circle(img, (int(x), int(y)), 18, (0,255,0), 3)
            cv2.rectangle(img, (max(0,int(x)-35), max(0,int(y)-35)), (int(x)+35, int(y)+35), (0,0,255), 2)
            cv2.putText(img, label[:40], (20,50), cv2.FONT_HERSHEY_SIMPLEX, 1, (255,255,255), 2)
            name = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f") + "_click.png"
            cv2.imwrite(os.path.join(self.debug_click_dir, name), img)
        except Exception:
            pass

    # ============================================================
    # 通用安全导航
    # ============================================================

    def _tap_donation_close_x(self):
        """
        捐助弹窗必须点弹窗自身右上角 X。
        不能用 Android BACK，否则小游戏宿主可能把它解释成顶部“-”最小化/退出。
        坐标使用相对位置，基于已采集画面：
            X ≈ 91.8% 宽度
            Y ≈ 24.5% 高度
        """
        b = self.bot
        fr = b.frame()

        if fr is None:
            return False

        h, w = fr.shape[:2]
        x = int(round(w * 0.918))
        y = int(round(h * 0.245))

        b.log(
            f"[NAV] 点击捐助弹窗右上角X "
            f"frame=({x},{y})"
        )

        return b.actions.tap_frame(
            x,
            y,
            fr.shape,
            "捐助弹窗-X关闭",
        )

    def _tap_game_back(self, label="游戏内返回"):
        """
        子页面统一优先点游戏自身左下角返回箭头，
        避免 Android BACK 被小游戏宿主解释为最小化/退出。

        已采集页面的返回按钮中心基本固定在：
            X ≈ 13.8% 宽度
            Y ≈ 96.4% 高度
        """
        b = self.bot
        fr = b.frame()

        if fr is None:
            return False

        h, w = fr.shape[:2]
        x = int(round(w * 0.138))
        y = int(round(h * 0.964))

        b.log(
            f"[NAV] {label} "
            f"frame=({x},{y})"
        )

        return b.actions.tap_frame(
            x,
            y,
            fr.shape,
            label,
        )

    # ============================================================
    # global
    # ============================================================

    def startup_to_main(self):
        b = self.bot
        b.log("[FLOW] 启动状态整理")

        deadline = time.time() + 25

        while time.time() < deadline:
            frame = b.frame()

            if frame is None:
                time.sleep(0.1)
                continue

            m = b.vision.match(frame, "welcome_claim")
            if m:
                b.actions.tap_match(m, frame.shape, "欢迎回家-领取")
                time.sleep(0.5)
                continue

            m = b.vision.match(frame, "reward_continue")
            if m:
                b.actions.tap_match(m, frame.shape, "奖励-点击继续")
                time.sleep(0.4)
                continue

            if b.vision.match(frame, "main_alliance"):
                b.log("[OK] 已到主界面")
                return True

            time.sleep(0.12)

        b.fail_snapshot("startup_unknown")
        b.log("[FAIL] 25秒内没有识别到欢迎页/主界面")
        return False

    def _match_top_gift(self, frame):
        if frame is None:
            return None
        for name in ("top_gift_inner", "top_gift_inner_alt", "top_gift"):
            m = self.bot.vision.match(frame, name)
            if m:
                return m
        return None

    def handle_main_extras(self, rounds=3):
        b = self.bot

        if not b.is_main():
            return False

        for _ in range(rounds):
            acted = False
            frame = b.frame()

            # 红心下面/顶部中央的周期礼物
            gift = self._match_top_gift(frame)
            if gift:
                if not b.actions.tap_match(gift, frame.shape, "主界面周期礼物"):
                    return False
                acted = True

                cont = b.wait_template("reward_continue", 5)

                if cont:
                    # 奖励页刚出现时可能仍在动画阶段，
                    # 太早点击会被吃掉。先稍等，再确认页面真的关闭。
                    time.sleep(0.55)

                    reward_closed = False

                    for retry in range(1, 4):
                        fr = b.frame()

                        cont_now = b.vision.match(
                            fr,
                            "reward_continue",
                        )

                        if not cont_now:
                            reward_closed = True
                            break

                        b.log(
                            f"[MAIN GIFT] 点击继续#{retry}"
                        )

                        b.actions.tap_match(
                            cont_now,
                            fr.shape,
                            f"主界面礼物-点击继续#{retry}",
                        )

                        deadline = time.time() + 1.4

                        while time.time() < deadline:
                            fr2 = b.frame()

                            if not b.vision.match(
                                fr2,
                                "reward_continue",
                            ):
                                reward_closed = True
                                break

                            time.sleep(0.10)

                        if reward_closed:
                            b.log(
                                f"[OK] 主界面礼物奖励页已关闭 "
                                f"(第{retry}次点击成功)"
                            )
                            break

                        b.log(
                            f"[WARN] 主界面礼物第{retry}次点击后"
                            "奖励页仍在，准备重试"
                        )

                    if not reward_closed:
                        b.log(
                            "[WARN] 主界面礼物连续3次点击继续后"
                            "奖励页仍未关闭"
                        )
                        b.fail_snapshot(
                            "main_gift_reward_stuck"
                        )
                        return False

                    # 奖励页真正消失后，再确认主界面。
                    if not b.wait_main(4):
                        if not b.ensure_main():
                            return False

            frame = b.frame()
            if frame is None or not b.is_main(frame):
                if not b.ensure_main():
                    return False

            # 同盟旁边临时帮助小手
            if not self._handle_congratulations():
                return False
            frame = b.frame()
            hand = b.vision.match(frame, "help_hand") if frame is not None else None
            if hand:
                b.actions.tap_match(hand, frame.shape, "帮助小手")
                acted = True
                # 它点一下会直接消失
                b.wait_template_gone("help_hand", 4)

            if not acted:
                break

        frame = b.frame()
        if frame is not None and b.is_main(frame) and self._match_top_gift(frame):
            b.fail_snapshot("main_gift_not_collected")
            b.log("[WARN] 顶部礼物仍可领取，不能判定领取成功")
            return False
        return True

    # ============================================================
    # alliance
    # ============================================================

    def alliance(self):
        b = self.bot
        b.log("\n[FLOW] 同盟科技捐助 + 同盟礼物 V3")

        if not b.ensure_main():
            return False

        self.handle_main_extras()

        if not b.tap_template("main_alliance", 3, "进入同盟"):
            b.log("[ALLIANCE] 初次未找到同盟入口，复位城内外视图后重试")
            if not self._reset_city_world_for_entry("同盟入口复位", cycles=2):
                b.log("[ALLIANCE] 视图复位未完成，仍继续重试当前入口")
            if not b.tap_template("main_alliance", 3, "复位后进入同盟"):
                b.fail_snapshot("main_alliance_not_found")
                return False

        if not b.wait_template("alliance_tech", 6):
            b.fail_snapshot("alliance_home_not_found")
            return False

        # ---- 科技捐助 ----
        if b.tap_template("alliance_tech", 2, "同盟科技"):
            hand = None
            hand_name = None
            hand_deadline = time.monotonic() + 5
            while time.monotonic() < hand_deadline and hand is None:
                fr = b.frame()
                for candidate in ("tech_hand_tutorial", "tech_hand"):
                    if candidate in b.vision.meta:
                        hand = b.vision.match(fr, candidate)
                        if hand:
                            hand_name = candidate
                            break
                if hand is None:
                    time.sleep(.25)

            if hand:
                fr = b.frame()
                # The green hand is a tutorial pointer over an available tech
                # node. Use its current match location, not an old fixed node.
                hand = b.vision.match(fr, hand_name)
                if hand is None:
                    b.log("[TECH] 小手消失，重取画面后再定位")
                    time.sleep(.3)
                    fr = b.frame()
                    hand = b.vision.match(fr, hand_name) if fr is not None else None
                if hand is None:
                    b.fail_snapshot("alliance_tech_hand_lost_before_click")
                    return False
                b.log(f"[TECH] 发现可点击绿色小手 {hand_name} score={hand.score:.3f}")
                b.actions.tap_match(hand, fr.shape, "科技绿色小手")

                blue = b.wait_template("donate_blue", 5)

                if blue:
                    self._hold_donate_until_done(blue)
                else:
                    b.log("[INFO] 没有蓝色捐助按钮，跳过")

                self._tap_donation_close_x()
                time.sleep(0.5)

            self._tap_game_back("同盟科技-返回同盟首页")
            time.sleep(0.8)

            # 确认已经回到同盟首页，再进入同盟礼物
            if not b.wait_template("page_alliance_home", 5):
                b.log("[WARN] 未返回同盟首页，尝试再次返回")
                self._tap_game_back("同盟科技-重新返回同盟首页")
                time.sleep(1)

                if not b.wait_template("page_alliance_home", 5):
                    b.fail_snapshot("alliance_home_after_tech_failed")
                    return False

        # ---- 同盟礼物 V16：按当前高亮页签处理 ----
        # 页面进入后不再假定哪一个页签默认高亮。用户实际看到的状态
        # 可能是“同盟送礼”，也可能是“同盟礼物宝箱”；先识别高亮页，
        # 处理当前页，再切换到另一个页签。
        TAB_LAYOUT = {
            "同盟礼物宝箱": {
                "center": (180, 507),
                "rect": (45, 470, 315, 544),
                "active_template": "gift_tab_left_active",
            },
            "同盟送礼": {
                "center": (509, 507),
                "rect": (374, 470, 644, 544),
                "active_template": "gift_tab_right_active",
            },
        }

        def _scaled_rect(frame, rect, inset=(0, 0, 0, 0)):
            """把基准 726x1600 坐标换算为当前视频帧坐标。"""
            fh, fw = frame.shape[:2]
            sx, sy = fw / 726.0, fh / 1600.0
            x1, y1, x2, y2 = rect
            ix1, iy1, ix2, iy2 = inset
            return (
                max(0, int(round((x1 + ix1) * sx))),
                max(0, int(round((y1 + iy1) * sy))),
                min(fw, int(round((x2 - ix2) * sx))),
                min(fh, int(round((y2 - iy2) * sy))),
            )

        def _tab_light_score(frame, label):
            """活动页签是浅棕色，未选页签是深灰棕色。"""
            if frame is None:
                return -1.0

            box = _scaled_rect(
                frame,
                TAB_LAYOUT[label]["rect"],
                inset=(12, 10, 12, 9),
            )
            x1, y1, x2, y2 = box
            crop = frame[y1:y2, x1:x2]
            if crop.size == 0:
                return -1.0

            # frame 是 BGR；用亮度而不是单一颜色，适应不同压缩/亮度。
            bgr = crop.astype(np.float32)
            luminance = (
                0.114 * bgr[:, :, 0]
                + 0.587 * bgr[:, :, 1]
                + 0.299 * bgr[:, :, 2]
            )
            return float(luminance.mean())

        def active_gift_tab(frame):
            """返回当前高亮页签；无法判断时返回 None。"""
            if frame is None:
                return None

            scores = {
                label: _tab_light_score(frame, label)
                for label in TAB_LAYOUT
            }
            labels = list(scores)
            labels.sort(key=lambda item: scores[item], reverse=True)

            # 当前截图中高亮/未高亮亮度差很明显；保留阈值避免动画中误判。
            if scores[labels[0]] - scores[labels[1]] >= 10.0:
                return labels[0]

            # 亮度不够明确时再用页签文字模板兜底。
            for label in labels:
                template = TAB_LAYOUT[label]["active_template"]
                match = b.vision.match(frame, template, threshold=0.64)
                if match:
                    return label

            return None

        def _tab_points(frame, label):
            """给页签中心和小范围边缘点，避免固定比例落在标题区。"""
            fh, fw = frame.shape[:2]
            sx, sy = fw / 726.0, fh / 1600.0
            cx, cy = TAB_LAYOUT[label]["center"]
            dx, dy = int(round(42 * sx)), int(round(12 * sy))
            return [
                (int(round(cx * sx)), int(round(cy * sy))),
                (int(round((cx - dx) * sx)), int(round(cy * sy))),
                (int(round((cx + dx) * sx)), int(round(cy * sy))),
                (int(round(cx * sx)), int(round((cy - dy) * sy))),
                (int(round(cx * sx)), int(round((cy + dy) * sy))),
            ]

        def tap_gift_tab(label):
            """点击目标页签，并用高亮底色确认切换成功。"""
            for attempt in range(1, 4):
                fr = b.frame()
                if fr is None:
                    time.sleep(0.10)
                    continue

                current = active_gift_tab(fr)
                if current == label:
                    b.log(f"[TAB] {label}当前已高亮")
                    return True

                b.log(
                    f"[TAB] 当前高亮={current or '未知'}，准备切换到{label}"
                )

                for point_index, (x, y) in enumerate(
                    _tab_points(fr, label),
                    start=1,
                ):
                    b.log(
                        f"[TAB] 点击{label}尝试#{attempt}.{point_index} "
                        f"frame=({x},{y})"
                    )
                    b.actions.tap_frame(
                        x,
                        y,
                        fr.shape,
                        f"切换{label}",
                    )

                    deadline = time.time() + 1.25
                    while time.time() < deadline:
                        fr2 = b.frame()
                        if fr2 is not None:
                            if active_gift_tab(fr2) == label:
                                b.log(f"[OK] {label}已切换并高亮")
                                return True
                        time.sleep(0.10)

                time.sleep(0.20)

            b.log(f"[WARN] {label}连续重试后仍未切换")
            b.fail_snapshot(f"alliance_gift_{label}_tab_failed")
            return False

        def _green_claim_centers(frame):
            """找出当前页可领取的绿色小“领取”按钮，按屏幕从上到下返回。"""
            if frame is None:
                return []

            h, w = frame.shape[:2]
            bgr = frame.astype(np.int16)
            blue = bgr[:, :, 0]
            green = bgr[:, :, 1]
            red = bgr[:, :, 2]

            # 绿色按钮的底色；把绿色进度条和左侧图标排除在扫描区外。
            mask = (
                (green >= 105)
                & ((green - red) >= 32)
                & ((green - blue) >= 22)
                & (red <= 155)
            )

            y_start = max(0, int(round(h * 0.35)))
            y_end = min(h, int(round(h * 0.90)))
            x_start = max(0, int(round(w * 0.62)))
            row_counts = mask[y_start:y_end, x_start:].sum(axis=1)
            min_row_pixels = max(10, int(round(w * 0.05)))
            rows = np.flatnonzero(row_counts >= min_row_pixels)
            if rows.size == 0:
                return []

            # 把同一个按钮的连续绿色行合并；允许两行压缩断点。
            runs = []
            run_start = run_prev = int(rows[0])
            for row in rows[1:]:
                row = int(row)
                if row <= run_prev + 3:
                    run_prev = row
                    continue
                runs.append((run_start, run_prev))
                run_start = run_prev = row
            runs.append((run_start, run_prev))

            centers = []
            for rel_y1, rel_y2 in runs:
                y1, y2 = y_start + rel_y1, y_start + rel_y2
                height = y2 - y1 + 1
                if height < max(8, int(round(h * 0.012))):
                    continue
                if height > int(round(h * 0.10)):
                    continue

                cols = np.flatnonzero(mask[y1:y2 + 1].any(axis=0))
                if cols.size == 0:
                    continue
                x1, x2 = int(cols[0]), int(cols[-1])
                width = x2 - x1 + 1
                if x1 < x_start:
                    continue
                if width < max(20, int(round(w * 0.08))):
                    continue
                if width > int(round(w * 0.30)):
                    continue

                centers.append(((x1 + x2) // 2, (y1 + y2) // 2))

            centers.sort(key=lambda point: point[1])
            return centers

        def _dismiss_gift_reward():
            """领取后若出现奖励继续页，先清掉再继续扫描当前页。"""
            dismissed = False
            for _ in range(4):
                fr = b.frame()
                if fr is None:
                    break
                cont = b.vision.match(fr, "reward_continue")
                if not cont:
                    break
                b.actions.tap_match(cont, fr.shape, "同盟礼物奖励-点击继续")
                dismissed = True
                time.sleep(0.35)
            return dismissed

        def _click_oneclick_if_ready(name):
            """若当前页签的一键领取按钮明确为绿色，优先点击。"""
            fr = b.frame()
            if fr is None or "gift_oneclick_green" not in b.vision.meta:
                return None
            if active_gift_tab(fr) != name:
                b.log(f"[GIFT] {name}一键领取前页签未确认")
                return False

            green_score = b.vision.score(fr, "gift_oneclick_green")
            grey_score = b.vision.score(fr, "gift_oneclick_grey")
            b.log(
                f"[GIFT] {name}一键领取状态 "
                f"green={green_score:.2f} grey={grey_score:.2f}"
            )
            if green_score < 0.60 or green_score <= grey_score + 0.04:
                return None

            one = b.vision.match(
                fr,
                "gift_oneclick_green",
                threshold=0.60,
            )
            if not one:
                return None

            if not b.actions.tap_match(one, fr.shape, f"{name}-一键领取"):
                b.log(f"[GIFT] {name}一键领取点击失败")
                return False

            time.sleep(0.55)
            _dismiss_gift_reward()
            if active_gift_tab(b.frame()) != name:
                b.log(f"[GIFT] {name}一键领取后页签状态未确认")
                b.fail_snapshot(f"alliance_gift_{name}_oneclick_state_unknown")
                return False

            fresh = b.frame()
            if fresh is None:
                return False
            after_green = b.vision.score(fresh, "gift_oneclick_green")
            after_grey = b.vision.score(fresh, "gift_oneclick_grey")
            if after_green >= .60 and after_green > after_grey + .04:
                b.log(f"[GIFT] {name}一键领取仍为绿色，不能确认已领")
                b.fail_snapshot(f"alliance_gift_{name}_oneclick_unchanged")
                return False

            b.log(f"[GIFT] {name}已优先执行一键领取")
            return True

        def scan_gift_page(name):
            """优先一键领取；一键按钮不可用时再逐个领取。"""
            b.log(f"[GIFT] 扫描 {name}")
            claimed = 0

            oneclick = _click_oneclick_if_ready(name)
            if oneclick is True:
                b.log(f"[GIFT] {name}一键领取完成")
                return True
            if oneclick is False:
                b.fail_snapshot(f"alliance_gift_{name}_oneclick_failed")
                return False

            b.log(f"[GIFT] {name}没有可用的一键领取，改为逐项领取")

            # 每次都重新取图：领取后列表可能上移，不能复用旧坐标。
            for attempt in range(1, 25):
                _dismiss_gift_reward()
                fr = b.frame()
                if fr is None:
                    return False
                if active_gift_tab(fr) != name:
                    b.log(f"[GIFT] {name}逐项领取前页签未确认，重新切换")
                    if not tap_gift_tab(name):
                        return False
                    fr = b.frame()
                    if fr is None or active_gift_tab(fr) != name:
                        b.fail_snapshot(f"alliance_gift_{name}_tab_lost")
                        return False

                buttons = _green_claim_centers(fr)
                if not buttons:
                    break

                x, y = buttons[0]
                b.log(
                    f"[GIFT] {name}第{attempt}个绿色领取 "
                    f"frame=({x},{y})"
                )
                if not b.actions.tap_frame(
                    x,
                    y,
                    fr.shape,
                    f"{name}-领取#{attempt}",
                ):
                    return False
                time.sleep(0.45)
                _dismiss_gift_reward()
                fresh = b.frame()
                if fresh is None or active_gift_tab(fresh) != name:
                    b.fail_snapshot(f"alliance_gift_{name}_after_claim_unknown")
                    return False
                # A new item may move into the same screen position, so use
                # actual local image change rather than button disappearance.
                radius = max(8, round(fr.shape[1] * 45 / 726))
                x1, x2 = max(0, x-radius), min(fr.shape[1], x+radius)
                y1, y2 = max(0, y-radius), min(fr.shape[0], y+radius)
                change = float(np.mean(cv2.absdiff(
                    fr[y1:y2, x1:x2], fresh[y1:y2, x1:x2])))
                if change < 5.0:
                    b.log(f"[GIFT] {name}点击后局部画面未变化 delta={change:.1f}")
                    b.fail_snapshot(f"alliance_gift_{name}_claim_unchanged")
                    return False
                claimed += 1

            if claimed:
                b.log(f"[GIFT] {name}领取完成，共处理{claimed}项")
            else:
                b.log(f"[GIFT] {name}没有绿色单项领取，跳过")
            return True


        # 进入同盟礼物。
        # 旧版本直接点击固定比例坐标，画面缩放或页面动画稍有变化时
        # 可能点在礼物行外面，而且点击失败后不会重试。这里先用礼物
        # 行模板取得当前画面的真实位置，再用多个行内候选点点击，并且
        # 每次点击后确认标题已经切换为“同盟礼物”。
        def wait_gift_page(timeout=1.2):
            deadline = time.time() + timeout
            while time.time() < deadline:
                fr2 = b.frame()
                if fr2 is not None and b.vision.match(
                    fr2,
                    "page_alliance_gift",
                ):
                    return True
                time.sleep(0.10)
            return False

        # 无论科技入口本轮是否点成功，都先确认当前确实是同盟首页。
        if not b.wait_template("page_alliance_home", 3):
            b.log("[WARN] 进入同盟礼物前未确认同盟首页")
            b.fail_snapshot("alliance_home_before_gift_failed")
            return False

        entered_gift = False
        for attempt in range(1, 4):
            fr = b.frame()
            if fr is None:
                time.sleep(0.10)
                continue

            match = b.vision.match(
                fr,
                "alliance_gift",
                threshold=0.60,
            )

            if match:
                x1, y1, x2, y2 = match.bbox
                cy = (y1 + y2) // 2
                # 从图标区、模板中心、文字区各点一次，均在同盟礼物行内。
                points = [
                    (match.center[0], match.center[1]),
                    (x1 + (x2 - x1) // 4, cy),
                    (x1 + (x2 - x1) * 3 // 4, cy),
                ]
            else:
                h, w = fr.shape[:2]
                # 模板暂时未匹配到时保留按比例的兼容兜底点。
                points = [
                    (int(w * 0.25), int(h * 0.515)),
                    (int(w * 0.17), int(h * 0.515)),
                    (int(w * 0.34), int(h * 0.515)),
                ]

            seen = set()
            for point_index, (x, y) in enumerate(points, start=1):
                point = (int(x), int(y))
                if point in seen:
                    continue
                seen.add(point)

                b.log(
                    f"[GIFT] 进入同盟礼物尝试#{attempt}.{point_index} "
                    f"frame=({point[0]},{point[1]})"
                )
                b.actions.tap_frame(
                    point[0],
                    point[1],
                    fr.shape,
                    "进入同盟礼物",
                )

                if wait_gift_page(1.2):
                    entered_gift = True
                    break

            if entered_gift:
                break

            # 页面可能正在回到首页，给下一轮一个稳定画面。
            time.sleep(0.20)

        if not entered_gift:
            b.log("[WARN] 同盟礼物入口点击后仍未进入礼物页")
            b.fail_snapshot("alliance_gift_enter_failed")
            return False

        # 可能从任意一个页签进入：先处理当前高亮页，再处理另一个页签。
        # 这样不会因为默认状态不同而先点错页签或误判“已经领取”。
        current = active_gift_tab(b.frame())
        if current == "同盟礼物宝箱":
            tab_order = ["同盟礼物宝箱", "同盟送礼"]
        elif current == "同盟送礼":
            tab_order = ["同盟送礼", "同盟礼物宝箱"]
        else:
            # 动画期间无法判断时，使用截图中更常见的右侧页签作为首选；
            # 后面仍会强制处理另一个页签。
            tab_order = ["同盟送礼", "同盟礼物宝箱"]

        b.log(
            f"[TAB] 初始高亮={current or '未知'}，处理顺序="
            + " -> ".join(tab_order)
        )

        for label in tab_order:
            if not tap_gift_tab(label):
                return False
            if not scan_gift_page(label):
                return False

        # 用户实际流程是在礼物页处理完后回到同盟主页；不要再多退一层
        # 回到主城，否则会把下一步的页面状态打乱。
        self._tap_game_back("同盟礼物-返回同盟主页")
        time.sleep(0.8)

        if not b.wait_template("page_alliance_home", 6):
            b.log("[WARN] 同盟礼物返回后未确认同盟主页")
            b.fail_snapshot("alliance_home_after_gift_failed")
            return False

        return True


    def _hold_donate_until_done(self, initial_match):
        b = self.bot
        frame = b.frame()
        x, y = initial_match.center

        b.log("[ACTION] 开始按住右侧蓝色捐助按钮")

        down_ok = b.actions.down_frame(
            x,
            y,
            frame.shape,
            "捐助按住",
        )

        if not down_ok:
            b.log("[WARN] motionevent DOWN失败，改用连点fallback")
            b.actions.fallback_tap_burst(
                x,
                y,
                frame.shape,
                count=10,
            )
            return

        try:
            deadline = time.time() + 8.0
            done_hits = 0

            while time.time() < deadline:
                fr = b.frame()

                if fr is None:
                    time.sleep(0.08)
                    continue

                blue_score = b.vision.score(fr, "donate_blue")
                grey_score = b.vision.score(fr, "donate_grey")

                if (
                    grey_score > 0.90
                    and grey_score > blue_score + 0.04
                ):
                    done_hits += 1

                    if done_hits >= 2:
                        b.log(
                            f"[OK] 捐助按钮已灰："
                            f"blue={blue_score:.2f} "
                            f"grey={grey_score:.2f}"
                        )
                        break
                else:
                    done_hits = 0

                time.sleep(0.09)

        finally:
            fr = b.frame()
            shape = fr.shape if fr is not None else frame.shape

            b.actions.up_frame(
                x,
                y,
                shape,
                "捐助松开",
            )

            time.sleep(0.3)

    # ============================================================
    # crusade
    # ============================================================

    def _tap_crusade_reward_close_x(self):
        """
        讨伐奖励弹窗右上角 X。
        不用 Android BACK，也不点底部返回。
        """
        b = self.bot
        fr = b.frame()

        if fr is None:
            return False

        h, w = fr.shape[:2]

        # 根据已采集“讨伐奖励”弹窗位置
        x = int(round(w * 0.918))
        y = int(round(h * 0.330))

        b.log(
            f"[NAV] 点击讨伐奖励弹窗右上角X "
            f"frame=({x},{y})"
        )

        return b.actions.tap_frame(
            x,
            y,
            fr.shape,
            "讨伐奖励弹窗-X关闭",
        )

    def crusade(self):
        b = self.bot
        b.log("\n[FLOW] 讨伐奖励")

        if not b.ensure_main():
            return False

        self.handle_main_extras()

        if not b.tap_template("main_crusade", 3, "进入讨伐"):
            b.fail_snapshot("main_crusade_not_found")
            return False

        # 确认真正进入讨伐页面
        crusade_page_names = ["page_crusade_wechat", "page_crusade"]
        if not b.wait_template_any(crusade_page_names, 5):
            b.fail_snapshot("crusade_page_not_confirmed")
            return False

        gift = b.wait_template("crusade_gift", 4)

        if not gift:
            b.log("[INFO] 未发现讨伐右侧礼物，跳过")
            self._tap_game_back("讨伐-返回主界面")

            if not b.wait_main(5):
                b.ensure_main()

            return True

        fr = b.frame()
        b.actions.tap_match(
            gift,
            fr.shape,
            "讨伐右侧礼物",
        )

        # 等弹窗出现。优先看绿色领取，也允许只识别到弹窗标题。
        claim = b.wait_template("crusade_claim", 4)

        if claim:
            fr = b.frame()

            b.actions.tap_match(
                claim,
                fr.shape,
                "讨伐奖励-领取",
            )

            # 领取后等待奖励结果页
            cont = b.wait_template(
                "reward_continue",
                5,
            )

            if cont:
                # 奖励结果页刚出现时可能仍处于动画阶段，
                # 第一次点击可能被游戏吃掉。
                # 因此先短暂等待，再最多重试3次，
                # 直到 reward_continue 真正消失。
                time.sleep(0.65)

                reward_closed = False

                for retry in range(1, 4):
                    fr = b.frame()

                    cont_now = b.vision.match(
                        fr,
                        "reward_continue",
                    )

                    if not cont_now:
                        reward_closed = True
                        b.log(
                            f"[OK] 奖励页已消失 "
                            f"(点击继续重试次数={retry-1})"
                        )
                        break

                    b.log(
                        f"[REWARD] 点击继续#{retry}"
                    )

                    b.actions.tap_match(
                        cont_now,
                        fr.shape,
                        f"讨伐奖励-点击继续#{retry}",
                    )

                    # 等待最多1.5秒看奖励页是否消失。
                    deadline = time.time() + 1.5

                    while time.time() < deadline:
                        fr2 = b.frame()

                        if not b.vision.match(
                            fr2,
                            "reward_continue",
                        ):
                            reward_closed = True
                            break

                        time.sleep(0.10)

                    if reward_closed:
                        b.log(
                            f"[OK] 奖励页已关闭 "
                            f"(第{retry}次点击继续成功)"
                        )
                        break

                    b.log(
                        f"[WARN] 第{retry}次点击继续后奖励页仍在，准备重试"
                    )

                if not reward_closed:
                    b.log(
                        "[WARN] 连续3次点击继续后奖励页仍未关闭"
                    )
                    b.fail_snapshot(
                        "crusade_reward_continue_stuck"
                    )

                    if not b.ensure_main():
                        return False

                    self.handle_main_extras()
                    return True

                # 奖励页真正消失后，再等讨伐主界面出现。
                back_to_crusade = b.wait_template_any(
                    crusade_page_names, 5
                )

                if back_to_crusade:
                    b.log(
                        "[OK] 奖励页已关闭，确认回到讨伐主界面"
                    )
                else:
                    b.log(
                        "[WARN] 奖励页消失后5秒仍未确认讨伐主界面"
                    )
                    b.fail_snapshot(
                        "crusade_after_reward_not_back"
                    )

                    if not b.ensure_main():
                        return False

                    self.handle_main_extras()
                    return True

            else:
                # 点击领取后没出现“获得奖励”，不继续猜。
                # 看看是否已经直接回到讨伐页。
                if b.wait_template_any(crusade_page_names, 1.5):
                    b.log(
                        "[INFO] 未出现奖励结果页，但已回到讨伐主界面"
                    )
                else:
                    b.log(
                        "[WARN] 领取后未出现奖励结果页，状态不明确"
                    )
                    b.fail_snapshot(
                        "crusade_claim_result_unknown"
                    )

                    if not b.ensure_main():
                        return False

                    self.handle_main_extras()
                    return True

        else:
            # 如果礼物弹窗打开了但没有绿色领取，安全关闭弹窗。
            popup = b.vision.match(
                b.frame(),
                "page_crusade_reward_popup",
            )

            if popup:
                b.log(
                    "[INFO] 讨伐奖励弹窗当前不可领取，关闭弹窗"
                )
                self._tap_crusade_reward_close_x()

                if not b.wait_template_any(crusade_page_names, 4):
                    b.fail_snapshot(
                        "crusade_popup_close_failed"
                    )
                    return False
            else:
                b.log(
                    "[WARN] 点击讨伐礼物后未识别到奖励弹窗"
                )
                b.fail_snapshot(
                    "crusade_reward_popup_unknown"
                )
                return False

        # 到这里必须已确认在讨伐主界面，再返回主界面
        if not b.wait_template_any(crusade_page_names, 3):
            b.log(
                "[WARN] 返回主界面前没有确认讨伐页面"
            )
            b.fail_snapshot(
                "crusade_before_return_not_confirmed"
            )

            if not b.ensure_main():
                return False

            self.handle_main_extras()
            return True

        self._tap_game_back(
            "讨伐-返回主界面"
        )

        if not b.wait_main(5):
            b.log(
                "[WARN] 讨伐返回主界面第一次未确认，"
                "交给安全恢复逻辑"
            )

            if not b.ensure_main():
                return False

        self.handle_main_extras()

        return True

    # ============================================================
    # recruit
    # ============================================================

    def _exit_recruit_result_safely(self):
        """
        招募结果页：
        - 绝不点黄色“再招募一次”
        - 只点左侧安全空白
        - 确认“获得奖励”结果页真的消失
        """
        b = self.bot

        time.sleep(0.60)

        for retry in range(1, 4):
            fr = b.frame()

            result_page = b.vision.match(
                fr,
                "page_recruit_result",
            ) or b.vision.match(fr, "page_daily_total_reward")

            if not result_page:
                b.log(
                    f"[OK] 招募结果页已消失 "
                    f"(安全退出重试次数={retry-1})"
                )
                return True

            h, w = fr.shape[:2]

            b.log(
                f"[RECRUIT] 安全空白退出#{retry}"
            )

            b.actions.tap_frame(
                int(w * 0.10),
                int(h * 0.70),
                fr.shape,
                f"招募结果-安全空白退出#{retry}",
            )

            deadline = time.time() + 1.6

            while time.time() < deadline:
                fr2 = b.frame()

                if not b.vision.match(
                    fr2,
                    "page_recruit_result",
                ) and not b.vision.match(
                    fr2,
                    "page_daily_total_reward",
                ):
                    b.log(
                        f"[OK] 招募结果页已关闭 "
                        f"(第{retry}次安全点击成功)"
                    )
                    return True

                time.sleep(0.10)

            b.log(
                f"[WARN] 第{retry}次安全空白点击后结果页仍在"
            )

        b.fail_snapshot(
            "recruit_result_exit_stuck"
        )
        b.log(
            "[STOP] 连续3次安全空白点击仍无法退出招募结果页"
        )
        return False

    def _wait_recruit_entry_state(self, timeout=5):
        from recruit_tasks import wait_state
        return wait_state(self, timeout)

    def _return_recruit_to_main(self):
        """
        当前处于任一种武将招募页面时安全回主界面：
        招募页 -> 武将列表 -> 主界面。
        """
        b = self.bot

        self._tap_game_back(
            "武将招募-返回武将列表"
        )

        if not b.wait_template(
            "page_generals",
            5,
        ):
            b.log(
                "[WARN] 武将招募返回后未确认武将列表"
            )
            b.fail_snapshot(
                "recruit_return_generals_timeout"
            )

            return b.ensure_main()

        b.log(
            "[OK] 已确认回到武将列表"
        )

        self._tap_game_back(
            "武将列表-返回主界面"
        )

        if not b.wait_main(5):
            b.log(
                "[WARN] 武将列表返回主界面未确认，"
                "交给安全恢复逻辑"
            )
            return b.ensure_main()

        return True

    def recruit(self):
        from recruit_tasks import run
        return run(self)

    # ============================================================
    # daily tasks
    # ============================================================

    def _match_daily_tasks_page(self, frame=None):
        b = self.bot
        fr = b.frame() if frame is None else frame
        if fr is None:
            return None
        return (
            b.vision.match(fr, "page_daily_tasks_wechat")
            or b.vision.match(fr, "page_daily_tasks")
        )

    def _wait_daily_tasks_page(self, timeout):
        return self.bot.wait_template_any(
            ["page_daily_tasks_wechat", "page_daily_tasks"],
            timeout,
        )

    def _match_daily_claim_button(self, frame):
        b = self.bot
        if frame is None:
            return None
        return (
            b.vision.match(frame, "daily_claim_green")
            or b.vision.match(frame, "daily_claim_oneclick")
        )

    def _tap_daily_close_x(self):
        """
        日常任务弹窗右上角 X。
        基于 447x1001 采样图，中心约为 (422, 222)。
        """
        b = self.bot
        fr = b.frame()

        if fr is None:
            return False

        h, w = fr.shape[:2]
        x = int(round(w * 0.944))
        is_wechat_daily = b.vision.match(
            fr,
            "page_daily_tasks_wechat",
        )
        y = int(round(h * (0.181 if is_wechat_daily else 0.222)))

        b.log(
            f"[NAV] 日常任务-X关闭 frame=({x},{y})"
        )

        return b.actions.tap_frame(
            x,
            y,
            fr.shape,
            "日常任务-X关闭",
        )

    def _close_daily_total_reward(self):
        """
        已知的“全部做完后总奖励”页面。
        底部只有“点击继续”，但仍做状态确认和重试。
        """
        b = self.bot

        time.sleep(0.55)

        for retry in range(1, 4):
            fr = b.frame()

            if not b.vision.match(
                fr,
                "page_daily_total_reward",
            ):
                b.log(
                    f"[OK] 日常总奖励页已消失 "
                    f"(重试次数={retry-1})"
                )
                return True

            h, w = fr.shape[:2]
            x = int(round(w * 0.50))
            y = int(round(h * 0.958))

            b.log(
                f"[DAILY REWARD] 点击继续#{retry}"
            )

            b.actions.tap_frame(
                x,
                y,
                fr.shape,
                f"日常总奖励-点击继续#{retry}",
            )

            deadline = time.time() + 1.5

            while time.time() < deadline:
                fr2 = b.frame()

                if not b.vision.match(
                    fr2,
                    "page_daily_total_reward",
                ):
                    b.log(
                        f"[OK] 日常总奖励页已关闭 "
                        f"(第{retry}次点击成功)"
                    )
                    return True

                time.sleep(0.10)

            b.log(
                f"[WARN] 日常总奖励第{retry}次点击后仍在"
            )

        b.fail_snapshot(
            "daily_total_reward_stuck"
        )
        return False

    def _close_daily_claim_reward_popup(self):
        """关闭单项日常领取后出现的通用奖励页。"""
        b = self.bot
        time.sleep(0.35)
        for retry in range(1, 4):
            fr = b.frame()
            cont = b.vision.match(fr, "reward_continue")
            if not cont:
                return True
            b.log(f"[DAILY] 领取奖励弹窗-点击继续#{retry}")
            b.actions.tap_match(
                cont,
                fr.shape,
                f"日常领取奖励-点击继续#{retry}",
            )
            if b.wait_template_gone("reward_continue", 2.5):
                return True
        b.fail_snapshot("daily_claim_reward_stuck")
        b.log("[STOP] 日常领取奖励弹窗未能关闭")
        return False

    def _select_daily_tasks_tab(self):
        """确认当前在赛季任务面板后，切换右侧日常任务页签。"""
        b = self.bot
        tab_points = (
            (0.80, 0.832),
            (0.76, 0.832),
            (0.84, 0.832),
            (0.80, 0.818),
            (0.80, 0.846),
        )

        for attempt, (xr, yr) in enumerate(tab_points, start=1):
            fr = b.frame()
            if fr is None:
                return False

            if self._match_daily_tasks_page(fr):
                b.log("[DAILY] 已确认当前选中日常任务页签")
                return True

            if not b.vision.match(fr, "page_season_tasks"):
                b.log("[DAILY] 当前任务页既非赛季任务也非日常任务，停止切换")
                b.fail_snapshot("daily_tab_source_unknown")
                return False

            h, w = fr.shape[:2]
            x = int(round(w * xr))
            y = int(round(h * yr))
            b.log(
                f"[DAILY] 赛季任务页切换日常任务页签#{attempt} "
                f"frame=({x},{y})"
            )
            if not b.actions.tap_frame(
                x,
                y,
                fr.shape,
                f"切换日常任务页签#{attempt}",
            ):
                continue

            if self._wait_daily_tasks_page(1.5):
                b.log("[OK] 已切换并确认日常任务页签")
                return True

        b.log("[DAILY] 未能确认切换到日常任务页签，停止领取")
        b.fail_snapshot("daily_tab_switch_failed")
        return False

    def _daily_entry_badge_present(self):
        """Detect the red notification badge above the main daily icon."""
        b = self.bot
        seen = 0
        valid = 0
        for _ in range(3):
            fr = b.frame()
            if fr is None:
                time.sleep(.15)
                continue
            valid += 1
            h, w = fr.shape[:2]
            icon = b.vision.match(fr, "main_daily_entry")
            if icon and icon.center[0] < w * .20 and icon.center[1] > h * .77:
                x1, y1, x2, _ = icon.bbox
                badge_roi = (x2 - round(w * .035), y1 - round(h * .035),
                             x2 + round(w * .025), y1 - round(h * .004))
            else:
                badge_roi = (round(w * 63 / 726), round(h * 1290 / 1600),
                             round(w * 112 / 726), round(h * 1350 / 1600))
            x1, y1, x2, y2 = badge_roi
            patch = fr[max(0, y1):min(h, y2), max(0, x1):min(w, x2)]
            if patch.size == 0:
                continue
            hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
            low_red = cv2.inRange(hsv, (0, 145, 95), (12, 255, 255))
            high_red = cv2.inRange(hsv, (168, 145, 95), (179, 255, 255))
            mask = cv2.bitwise_or(low_red, high_red)
            count, _, stats, _ = cv2.connectedComponentsWithStats(mask)
            scale = w / 726
            for index in range(1, count):
                _, _, cw, ch, area = stats[index]
                if (7 * scale <= cw <= 38 * scale
                        and 7 * scale <= ch <= 38 * scale
                        and .55 <= cw / ch <= 1.55
                        and area >= 45 * scale * scale):
                    seen += 1
                    break
            time.sleep(.15)
        if valid < 2:
            b.log("[DAILY] 主界面帧不足，无法确认日常入口红色角标")
            return None
        b.log(f"[DAILY] 入口红色角标检测：{seen}/{valid}帧")
        if seen >= 2:
            return True
        return False if seen == 0 else None

    def daily_tasks(self):
        """
        日常任务处理：

        规则：
        1. 进入日常任务页。
        2. 只点击绿色“领取”，绝不点击蓝色“前往”。
        3. 绿色领取按钮出现后直接领取，不再等待最终档模板。
        4. 领取后：
           - 有总奖励页则关闭；
           - 没有总奖励页则继续扫描。
        5. 最多循环领取，避免卡死。
        """

        b = self.bot
        b.log("\n[FLOW] 日常任务奖励 V2")

        if not b.ensure_main():
            return False

        if not self.handle_main_extras():
            return False

        badge = self._daily_entry_badge_present()
        if badge is not True:
            b.log("[DAILY] 日常入口红色角标未确认，跳过本次打开；状态待下轮复查"
                  if badge is None else "[DAILY] 日常入口无红色角标，跳过面板")
            return True

        entry_opened = b.tap_template(
            "main_daily_entry",
            2,
            "进入日常任务",
        )

        if not entry_opened:
            # 当前版本主城左下角入口图案会随红点/遮挡变化，旧模板可能
            # 低于阈值。按已确认的入口区域做小范围重试，每次都要求
            # “日常任务”页面模板出现，避免把固定坐标当成盲点。
            b.log(
                "[DAILY] 日常任务入口模板未命中，"
                "按左下角入口区域重试"
            )
            fallback_points = (
                (0.080, 0.840),
                (0.080, 0.855),
                (0.095, 0.855),
                (0.065, 0.855),
            )
            for point_index, (xr, yr) in enumerate(
                fallback_points,
                start=1,
            ):
                fr = b.frame()
                if fr is None:
                    break
                h, w = fr.shape[:2]
                x = int(round(w * xr))
                y = int(round(h * yr))
                b.log(
                    f"[DAILY] 固定入口重试#{point_index} "
                    f"frame=({x},{y})"
                )
                b.actions.tap_frame(
                    x,
                    y,
                    fr.shape,
                    f"日常任务入口-固定区域#{point_index}",
                )
                if self._wait_daily_tasks_page(1.6):
                    entry_opened = True
                    break

        if not entry_opened:
            b.log("[INFO] 当前主界面未识别到日常任务入口，跳过")
            return True

        daily_page = self._wait_daily_tasks_page(1.8)
        if not daily_page:
            season_page = b.wait_template("page_season_tasks", 1.8)
            if season_page:
                b.log("[DAILY] 入口打开的是赛季任务页，先切换到日常任务")
                if not self._select_daily_tasks_tab():
                    return False
                daily_page = self._match_daily_tasks_page(b.frame())
            else:
                # 任务面板可能仍在动画中，再等待日常页标题加载。
                daily_page = self._wait_daily_tasks_page(3.4)

        if not daily_page:
            b.fail_snapshot("daily_page_not_confirmed")
            return False

        b.log("[OK] 已确认日常任务页")

        # 连续领取，最多20次，防止异常循环
        for idx in range(1, 21):

            fr = b.frame()

            if fr is None:
                break

            claim = self._match_daily_claim_button(fr)

            if claim:
                b.log(f"[DAILY] 找到领取按钮 #{idx}")

                b.actions.tap_match(
                    claim,
                    fr.shape,
                    f"日常任务-领取#{idx}",
                )

                time.sleep(0.8)

                # 单项领取后可能弹出日常总奖励页或普通奖励页。
                after_claim = b.frame()
                if b.vision.match(after_claim, "page_daily_total_reward"):
                    b.log("[DAILY] 检测到日常总奖励页")
                    if not self._close_daily_total_reward():
                        return False
                elif b.vision.match(after_claim, "reward_continue"):
                    if not self._close_daily_claim_reward_popup():
                        return False

                if not self._wait_daily_tasks_page(3):
                    b.log("[WARN] 日常领取后未能确认回到任务列表")
                    b.fail_snapshot("daily_page_missing_after_claim")
                    return False

                continue

            # 没有领取，尝试向下扫描任务列表
            b.log("[DAILY] 当前屏无领取，尝试下滑扫描")

            fr = b.frame()

            if fr is None:
                break

            b.actions.swipe_frame(
                int(fr.shape[1] * 0.50),
                int(fr.shape[0] * 0.78),
                int(fr.shape[1] * 0.50),
                int(fr.shape[0] * 0.45),
                fr.shape,
                700,
                "日常任务-向下扫描",
            )

            time.sleep(0.8)

            claim_after = self._match_daily_claim_button(b.frame())

            if claim_after:
                continue

            # 没有任何领取，结束扫描
            b.log("[DAILY] 未发现更多绿色领取")
            break

        # 回顶部不是必须，直接安全退出
        self._tap_daily_close_x()

        if not b.wait_main(5):
            if not b.ensure_main():
                return False

        b.log("[OK] 日常任务流程结束")
        return True

    # ============================================================
    # beacon / 烽火台
    # ============================================================

    def _close_beacon_reward(self):
        """兼容专用奖励页和普通奖励弹窗，确认返回烽火台。"""
        b = self.bot
        time.sleep(.60)
        for retry in range(1, 7):
            fr = b.frame()
            if fr is None:
                time.sleep(.2)
                continue
            cont = b.vision.match(fr, "reward_continue")
            reward = b.vision.match(fr, "page_beacon_reward")
            if cont:
                if not b.actions.tap_match(cont, fr.shape, "烽火台奖励-继续"):
                    return False
            elif reward:
                h, w = fr.shape[:2]
                if not b.actions.tap_frame(round(w*.50), round(h*.958), fr.shape,
                                           "烽火台奖励-继续"):
                    return False
            elif self._beacon_page_match(fr):
                return True
            time.sleep(.55)
        b.fail_snapshot("beacon_reward_stuck")
        return False

    def _beacon_wait_page(self, *, after_assist=False, timeout=12):
        """Wait for the beacon page to remain visible across several frames."""
        b = self.bot
        deadline = time.monotonic() + timeout
        reentered = False
        stable_hits = 0
        while time.monotonic() < deadline:
            fr = b.frame()
            if fr is None:
                stable_hits = 0
                time.sleep(0.25)
                continue
            # Reward overlays take priority over the beacon page underneath.
            if (b.vision.match(fr, "reward_continue") is not None
                    or b.vision.match(fr, "page_beacon_reward") is not None):
                stable_hits = 0
                if not self._close_beacon_reward():
                    return None
                time.sleep(0.35)
                continue
            page_match = self._beacon_page_match(fr)
            if page_match is not None:
                stable_hits += 1
                if stable_hits >= 3:
                    b.log("[BEACON] Beacon page stable across 3 frames")
                    learner = getattr(b, "learning", None)
                    if learner:
                        learner.outcome(fr, target=page_match.name)
                    return fr
                time.sleep(0.25)
                continue
            stable_hits = 0
            if after_assist and not reentered:
                if hasattr(b, "current_page"):
                    on_main = b.current_page(fr)[0] == "main"
                else:
                    on_main = b.is_main(fr)
                if on_main:
                    b.log("[BEACON] Assist returned to world; re-entering and waiting for page stability")
                    if not self._enter_beacon_from_main():
                        return None
                    reentered = True
                    deadline = time.monotonic() + timeout
                    stable_hits = 0
                    continue
            time.sleep(0.25)
        b.log("[STOP] Beacon page did not stabilize before timeout")
        b.fail_snapshot("beacon_assist_page_missing" if after_assist
                        else "beacon_page_not_ready")
        return None

    def _beacon_assist_once(self):
        """
        一键协助仅在明确蓝色可点击时点击，并复查是否还有协助。
        不因为“一键领取”变绿就提前领取。
        """
        b = self.bot
        fr = self._beacon_wait_page()

        for attempt in range(1, 5):
            if fr is None:
                return False
            blue = b.vision.match(fr, "beacon_assist_blue")
            if not blue:
                return True
            b.log(f"[BEACON] 蓝色一键协助，点击并复查#{attempt}")
            if not b.actions.tap_match(blue, fr.shape, "烽火台-一键协助"):
                b.fail_snapshot("beacon_assist_click_failed")
                return False
            time.sleep(0.8)
            fr = self._beacon_wait_page(after_assist=True)
            if fr is None:
                return False
        if fr is not None and b.vision.match(fr, "beacon_assist_blue") is None:
            return True
        b.fail_snapshot("beacon_assist_stuck")
        return False

    def _beacon_page_match(self, frame):
        if frame is None:
            return None
        for name in ("page_beacon_alt", "page_beacon"):
            try:
                match = self.bot.vision.match(frame, name)
            except KeyError:
                continue
            if match is not None:
                return match
        return None

    def _beacon_check_markers(self, fr):
        if fr is None:
            return []
        found = []
        for name, threshold in (
            ("beacon_check_badge", .74),
            ("beacon_check_badge_alt", .78),
        ):
            if name not in self.bot.vision.meta:
                continue
            for candidate in self._multi_template_candidates(
                fr, name, threshold, nms_distance=12
            ):
                _, x, y = candidate
                if not any((x-px)**2 + (y-py)**2 < 24**2 for _, px, py in found):
                    found.append(candidate)
        return found

    def _beacon_green_check_count(self, fr=None):
        """只计白圆底绿色完成勾，排除地图树木和玩家头像。"""
        fr = self.bot.frame() if fr is None else fr
        return len(self._beacon_check_markers(fr))

    def _beacon_compare_page(self, fr, reason):
        """Keep a page image and compare visible pins with the preceding scan."""
        if fr is None or self._beacon_page_match(fr) is None:
            return
        b = self.bot
        previous = getattr(self, "_beacon_previous_scan", None)
        current = self._beacon_all_events(fr)
        pending = self._beacon_unfinished_events(fr)
        checks = self._beacon_check_markers(fr)
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        path = b.debug_dir / f"{stamp}_beacon_page_{reason}.png"
        if previous is not None:
            old_frame = previous["frame"]
            map_change = None
            if old_frame.shape == fr.shape:
                old_map = old_frame[250:1120]
                new_map = fr[250:1120]
                delta = cv2.absdiff(old_map, new_map)
                map_change = float(np.mean(np.max(delta, axis=2) > 35))
            completed = sum(
                self._beacon_has_check_near(fr, old["x"], old["y"])
                for old in previous["pending"]
            )
            if (reason == "rescan" and len(pending) == len(previous["pending"])
                    and completed == 0 and len(checks) == previous["checks"]
                    and time.monotonic() - getattr(
                        self, "_beacon_last_snapshot_at", 0
                    ) < 60):
                return
            change_text = (
                f"地图像素变化={map_change:.1%} "
                if map_change is not None else "地图分辨率变化 "
            )
            b.log(
                f"[BEACON COMPARE] {reason}: 上次待办={len(previous['pending'])} "
                f"当前待办={len(pending)} 上次待办新增完成勾={completed} "
                f"当前图标={len(current)} 当前完成勾={len(checks)} "
                f"{change_text}"
            )
            b.log(
                f"[BEACON COMPARE] 对比截图：{path.name}"
            )
        else:
            b.log(f"[BEACON COMPARE] 保存基准图：{path.name}；待办={len(pending)}")
        cv2.imwrite(str(path), fr)
        self._beacon_last_snapshot_at = time.monotonic()
        self._beacon_previous_scan = {
            "pending": pending, "checks": len(checks), "frame": fr.copy()
        }

    def _beacon_has_check_near(self, fr, cx, cy):
        sx = fr.shape[1] / 726.0
        sy = fr.shape[0] / 1600.0
        if any(
            -5 * sx <= x - cx <= 60 * sx
            and -75 * sy <= y - cy <= 5 * sy
            for _, x, y in self._beacon_check_markers(fr)
        ):
            return True

        # A check badge can score lower in a single animated frame. Match it
        # only in the small area above and to the right of this specific pin.
        x1 = max(0, round(cx - 5 * sx))
        x2 = min(fr.shape[1], round(cx + 75 * sx))
        y1 = max(0, round(cy - 75 * sy))
        y2 = min(fr.shape[0], round(cy + 5 * sy))
        roi = fr[y1:y2, x1:x2]
        base = self.bot.vision.templates["beacon_check_badge"]
        template = cv2.resize(
            base,
            (max(4, round(base.shape[1] * sx)),
             max(4, round(base.shape[0] * sy))),
            interpolation=cv2.INTER_AREA,
        )
        if roi.shape[0] <= template.shape[0] or roi.shape[1] <= template.shape[1]:
            return False
        score = cv2.minMaxLoc(cv2.matchTemplate(
            roi, template, cv2.TM_CCOEFF_NORMED
        ))[1]
        return score >= .65

    def _multi_template_candidates(
        self,
        fr,
        template_name,
        threshold,
        nms_distance=36,
    ):
        """
        在模板search ROI中找多个候选，而不是只返回最佳匹配。
        """
        b = self.bot
        item = b.vision.meta[template_name]
        base_t = b.vision.templates[template_name]

        fh, fw = fr.shape[:2]
        sx = fw / 726.0
        sy = fh / 1600.0

        x1, y1, x2, y2 = item["search_roi_raw"]
        x1 = max(0, min(fw - 1, round(x1 * sx)))
        y1 = max(0, min(fh - 1, round(y1 * sy)))
        x2 = max(x1 + 1, min(fw, round(x2 * sx)))
        y2 = max(y1 + 1, min(fh, round(y2 * sy)))

        roi = fr[y1:y2, x1:x2]

        tw = max(4, round(base_t.shape[1] * sx))
        th = max(4, round(base_t.shape[0] * sy))

        t = cv2.resize(
            base_t,
            (tw, th),
            interpolation=cv2.INTER_AREA,
        )
        # The same task glyph is reused on blue, purple, and gold pins.
        # Compare these two emblems in grayscale so the pin's color variant
        # does not decide whether the task is found.
        if template_name in {"beacon_swords_inner", "beacon_skull_inner"}:
            roi = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
            t = cv2.cvtColor(t, cv2.COLOR_BGR2GRAY)

        if (
            t.shape[0] >= roi.shape[0]
            or t.shape[1] >= roi.shape[1]
        ):
            return []

        result = cv2.matchTemplate(
            roi,
            t,
            cv2.TM_CCOEFF_NORMED,
        )

        ys, xs = np.where(
            result >= float(threshold)
        )

        raw = []

        for yy, xx in zip(ys, xs):
            score = float(result[yy, xx])
            cx = x1 + xx + tw // 2
            cy = y1 + yy + th // 2
            raw.append(
                (score, int(cx), int(cy))
            )

        raw.sort(
            reverse=True,
            key=lambda x: x[0],
        )

        kept = []
        dist = max(
            20,
            int(round(nms_distance * fw / 447.0)),
        )

        for score, cx, cy in raw:
            duplicate = False

            for _, kx, ky in kept:
                if (
                    (cx-kx) * (cx-kx)
                    + (cy-ky) * (cy-ky)
                    < dist * dist
                ):
                    duplicate = True
                    break

            if not duplicate:
                kept.append(
                    (score, cx, cy)
                )

        return kept[:8]

    def _beacon_camp_candidates(self, fr):
        """Locate the white camp flag regardless of blue, purple, or gold pin color."""
        item = self.bot.vision.meta["beacon_camp_flag_shape"]
        base = cv2.cvtColor(
            self.bot.vision.templates["beacon_camp_flag_shape"],
            cv2.COLOR_BGR2GRAY,
        )
        fh, fw = fr.shape[:2]
        sx, sy = fw / 726.0, fh / 1600.0
        x1, y1, x2, y2 = item["search_roi_raw"]
        x1, x2 = round(x1 * sx), round(x2 * sx)
        y1, y2 = round(y1 * sy), round(y2 * sy)
        hsv = cv2.cvtColor(fr[y1:y2, x1:x2], cv2.COLOR_BGR2HSV)
        white = cv2.inRange(hsv, (0, 0, 140), (179, 120, 255))
        template = cv2.resize(
            base,
            (max(4, round(base.shape[1] * sx)),
             max(4, round(base.shape[0] * sy))),
            interpolation=cv2.INTER_NEAREST,
        )
        if (white.shape[0] <= template.shape[0]
                or white.shape[1] <= template.shape[1]):
            return []
        result = cv2.matchTemplate(white, template, cv2.TM_CCOEFF_NORMED)
        ys, xs = np.where(result >= item["threshold"])
        candidates = sorted(
            ((float(result[y, x]),
              int(x1 + x + template.shape[1] // 2),
              int(y1 + y + template.shape[0] // 2))
             for y, x in zip(ys, xs)),
            reverse=True,
        )
        found = []
        distance = round(45 * sx)
        for score, cx, cy in candidates:
            if any((cx-x)**2 + (cy-y)**2 < distance**2 for _, x, y in found):
                continue
            found.append((score, cx, cy))
            if len(found) >= 8:
                break
        return found

    def _beacon_all_events(self, fr=None):
        """
        返回当前烽火台地图上能识别到的全部事件图标，
        不管它是否已经有绿色✓。

        用途：
        - 动态记录本轮实际事件总数。
        - 事件数量不是固定4个，可能是5个、6个或更多。

        进行中的半透明事件有时模板分数会下降，
        因此调用方会保存“本轮观察到的最大事件数”，
        不会因为某一帧暂时少识别几个就把总数变小。
        """
        b = self.bot
        fr = b.frame() if fr is None else fr

        if fr is None:
            return []

        specs = [
            (
                "rally",
                "beacon_camp_flag_shape",
                0.70,
            ),
            (
                "rally",
                "beacon_event_rally_flag",
                0.74,
            ),
            (
                "resource",
                "beacon_event_resource_yellow",
                0.72,
            ),
            (
                "resource",
                "beacon_event_resource_purple",
                0.72,
            ),
        ]

        specs.extend([
            ("resource", "beacon_resource_yellow_inner", .70),
            ("resource", "beacon_resource_purple_inner", .70),
            ("resource", "beacon_resource_blue_inner", .70),
            ("resource", "beacon_resource_gold_live", .78),
            ("excavation", "beacon_excavation_event", .86),
        ])
        # 剑/骷髅也计入总数，不把这些图标当作集结旗帜点击。
        specs.extend([
            ("pending", "beacon_swords_inner", .78),
            ("pending", "beacon_skull_inner", .78),
        ])

        events = []

        for event_type, name, threshold in specs:
            candidates = (self._beacon_camp_candidates(fr)
                          if name == "beacon_camp_flag_shape" else
                          self._multi_template_candidates(fr, name, threshold))

            for score, cx, cy in candidates:
                duplicate = False

                for e in events:
                    dx = e["x"] - cx
                    dy = e["y"] - cy

                    if dx*dx + dy*dy < 45*45:
                        duplicate = True

                        if score > e["score"]:
                            e.update(
                                type=event_type,
                                template=name,
                                score=score,
                                x=cx,
                                y=cy,
                            )
                        break

                if not duplicate:
                    events.append(
                        {
                            "type": event_type,
                            "template": name,
                            "score": score,
                            "x": cx,
                            "y": cy,
                        }
                    )

        # 已完成图标可能改变样式，独立完成勾仍代表一个事件。
        for _, x, y in self._beacon_check_markers(fr):
            scale = fr.shape[1] / 726.0
            if not any(-5*scale <= x-e["x"] <= 60*scale
                       and -75*scale <= y-e["y"] <= 5*scale for e in events):
                events.append({"type": "completed", "template": "beacon_check_badge",
                               "score": 1.0, "x": x-round(25*scale),
                               "y": y+round(33*scale)})

        return events

    def _beacon_event_is_active(self, fr, cx, cy):
        """fht.mp4：已派遣图标变淡，但相关系数仍很高，需独立检查颜色。"""
        # A red clock badge is the event's remaining-time indicator. It means
        # this is still an expiring task, even if the pin body is translucent;
        # do not confuse it with a faded, already-dispatched resource pin.
        if self._beacon_has_expiry_clock(fr, cx, cy):
            return True
        if self._beacon_dispatched_blue_pin(fr, cx, cy):
            return False
        # The user confirmed blue and purple crossed-pick pins are the same
        # resource-task type. Blue pins may lack the red clock yet remain
        # actionable; recognize their cyan fill separately from faded pins.
        sx, sy = fr.shape[1] / 726.0, fr.shape[0] / 1600.0
        rx, ry = max(1, round(18*sx)), max(1, round(18*sy))
        color_patch = fr[max(0,cy-ry):min(fr.shape[0],cy+ry),
                         max(0,cx-rx):min(fr.shape[1],cx+rx)]
        if color_patch.size:
            hsv_pin = cv2.cvtColor(color_patch, cv2.COLOR_BGR2HSV)
            cyan = cv2.inRange(hsv_pin, (82, 90, 125), (112, 255, 255))
            if cv2.countNonZero(cyan) >= max(12, round(cyan.size*.08)):
                return True
        rx, ry = max(1, round(20 * sx)), max(1, round(20 * sy))
        patch = fr[max(0, cy-ry):cy+ry, max(0, cx-rx):cx+rx]
        if not patch.size:
            return False
        hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
        # 日/夜录像的亮图标均保留高饱和底色和亮白图案；半透明蓝色
        # 矿点也必须过滤，不能仅检查“还有蓝色”或模板是否相似。
        return bool(np.percentile(hsv[:, :, 1], 75) >= 180
                    and np.percentile(hsv[:, :, 2], 90) >= 235)

    def _beacon_dispatched_blue_pin(self, fr, cx, cy):
        """Recognize the faded blue pin near this candidate, without searching other events."""
        if fr is None:
            return False
        template = self.bot.vision.templates.get("beacon_dispatched_blue_pin")
        if template is None:
            return False
        sx, sy = fr.shape[1] / 726.0, fr.shape[0] / 1600.0
        tw, th = round(template.shape[1]*sx), round(template.shape[0]*sy)
        center_x, center_y = round(cx), round(cy + 7*sy)
        radius_x, radius_y = round(25*sx), round(25*sy)
        x1 = max(0, center_x - tw//2 - radius_x)
        y1 = max(0, center_y - th//2 - radius_y)
        x2 = min(fr.shape[1], center_x + tw//2 + radius_x)
        y2 = min(fr.shape[0], center_y + th//2 + radius_y)
        patch = fr[y1:y2, x1:x2]
        resized = cv2.resize(template, (tw, th), interpolation=cv2.INTER_AREA)
        if patch.shape[0] < th or patch.shape[1] < tw:
            return False
        score = cv2.minMaxLoc(cv2.matchTemplate(
            patch, resized, cv2.TM_CCOEFF_NORMED))[1]
        if score >= .84:
            self.bot.log(f"[BEACON] 已派遣蓝色图标 frame=({cx},{cy}) score={score:.3f}")
            return True
        return False

    @staticmethod
    def _beacon_has_expiry_clock(fr, cx, cy):
        """Find the small red clock badge at the event pin's upper right."""
        sx, sy = fr.shape[1] / 726.0, fr.shape[0] / 1600.0
        x1, x2 = round((cx + 15) * sx), round((cx + 43) * sx)
        y1, y2 = round((cy - 47) * sy), round((cy - 14) * sy)
        patch = fr[max(0, y1):min(fr.shape[0], y2),
                   max(0, x1):min(fr.shape[1], x2)]
        if patch.size == 0:
            return False
        hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
        red = (cv2.inRange(hsv, (0, 90, 70), (12, 255, 255)) |
               cv2.inRange(hsv, (168, 90, 70), (180, 255, 255)))
        return cv2.countNonZero(red) >= max(12, round(red.size * .04))

    def _beacon_in_progress_events(self, fr=None):
        """Find faded resource pins that are already being gathered."""
        b = self.bot
        fr = b.frame() if fr is None else fr
        if fr is None:
            return []
        events = []
        for name in (
            "beacon_event_resource_yellow",
            "beacon_event_resource_purple",
            "beacon_resource_yellow_inner",
            "beacon_resource_purple_inner",
            "beacon_resource_blue_inner",
        ):
            if name not in b.vision.meta:
                continue
            threshold = .58 if name.startswith("beacon_event_") else .62
            for score, cx, cy in self._multi_template_candidates(
                fr, name, threshold
            ):
                dispatched_blue = self._beacon_dispatched_blue_pin(fr, cx, cy)
                # Countdown-marked pins remain actionable; don't list them as
                # already dispatched merely because their bodies look dim.
                if self._beacon_has_expiry_clock(fr, cx, cy):
                    continue
                if not dispatched_blue and self._beacon_event_is_active(fr, cx, cy):
                    continue
                if self._beacon_has_check_near(fr, cx, cy):
                    continue
                if any((cx-e["x"])**2 + (cy-e["y"])**2 < 45**2
                       for e in events):
                    continue
                events.append({"type": "resource", "template": name,
                               "score": score, "x": cx, "y": cy})
        return events

    def _beacon_unfinished_events(self, fr=None):
        """
        返回当前仍然“正常亮着、没有✓”的事件。

        rally:
            紫色白旗 -> 集结任务

        resource:
            黄色/紫色小锤子 -> 采集任务

        已完成✓会被绿色检测过滤。
        进行中的半透明图标额外按颜色过滤，不能靠模板分数判断。
        """
        b = self.bot
        fr = b.frame() if fr is None else fr

        if fr is None:
            return []

        specs = [
            (
                "rally",
                "beacon_camp_flag_shape",
                0.70,
            ),
            (
                "rally",
                "beacon_event_rally_flag",
                0.80,
            ),
            (
                "resource",
                "beacon_event_resource_yellow",
                0.72,
            ),
            (
                "resource",
                "beacon_event_resource_purple",
                0.72,
            ),
        ]

        specs.extend([
            ("resource", "beacon_resource_yellow_inner", .70),
            ("resource", "beacon_resource_purple_inner", .70),
            ("resource", "beacon_resource_blue_inner", .70),
            ("resource", "beacon_resource_gold_live", .78),
            ("excavation", "beacon_excavation_event", .86),
            # Crossed-swords pins open the distinct 讨平山贼 task card.
            ("bandit_suppression", "beacon_swords_inner", .78),
            # Skull pins open 流寇 targets; they require an army march slot.
            ("bandit_raid", "beacon_skull_inner", .78),
        ])

        events = []

        for event_type, name, threshold in specs:
            candidates = (self._beacon_camp_candidates(fr)
                          if name == "beacon_camp_flag_shape" else
                          self._multi_template_candidates(fr, name, threshold))

            for score, cx, cy in candidates:
                if self._beacon_has_check_near(
                    fr,
                    cx,
                    cy,
                ):
                    continue
                if (event_type == "resource"
                        and not self._beacon_has_expiry_clock(fr, cx, cy)
                        and self._beacon_dispatched_blue_pin(fr, cx, cy)):
                    continue
                if not self._beacon_event_is_active(fr, cx, cy):
                    continue

                # 跨模板去重
                duplicate = False

                for e in events:
                    dx = e["x"] - cx
                    dy = e["y"] - cy

                    if dx*dx + dy*dy < 45*45:
                        duplicate = True

                        if score > e["score"]:
                            e.update(
                                type=event_type,
                                template=name,
                                score=score,
                                x=cx,
                                y=cy,
                            )
                        break

                if not duplicate:
                    events.append(
                        {
                            "type": event_type,
                            "template": name,
                            "score": score,
                            "x": cx,
                            "y": cy,
                        }
                    )

        # 优先发起3分钟集结，让长计时先跑；
        # 再并行派采集。
        events.sort(
            key=lambda e: (
                {"rally": 0, "resource": 1, "bandit_suppression": 2,
                 "bandit_raid": 3, "combat": 4, "excavation": 5}[e["type"]],
                -e["score"],
            )
        )

        return events

    def _enter_beacon_from_main(self):
        b = self.bot

        # 已在烽火台时直接复用，避免加载抖动触发多余的退出和进入。
        fr = b.frame()
        if (fr is not None and self._beacon_page_match(fr) is not None
                and b.vision.match(fr, "reward_continue") is None
                and b.vision.match(fr, "page_beacon_reward") is None):
            if getattr(self, "_beacon_hold_mode", None) is not None:
                return True
            if not self._tap_game_back("烽火台-返回主界面核对旗帜") or not b.wait_main(5):
                return False
        if not b.ensure_main():
            return False
        hold_mode = self._beacon_check_hold_banner()
        if hold_mode is None:
            return False
        self._beacon_hold_mode = hold_mode

        # Retry inside the entry flow, including --task beacon. A successful
        # ADB tap only confirms delivery, not that the game opened the page.
        offsets = ((0, 0), (-.12, .12), (.12, .12))
        for attempt, (dx, dy) in enumerate(offsets, 1):
            fr = b.frame()
            if fr is None:
                time.sleep(.25)
                continue
            if self._beacon_page_match(fr):
                return True
            if not b.is_main(fr):
                # A transition or another popup may now cover the entry.
                # Wait for the destination without clicking through it.
                if b.wait_template_any(("page_beacon_alt", "page_beacon"), 3):
                    return True
                b.fail_snapshot("beacon_entry_page_changed")
                return False
            entries = [b.vision.match(fr, name) for name in
                       ("main_beacon_entry", "main_beacon_entry_wechat")]
            entry = max((m for m in entries if m),
                        key=lambda m: m.score, default=None)
            if entry:
                x1, y1, x2, y2 = entry.bbox
                x = round(entry.center[0] + dx * (x2 - x1))
                y = round(entry.center[1] + dy * (y2 - y1))
                b.log(f"[BEACON ENTRY] 新帧定位#{attempt} {entry.name} "
                      f"score={entry.score:.3f} frame=({x},{y})")
            else:
                # Only use the calibrated icon area on a confirmed main page.
                h, w = fr.shape[:2]
                x = round(w * (48 + dx * 60) / 726)
                y = round(h * (1268 + dy * 60) / 1600)
                b.log(f"[BEACON ENTRY] 主城入口区域重试#{attempt} frame=({x},{y})")
            if not b.actions.tap_frame(
                    x, y, fr.shape, f"进入烽火台#{attempt}"):
                time.sleep(.25)
                continue
            if b.wait_template_any(("page_beacon_alt", "page_beacon"), 5):
                return True
            b.log(f"[BEACON ENTRY] 第{attempt}次点击未确认打开；重新截图检查页面")

        # Check one last fresh frame in case the final tap loaded slowly.
        final_frame = b.frame()
        if self._beacon_page_match(final_frame):
            return True
        b.fail_snapshot("beacon_entry_frame_missing" if final_frame is None
                        else "beacon_page_not_confirmed")
        b.log("[BEACON] 入口重新定位并点击3次后仍未确认打开")
        return False

    def _beacon_select_event_and_go(
        self,
        event,
    ):
        b = self.bot
        fr = b.frame()

        if fr is None or not self._beacon_page_match(fr):
            b.fail_snapshot("beacon_event_page_missing")
            return False

        # After dispatch the selected resource keeps a blue "前往" bubble on
        # the map. Its artwork can match a resource-pin template on another
        # account. Check the current pixels before treating that bubble as a
        # fresh task; the bottom card then explicitly says "进行中".
        if event["type"] == "resource":
            if (not self._beacon_has_expiry_clock(fr, event["x"], event["y"])
                    and self._beacon_dispatched_blue_pin(fr, event["x"], event["y"])):
                b.log("[BEACON] 当前候选是已派遣蓝色采集图标；跳过并检查其他事件")
                return "already_in_progress"
            from local_ocr import read_text
            sx, sy = fr.shape[1] / 726.0, fr.shape[0] / 1600.0
            cx, cy = event["x"] / sx, event["y"] / sy
            try:
                nearby = read_text(fr, (
                    max(0, round(cx - 85)), max(0, round(cy - 80)),
                    min(726, round(cx + 85)), min(1600, round(cy + 80)),
                ), scale=3)["text"]
                if "前往" in re.sub(r"\s+", "", nearby):
                    card = read_text(fr, (500, 1180, 725, 1380), scale=3)["text"]
                    if ("进行中" in re.sub(r"\s+", "", card) or
                            b.vision.match(fr, "beacon_resource_in_progress")):
                        b.log("[BEACON] 当前候选是已派遣事件的前往浮标，详情卡确认进行中；跳过")
                        return "already_in_progress"
            except (RuntimeError, OSError, subprocess.TimeoutExpired, ValueError) as exc:
                b.log(f"[BEACON] 候选浮标OCR暂不可用：{exc}")

        b.log(
            f"[BEACON EVENT] 点击 "
            f"{event['type']} "
            f"score={event['score']:.3f} "
            f"frame=({event['x']},{event['y']})"
        )

        detail_name = {
            "resource": "beacon_resource_detail",
            "excavation": "beacon_excavation_detail",
            "rally": "beacon_camp_detail",
        }.get(event["type"])
        # A pin can be visually detected while the tap lands just beside its
        # hitbox. Try a small, bounded cross around the detected center. Each
        # attempt uses a fresh frame and stops as soon as the selected state is
        # confirmed, so clustered neighboring events are not tapped blindly.
        offsets = ((0, 0), (0, -10), (0, 10), (-10, 0), (10, 0))
        detail = go = None
        confirmed_combat_popup = False
        for attempt, (ox, oy) in enumerate(offsets, 1):
            fr = b.frame()
            if fr is None:
                continue
            if not self._beacon_page_match(fr):
                if (b.vision.match(fr, "reward_continue") or
                        b.vision.match(fr, "page_beacon_reward")):
                    b.log("[BEACON] 事件点击后直接出现奖励页；关闭奖励并复查烽火台状态")
                    if not self._close_beacon_reward() or self._beacon_wait_page() is None:
                        return False
                    return "already_completed"
                # Combat pins may open their target popup directly.
                if event["type"] in {"combat", "bandit_suppression", "bandit_raid"} and b.wait_template(
                    "beacon_bandit_popup", 0.4
                ) is not None:
                    confirmed_combat_popup = True
                    break
                b.fail_snapshot("beacon_event_page_changed")
                return False
            px = event["x"] + round(ox * fr.shape[1] / 726)
            py = event["y"] + round(oy * fr.shape[0] / 1600)
            if not b.actions.tap_frame(
                px, py, fr.shape,
                f"烽火台事件-{event['type']}-尝试{attempt}",
            ):
                continue
            # Prefer the event-specific card; generic Go anchors can match
            # unrelated map decorations before a selection is made.
            detail = b.wait_template(detail_name, 0.7) if detail_name else None
            go = b.wait_template("beacon_go_button", 0.7) if (
                detail or event["type"] in {"combat", "bandit_suppression", "bandit_raid"}
            ) else None
            if detail is not None or go is not None:
                b.log(f"[BEACON] 点击位置第{attempt}次确认选中")
                break
            if event["type"] in {"combat", "bandit_suppression", "bandit_raid"} and b.wait_template(
                "beacon_bandit_popup", 0.3
            ) is not None:
                confirmed_combat_popup = True
                break
            if attempt < len(offsets):
                b.log(
                    f"[BEACON] 第{attempt}次点击无选中反馈，"
                    f"微调位置继续尝试 ({px},{py})"
                )

        if confirmed_combat_popup:
            return True

        if go is None and detail is None:
            b.log(
                f"[STOP] {len(offsets)}次邻近点击均未确认事件选中/前往"
            )
            b.fail_snapshot(
                "beacon_event_go_missing"
            )
            return False

        fr = b.frame()

        if fr is None or not self._beacon_page_match(fr):
            if fr is not None and (b.vision.match(fr, "reward_continue") or
                                   b.vision.match(fr, "page_beacon_reward")):
                b.log("[BEACON] 事件无‘前往’但已弹出奖励页；关闭并复查")
                if not self._close_beacon_reward() or self._beacon_wait_page() is None:
                    return False
                return "already_completed"
            b.fail_snapshot("beacon_event_page_changed")
            return False
        # 详情卡确认点中的确是该类型，再点它旁边浮出的“前往”。
        # 有其他已选择目标时，“前往”不在刚点的事件旁，不能误点。
        if go is not None:
            dx = (go.center[0] - event["x"]) * 726 / fr.shape[1]
            dy = (go.center[1] - event["y"]) * 1600 / fr.shape[0]
            clicked = bool(dx*dx + dy*dy <= 200**2 and
                           b.actions.tap_match(go, fr.shape, "烽火台事件-前往"))
        else:
            x, y = detail.center
            clicked = bool(b.actions.tap_frame(
                x, y, fr.shape, "烽火台事件-详情卡前往"))
        if not clicked:
            b.fail_snapshot("beacon_event_go_click_failed")
            return False

        return True

    def _beacon_dispatch_resource(self):
        """Select the resource marker, collect it, and send one march."""
        b = self.bot
        popup = b.wait_template_any(("world_resource_popup_alt", "world_resource_popup"), 6)
        collect = None
        if popup is None:
            # Resource detail cards vary in height between game builds. Their
            # title crop can miss even when the unique Collect button is clear.
            collect = b.wait_template_any(
                ("resource_collect_label", "resource_collect_button_alt",
                 "resource_collect_button"), 3
            )
            if collect is None:
                b.fail_snapshot("beacon_resource_popup_missing")
                return False
            b.log(
                f"[VISION] 矿点弹窗标题未命中；采集按钮确认详情页 "
                f"template={collect.name} score={collect.score:.3f}"
            )

        if collect is None:
            collect = b.wait_template_any(
                ("resource_collect_label", "resource_collect_button_alt",
                 "resource_collect_button"), 3
            )
        fr = b.frame()
        if collect is None or fr is None or not b.actions.tap_match(
            collect, fr.shape, "resource collect"
        ):
            b.fail_snapshot("beacon_resource_collect_missing")
            return False

        march = b.wait_template_any(
            ("page_resource_march_alt", "page_resource_march", "beacon_march_controls"),
            5,
        )
        if march is None:
            b.fail_snapshot("beacon_resource_march_missing")
            return False

        self._record_confirmed_visual(collect.name, fr, collect)

        if not self._beacon_depart_march("采集", "beacon_resource"):
            return False
        b.log("[OK] 采集队列已派出，立即返回烽火台处理下一个事件")
        return True

    def _beacon_depart_march(self, label, diagnostic):
        """沿用视频中的当前编队出征；按钮文字不包含会变化的倒计时。"""
        b = self.bot
        depart = b.wait_template_any(("resource_depart_button", "beacon_depart_text"), 3)
        fr = b.frame()
        if depart is None or fr is None or not b.actions.tap_match(
            depart, fr.shape, f"烽火台-{label}出征"
        ):
            b.fail_snapshot(f"{diagnostic}_depart_missing")
            return False

        if not b.wait_main(6):
            b.fail_snapshot(f"{diagnostic}_depart_failed")
            return False
        # 大地图在弹窗下仍可见，必须排除未关闭的目标/编队窗口。
        fr = b.frame()
        if fr is None or any(b.vision.match(fr, name) for name in (
            "world_resource_popup_alt", "world_resource_popup",
            "beacon_excavation_popup", "beacon_march_controls",
        )):
            b.fail_snapshot(f"{diagnostic}_depart_unconfirmed")
            return False
        return True

    def _beacon_dispatch_excavation(self):
        """卷轴事件：烽火秘藏 → 挖掘 → 保持当前编队 → 出征。"""
        b = self.bot
        if not b.wait_template("beacon_excavation_popup", 6):
            b.fail_snapshot("beacon_excavation_popup_missing")
            return False
        dig = b.wait_template("beacon_excavation_button", 3)
        fr = b.frame()
        if not dig or fr is None or not b.actions.tap_match(
                dig, fr.shape, "烽火秘藏-挖掘"):
            b.fail_snapshot("beacon_excavation_button_missing")
            return False
        if not b.wait_template("beacon_march_controls", 5):
            b.fail_snapshot("beacon_excavation_march_missing")
            return False
        if not self._beacon_depart_march("秘藏挖掘", "beacon_excavation"):
            return False
        b.log("[OK] 秘藏挖掘队列已派出，继续复查烽火台")
        return True

    def _beacon_find_zhurong(self):
        b = self.bot

        for attempt in range(6):
            fr = b.frame()

            m = b.vision.match(
                fr,
                "hero_zhurong",
            )

            if m:
                b.log(
                    f"[OK] 找到祝融 "
                    f"(滚动次数={attempt})"
                )
                b.actions.tap_match(
                    m,
                    fr.shape,
                    "选择祝融",
                )
                return True

            # 仅在武将选择列表区域向上滑，
            # 相当于浏览更下面的武将。
            h, w = fr.shape[:2]

            b.actions.swipe_frame(
                int(w * 0.50),
                int(h * 0.78),
                int(w * 0.50),
                int(h * 0.57),
                fr.shape,
                duration_ms=320,
                label="武将列表向下浏览",
            )

            time.sleep(0.45)

        b.log(
            "[STOP] 滑动6次仍未找到祝融"
        )
        b.fail_snapshot(
            "beacon_zhurong_not_found"
        )
        return False

    def _beacon_dispatch_rally(self):
        """Camp flag -> world camp -> 3-minute rally -> current march -> depart 10."""
        b = self.bot
        popup = b.wait_template_any(
            ("beacon_camp_popup", "world_rally_popup"), 6
        )
        if popup is None:
            b.fail_snapshot("beacon_camp_popup_missing")
            return False

        attack = b.wait_template("rally_attack_button", 3)
        fr = b.frame()
        attack_fr = fr
        popup_fr = fr
        if attack is None or fr is None:
            score_fn = getattr(b.vision, "score", None)
            if callable(score_fn) and fr is not None:
                try:
                    score = score_fn(fr, "rally_attack_button")
                    threshold = b.vision.meta["rally_attack_button"]["threshold"]
                    b.log(
                        f"[VISION] rally_attack_button miss score={score:.3f} "
                        f"threshold={threshold:.2f}"
                    )
                except Exception as exc:
                    b.log(f"[VISION] rally_attack_button diagnostic failed: {exc!r}")
            b.fail_snapshot("beacon_camp_attack_missing")
            return False
        if not b.actions.tap_match(attack, fr.shape, "营寨-集结攻击"):
            b.fail_snapshot("beacon_camp_attack_missing")
            return False

        modal = b.wait_template_any(
            ("beacon_march_queue_modal", "beacon_rally_modal",
             "page_rally_time_modal"), 5
        )
        if modal is None:
            b.fail_snapshot("beacon_rally_time_modal_missing")
            return False
        if modal.name == "beacon_march_queue_modal":
            b.log("[BEACON] 出征队列已满，关闭提示并跳过此营寨，继续其他任务")
            fr = b.frame()
            close = (b.vision.match(fr, "beacon_march_queue_close")
                     if fr is not None else None)
            if close is None or not b.actions.tap_match(
                close, fr.shape, "出征队列满-关闭提示"
            ):
                b.fail_snapshot("beacon_march_queue_modal_close_failed")
                return False
            if not b.wait_template_gone("beacon_march_queue_modal", 3):
                b.fail_snapshot("beacon_march_queue_modal_stuck")
                return False
            # The camp detail card remains below the queue warning. Tap a
            # known empty map area to dismiss that card before re-entering the
            # beacon event map.
            fr = b.frame()
            if fr is None:
                b.fail_snapshot("beacon_march_queue_map_missing")
                return False
            h, w = fr.shape[:2]
            if not b.actions.tap_frame(
                round(w * .52), round(h * .74), fr.shape,
                "出征队列满-关闭营寨详情"
            ):
                b.fail_snapshot("beacon_march_queue_camp_close_failed")
                return False
            time.sleep(.25)
            fr = b.frame()
            if fr is None or any(b.vision.match(fr, name) for name in (
                "beacon_camp_popup", "world_rally_popup",
            )):
                b.fail_snapshot("beacon_march_queue_camp_stuck")
                return False
            if getattr(self, "_beacon_queue_slot_pending", False):
                b.log("[BEACON] 采集队伍仍在返回；不重复召回，稍后重试")
                return "queue_returning"
            try:
                _, used, capacity, rows = self._march_state()
                collecting = sum("采集中" in state for state, _ in rows)
            except (ValueError, RuntimeError, OSError) as exc:
                b.log(f"[BEACON] 无法确认当前行军状态，暂缓营寨：{exc}")
                return "skipped_queue_full"
            if used >= capacity and collecting:
                b.log(f"[BEACON] 行军{used}/{capacity}，发现采集队伍；召回1支并继续其他任务")
                if self.gather_recall(max_recall=1, only_if_full=True):
                    self._beacon_queue_slot_pending = True
                    self._beacon_queue_eta_seconds = getattr(
                        self, "_last_march_return_seconds", None
                    )
                    return "queue_returning"
                else:
                    b.log("[BEACON] 采集队伍召回未确认，继续处理其他任务")
            else:
                b.log(f"[BEACON] 行军{used}/{capacity}，没有可回收的采集队列")
            return "skipped_queue_full"
        self._record_confirmed_visual(popup.name, popup_fr, popup)
        self._record_confirmed_visual("rally_attack_button", attack_fr, attack)
        modal_fr = b.frame()
        three_min = b.wait_template_any(
            ("beacon_rally_3min", "rally_3min_selected"), 3
        )
        if three_min is None:
            b.fail_snapshot("beacon_rally_3min_not_selected")
            return False

        start_button = b.wait_template_any(
            ("beacon_rally_start_label", "rally_start_button"), 3
        )
        fr = b.frame()
        start_fr = fr
        if start_button is None or fr is None or not b.actions.tap_match(
            start_button, fr.shape, "营寨-发起3分钟集结"
        ):
            b.fail_snapshot("beacon_rally_start_missing")
            return False

        if b.wait_template("beacon_march_controls", 5) is None:
            b.fail_snapshot("beacon_rally_march_missing")
            return False
        self._record_confirmed_visual(modal.name, modal_fr, modal)
        self._record_confirmed_visual(three_min.name, modal_fr, three_min)
        self._record_confirmed_visual(start_button.name, start_fr, start_button)
        depart = b.wait_template_any(
            ("beacon_rally_depart_10_label", "rally_depart_10"), 3
        )
        fr = b.frame()
        depart_fr = fr
        if depart is None or fr is None or not b.actions.tap_match(
            depart, fr.shape, "营寨-保留当前编队出征10"
        ):
            b.fail_snapshot("beacon_rally_depart10_missing")
            return False

        if not b.wait_main(6):
            b.fail_snapshot("beacon_rally_depart_failed")
            return False
        fr = b.frame()
        if fr is None or b.vision.match(fr, "beacon_march_controls") is not None:
            b.fail_snapshot("beacon_rally_depart_unconfirmed")
            return False
        self._record_confirmed_visual(depart.name, depart_fr, depart)
        b.log("[OK] 营寨3分钟集结已出征；继续处理烽火台其他事件")
        return True

    def _beacon_dispatch_battle(self):
        """Open a bandit target, start its battle, then leave the battle view."""
        b = self.bot
        target = b.wait_template_any(("beacon_bandit_popup",), 6)
        if not target:
            b.fail_snapshot("beacon_bandit_popup_missing")
            return False
        attack = b.wait_template("beacon_bandit_attack_button", 4)
        fr = b.frame()
        if not attack or fr is None or not b.actions.tap_match(
            attack, fr.shape, "beacon bandit attack"
        ):
            b.fail_snapshot("beacon_bandit_attack_missing")
            return False
        start = b.wait_template("beacon_battle_start_button", 8)
        fr = b.frame()
        if not start or fr is None or not b.actions.tap_match(
            start, fr.shape, "beacon battle start"
        ):
            b.fail_snapshot("beacon_battle_start_missing")
            return False
        if not b.wait_template("beacon_battle_timer", 8):
            b.fail_snapshot("beacon_battle_start_unconfirmed")
            return False
        if not self._tap_game_back("beacon battle exit") or not b.wait_main(6):
            b.fail_snapshot("beacon_battle_exit_failed")
            return False
        b.log("[OK] Beacon battle started; exited to continue remaining events")
        return True

    def _beacon_confirm_battle_return(self, event, label):
        """Accept either return destination, verifying the completed pin on Beacon."""
        b = self.bot
        deadline = time.monotonic() + 12
        stable_beacon = 0
        while time.monotonic() < deadline:
            fr = b.frame()
            if fr is None:
                time.sleep(.3)
                continue
            if self._beacon_page_match(fr) is not None:
                stable_beacon += 1
                if stable_beacon >= 2 and self._beacon_has_check_near(
                    fr, event["x"], event["y"]
                ):
                    b.log(f"[OK] {label}战斗胜利，烽火台任务已显示完成勾")
                    return True
            else:
                stable_beacon = 0
                if b.is_main(fr):
                    b.log(f"[OK] {label}战斗胜利并返回主地图；继续复查烽火台")
                    return True
            time.sleep(.3)
        b.fail_snapshot("beacon_battle_return_unconfirmed")
        return False

    def _beacon_dispatch_bandit_suppression(self, event):
        """讨平山贼: tap 讨伐, start the preselected formation, claim victory return."""
        b = self.bot
        challenge = b.wait_template("beacon_subjugate_button", 8)
        fr = b.frame()
        if challenge is None or fr is None or not b.actions.tap_match(
            challenge, fr.shape, "讨平山贼-讨伐"
        ):
            b.fail_snapshot("beacon_subjugate_button_missing")
            return False

        start = b.wait_template("beacon_bandit_formation_start", 12)
        fr = b.frame()
        if start is None or fr is None or not b.actions.tap_match(
            start, fr.shape, "讨平山贼-开始战斗"
        ):
            b.fail_snapshot("beacon_bandit_formation_start_missing")
            return False

        # The battle runs automatically. Wait for the victory result instead
        # of backing out while the fight is still active.
        victory = b.wait_template("beacon_bandit_victory_return", 180)
        fr = b.frame()
        if victory is None or fr is None or not b.actions.tap_match(
            victory, fr.shape, "讨平山贼-胜利后返回"
        ):
            b.fail_snapshot("beacon_bandit_victory_return_missing")
            return False
        return self._beacon_confirm_battle_return(event, "讨平山贼")

    def _beacon_dispatch_bandit_raid(self, event):
        """征伐流寇: attack and send the current march formation."""
        b = self.bot
        title = b.wait_template("beacon_raider_title", 6)
        attack = b.wait_template("beacon_raid_attack_button", 6)
        fr = b.frame()
        if title is None or attack is None or fr is None:
            b.fail_snapshot("beacon_raider_popup_missing")
            return False
        if not b.actions.tap_match(attack, fr.shape, "征伐流寇-攻击"):
            b.fail_snapshot("beacon_raid_attack_failed")
            return False

        depart = b.wait_template_any(
            ("beacon_raid_depart_button", "resource_depart_button", "beacon_depart_text"), 6
        )
        fr = b.frame()
        if depart is None or fr is None or not b.actions.tap_match(
            depart, fr.shape, "征伐流寇-出征"
        ):
            b.fail_snapshot("beacon_raid_depart_missing")
            return False
        if not b.wait_main(8):
            b.fail_snapshot("beacon_raid_depart_unconfirmed")
            return False
        b.log("[OK] 征伐流寇已出征；等待行军完成后再领取奖励")
        return True

    def _beacon_dispatch_event(self, event):
        handlers = {
            "resource": self._beacon_dispatch_resource,
            "excavation": self._beacon_dispatch_excavation,
            "rally": self._beacon_dispatch_rally,
            "combat": self._beacon_dispatch_battle,
            "bandit_suppression": self._beacon_dispatch_bandit_suppression,
            "bandit_raid": self._beacon_dispatch_bandit_raid,
        }
        handler = handlers.get(event["type"])
        if handler is None:
            self.bot.log(f"[STOP] 未知烽火台事件类型：{event['type']}，等待补充演示")
            self.bot.fail_snapshot("beacon_unsupported_event")
            return False
        selected = self._beacon_select_event_and_go(event)
        if selected in ("already_completed", "already_in_progress"):
            return selected
        if not selected:
            return False
        if event["type"] in ("bandit_suppression", "bandit_raid"):
            return handler(event)
        return handler()

    def _beacon_check_hold_banner(self):
        """Wednesday/Sunday AND the user-confirmed main-screen banner."""
        b = self.bot
        if datetime.datetime.now().weekday() not in (2, 6):
            return False
        present = absent = 0
        for _ in range(3):
            frame = b.frame()
            if frame is None or not b.is_main(frame):
                break
            match = b.vision.match(frame, "main_beacon_hold_banner", threshold=.60)
            score = match.score if match else 0.0
            b.log(f"[BEACON FLAG] 主界面右上旗帜 score={score:.3f}")
            if score >= .84:
                present += 1
            elif score < .60:
                absent += 1
            if present >= 2:
                return True
            time.sleep(.25)
        if absent == 3:
            return False
        b.fail_snapshot("beacon_main_flag_unknown")
        b.log("[BEACON] 旗帜状态不确定，停止领取并保留现场")
        return None

    def _beacon_safe_assist_only_check(self, fr=None):
        """
        周三/周日：主界面右上已确认指定旗帜时，只执行协助。
        任何领取按钮不处理。
        """
        b = self.bot
        if fr is None:
            fr = b.frame()

        if fr is None:
            return False

        if getattr(self, "_beacon_hold_mode", None) is True:
            b.log("[BEACON] 周三/周日且主界面旗帜已确认：只协助，不领取")

            # 蓝色一键协助允许点击
            if not self._beacon_assist_once():
                raise RuntimeError("烽火台特殊模式协助未完成")

            # 明确禁止领取
            if b.vision.match(fr, "beacon_claim_green"):
                b.log("[BEACON] 检测到一键领取，但特殊模式跳过")

            return True

        return False

    def _beacon_revisit(self, delay=2.5, inspect_queue=False,
                        recall_gather=False):
        """Leave, wait for a stable world screen, then re-enter and stabilize."""
        b = self.bot
        if not self._tap_game_back("Beacon revisit after reward"):
            b.log("[STOP] Could not tap beacon back button")
            b.fail_snapshot("beacon_revisit_back_failed")
            return False

        if not b.wait_main(10):
            b.log("[STOP] World main screen did not appear after leaving beacon")
            b.fail_snapshot("beacon_revisit_main_missing")
            return False

        # A single main-template hit can come from a transition frame. Require
        # three spaced confirmations before operating the next screen.
        deadline = time.monotonic() + 8
        stable_main = 0
        while time.monotonic() < deadline and stable_main < 3:
            fr = b.frame()
            if fr is not None and b.is_main(fr) and not b.vision.match(fr, "reward_continue"):
                stable_main += 1
            else:
                stable_main = 0
            if stable_main < 3:
                time.sleep(0.3)
        if stable_main < 3:
            b.log("[STOP] World main screen did not stabilize after leaving beacon")
            b.fail_snapshot("beacon_revisit_main_unstable")
            return False

        if not self.handle_main_extras():
            b.log("[STOP] Main screen extras did not settle before beacon re-entry")
            b.fail_snapshot("beacon_revisit_main_extras_failed")
            return False
        time.sleep(max(float(delay), 2.5))
        if inspect_queue:
            self._beacon_queue_slot_available = False
            self._beacon_queue_eta_seconds = None
            # The same main-page marker appears in city and world views; the
            # march counter exists only on the world map.
            fr = b.frame()
            if fr is not None and b.vision.match(fr, "city_exit_button"):
                b.log("[BEACON] 城内无行军计数；出城后读取队列")
                if not self._ensure_world():
                    b.fail_snapshot("beacon_queue_world_missing")
                    return False
            try:
                for queue_attempt in range(3):
                    try:
                        _, used, capacity, rows = self._march_state()
                        break
                    except (ValueError, RuntimeError, OSError):
                        if queue_attempt == 2:
                            raise
                        b.log(f"[BEACON] 行军数量第{queue_attempt+1}帧未确认，重新截图识别")
                        time.sleep(.45)
                self._beacon_queue_known_used = used
                self._beacon_queue_known_capacity = capacity
                self._beacon_queue_slot_available = used < capacity
                self._beacon_queue_eta_seconds = getattr(
                    self, "_last_march_return_seconds", None
                )
                b.log(
                    f"[BEACON] 返队时间到，检查行军队列：{used}/{capacity} "
                    f"{'有空位' if used < capacity else '仍满'}"
                )
                if used >= capacity and recall_gather:
                    gathering = sum("采集中" in state for state, _ in rows)
                    if gathering:
                        b.log("[BEACON] 队列满且有采集队；召回1支后等待空位")
                        if self.gather_recall(max_recall=1, only_if_full=True):
                            _, used, capacity, _ = self._march_state()
                            self._beacon_queue_known_used = used
                            self._beacon_queue_known_capacity = capacity
                            self._beacon_queue_slot_available = used < capacity
                            self._beacon_queue_slot_pending = used >= capacity
                            self._beacon_queue_eta_seconds = getattr(
                                self, "_last_march_return_seconds", None
                            )
                    else:
                        self._beacon_queue_slot_pending = True
            except (ValueError, RuntimeError, OSError) as exc:
                b.log(f"[BEACON] 返队后行军队列状态暂不可读：{exc}")
                b.fail_snapshot("beacon_queue_count_unknown")
                return False
        if not self._enter_beacon_from_main():
            b.log("[STOP] Could not re-enter beacon after waiting on world screen")
            return False
        if self._beacon_wait_page(timeout=12) is None:
            return False
        return True

    def beacon(self):
        """每次进入均协助、核对全部事件，跨两次进入确认完成状态。"""
        from workflow_state import WorkflowState, BEACON_TRANSITIONS
        b = self.bot
        state = WorkflowState(b, "beacon", BEACON_TRANSITIONS)
        self._beacon_hold_mode = None
        b.log("\n[FLOW] 烽火台：协助、派遣、重新进入复查")
        state.move("enter", "开始从当前画面定位烽火台入口")
        if not self._enter_beacon_from_main():
            state.move("unknown", "烽火台页面未确认")
            return False
        if self._beacon_safe_assist_only_check():
            self._tap_game_back("烽火台-特殊模式返回主界面")
            recovered = b.wait_main(5)
            state.move("complete" if recovered else "unknown",
                       "特殊模式协助后主界面已确认" if recovered else "特殊模式返回主界面未确认")
            return recovered

        deadline = time.monotonic() + 4 * 60 * 60
        expected_total = dispatched = skipped_count = 0
        skipped_events = []
        self._beacon_queue_slot_pending = False
        self._beacon_queue_eta_seconds = None
        self._beacon_queue_slot_available = False
        self._beacon_queue_known_used = None
        self._beacon_queue_known_capacity = None
        self._beacon_previous_scan = None
        queue_slot_checked = False
        queue_retry_at = 0.0
        empty_confirmations = 0
        while time.monotonic() < deadline:
            fr = b.frame()
            if fr is None or self._beacon_page_match(fr) is None:
                if not self._enter_beacon_from_main():
                    return False
            # 每次回到烽火台重新检查，不能只在任务开头协助一次。
            if not self._beacon_assist_once():
                return False
            fr = b.frame()
            if fr is None:
                continue
            state.move("scan", "新帧确认烽火台页面并完成协助检查")
            if self._beacon_previous_scan is None:
                self._beacon_compare_page(fr, "initial")
            all_events = self._beacon_all_events(fr)
            events = self._beacon_unfinished_events(fr)
            in_progress = self._beacon_in_progress_events(fr)
            if skipped_events:
                def was_skipped(candidate):
                    return any(
                        candidate["type"] == old["type"]
                        and (candidate["x"] - old["x"]) ** 2
                        + (candidate["y"] - old["y"]) ** 2 < 55 ** 2
                        for old in skipped_events
                    )
                events = [event for event in events if not was_skipped(event)]
            if (self._beacon_queue_slot_pending
                    and time.monotonic() < queue_retry_at):
                # 讨平山贼 battles do not consume a march slot. Every other
                # unfinished event waits until a slot is confirmed free.
                events = [event for event in events
                          if event["type"] == "bandit_suppression"]
            if (self._beacon_queue_slot_pending
                    and time.monotonic() >= queue_retry_at
                    and not any(e["type"] == "bandit_suppression" for e in events)):
                if not self._beacon_revisit(
                    delay=2.5, inspect_queue=True, recall_gather=True
                ):
                    return False
                if self._beacon_queue_slot_available:
                    self._beacon_queue_slot_pending = False
                    self._beacon_queue_eta_seconds = None
                    queue_slot_checked = True
                    queue_retry_at = 0.0
                    b.log("[BEACON] 行军名额已空出，恢复暂缓的烽火任务")
                else:
                    eta = self._beacon_queue_eta_seconds
                    queue_retry_at = time.monotonic() + (eta + 2 if eta else 30)
                    b.log(
                        f"[BEACON] 行军队列仍满；"
                        f"{f'预计{eta}秒后再查' if eta else '倒计时未识别，30秒后再查'}"
                    )
                continue
            checks = self._beacon_green_check_count(fr)
            final = b.vision.match(fr, "beacon_refresh_label")
            claim = b.vision.match(fr, "beacon_claim_green")
            # Settle every available bulk action before scanning individual
            # pins. Re-enter after claiming so the next pass confirms whether
            # claim or assist became available again.
            if claim is not None:
                state.move("claim", f"一键领取绿色模板 score={claim.score:.3f}")
                empty_confirmations = 0
                b.log("[BEACON] 一键领取仍为绿色，先领取并重新检查批量按钮")
                if not b.actions.tap_match(claim, fr.shape, "烽火台-一键领取"):
                    return False
                if not b.wait_template_any(("page_beacon_reward", "reward_continue"), 6):
                    b.fail_snapshot("beacon_initial_reward_missing")
                    return False
                if not self._close_beacon_reward():
                    return False
                if self._beacon_wait_page(timeout=12) is None:
                    return False
                self._beacon_compare_page(b.frame(), "after_claim")
                continue
            if all_events:
                summary = ", ".join(
                    f"{e['type']}@({e['x']},{e['y']})/{e['score']:.2f}"
                    for e in events
                ) or "none"
                b.log(f"[BEACON] scan candidates: {summary}")
                counts = {}
                for event in events:
                    counts[event["type"]] = counts.get(event["type"], 0) + 1
                type_summary = ", ".join(
                    f"{kind}={count}" for kind, count in sorted(counts.items())
                ) or "none"
                b.log(f"[BEACON] 可处理类型计数: {type_summary}")
            if in_progress:
                b.log("[BEACON] gathering pins: " + ", ".join(
                    f"({e['x']},{e['y']})" for e in in_progress
                ))
            if (final is not None and not all_events and not events
                    and not in_progress and claim is None):
                self._record_confirmed_visual("beacon_refresh_label", fr, final)
                b.log(f"[BEACON] Refresh countdown confirmed, score={final.score:.3f}; all work complete")
                break
            expected_total = max(expected_total, len(all_events),
                                 checks + len(in_progress))
            if (expected_total > 0 and dispatched >= expected_total
                    and not all_events and not events and not in_progress
                    and claim is None and not self._beacon_queue_slot_pending):
                empty_confirmations += 1
                b.log(f"[BEACON] 已处理{dispatched}/{expected_total}个已记录事件，"
                      f"空页面确认{empty_confirmations}/3")
                if empty_confirmations >= 3:
                    b.log("[BEACON] 连续3次确认无事件、无奖励、无进行中图标，任务结束")
                    break
            else:
                empty_confirmations = 0
            b.log(f"[BEACON] 事件={len(all_events)} 本轮最大={expected_total} "
                  f"完成勾={checks} 可派遣={len(events)} 领取={'绿' if claim is not None else '非绿'}")
            if (skipped_events and not events and not in_progress
                    and not self._beacon_queue_slot_pending):
                b.log(f"[BEACON] 其余可做事件已处理；暂缓营寨={skipped_count} 个，稍后可重试")
                break
            if (self._beacon_queue_slot_pending and not events
                    and time.monotonic() < queue_retry_at):
                state.move("waiting", "行军队列待释放，当前无可执行事件")
                wait_for = max(5.0, queue_retry_at-time.monotonic())
                b.log(f"[BEACON] 暂无其他可做事件；队伍预计{wait_for:.0f}秒后返回，再检查名额")
                if not self._beacon_revisit(
                    delay=wait_for, inspect_queue=True, recall_gather=True
                ):
                    return False
                if self._beacon_queue_slot_available:
                    self._beacon_queue_slot_pending = False
                    self._beacon_queue_eta_seconds = None
                    queue_slot_checked = True
                    queue_retry_at = 0.0
                else:
                    eta = self._beacon_queue_eta_seconds
                    queue_retry_at = time.monotonic() + (eta + 2 if eta else 30)
                continue
            march_required = {"rally", "resource", "excavation", "bandit_raid", "combat"}
            if (events and any(e["type"] in march_required for e in events)
                    and not queue_slot_checked
                    and not self._beacon_queue_slot_pending
                    and not any(e["type"] == "bandit_suppression" for e in events)):
                state.move("queue", "存在需出征事件，必须读取当前世界地图行军数量")
                b.log("[BEACON] 队列状态被烽火台界面遮挡；返回主地图核对实际名额")
                if not self._beacon_revisit(
                    delay=2.5, inspect_queue=True, recall_gather=True
                ):
                    return False
                if self._beacon_queue_slot_available:
                    queue_slot_checked = True
                    b.log("[BEACON] 确认有空闲行军名额，继续处理需出征任务")
                else:
                    self._beacon_queue_slot_pending = True
                    eta = self._beacon_queue_eta_seconds
                    queue_retry_at = time.monotonic() + (eta + 2 if eta else 30)
                    b.log(
                        "[BEACON] 当前没有空闲行军名额；暂缓所有需出征任务，"
                        f"{'预计'+str(eta)+'秒后' if eta else '30秒后'}复查"
                    )
                continue
            if events:
                if not queue_slot_checked or self._beacon_queue_slot_pending:
                    events.sort(key=lambda e: e["type"] != "bandit_suppression")
                event = events[0]
                state.move("dispatch", f"当前帧待办={event['type']}@({event['x']},{event['y']}) score={event['score']:.3f}")
                dispatch_result = self._beacon_dispatch_event(event)
                if dispatch_result is False:
                    state.move("unknown", "事件操作后的业务状态未确认")
                    return False
                if dispatch_result == "skipped_queue_full":
                    if event["type"] in march_required:
                        self._beacon_queue_slot_pending = True
                        eta = self._beacon_queue_eta_seconds
                        queue_retry_at = time.monotonic() + (eta + 2 if eta else 30)
                        b.log("[BEACON] 队列仍满，暂缓该出征任务并等待空位复查")
                    else:
                        skipped_events.append({
                            "type": event["type"], "x": event["x"], "y": event["y"],
                        })
                        skipped_count += 1
                elif dispatch_result == "already_in_progress":
                    skipped_events.append({
                        "type": event["type"], "x": event["x"], "y": event["y"],
                    })
                    b.log("[BEACON] 已派遣事件不重复点击；继续检查其他事件")
                elif dispatch_result == "queue_returning":
                    self._beacon_queue_slot_pending = True
                    eta = self._beacon_queue_eta_seconds
                    queue_retry_at = time.monotonic() + (eta + 2 if eta else 30)
                    b.log(
                        "[BEACON] 采集队伍返回途中；继续做其他事件；"
                        f"{'预计'+str(eta)+'秒后' if eta else '30秒后'}检查名额"
                    )
                elif dispatch_result == "already_completed":
                    dispatched += 1
                    b.log("[BEACON] 事件已在奖励页确认完成，不占用行军名额")
                else:
                    dispatched += 1
                    if event["type"] in march_required:
                        self._beacon_queue_slot_pending = False
                        self._beacon_queue_eta_seconds = None
                        queue_retry_at = 0.0
                if event["type"] in march_required:
                    # A successful dispatch consumes one of the slots just
                    # read on the world map; avoid leaving Beacon again while
                    # the remaining capacity is known.
                    if (dispatch_result is True
                            and self._beacon_queue_known_used is not None
                            and self._beacon_queue_known_capacity is not None):
                        self._beacon_queue_known_used += 1
                        queue_slot_checked = (
                            self._beacon_queue_known_used
                            < self._beacon_queue_known_capacity
                        )
                        if not queue_slot_checked:
                            self._beacon_queue_slot_pending = True
                            queue_retry_at = time.monotonic()
                    else:
                        queue_slot_checked = False
                if not self._enter_beacon_from_main():
                    return False
                time.sleep(.6)
                self._beacon_compare_page(b.frame(), "after_event")
            else:
                state.move("waiting", "当前帧无可派遣事件，等待画面更新")
                b.log("[BEACON] 尚未全部完成，留在烽火台等待画面更新并重新比对")
                time.sleep(20.0 if in_progress else 5.0)
                self._beacon_compare_page(b.frame(), "rescan")
        else:
            b.fail_snapshot("beacon_event_timeout")
            b.log("[STOP] 烽火台运行超过4小时，保存现场")
            return False
        if not self._tap_game_back("烽火台-复查完成返回主界面"):
            return False
        if not b.wait_main(5):
            state.move("unknown", "烽火台退出后未确认主界面")
            return False
        state.move("complete", f"已复查事件和奖励并确认主界面，派遣={dispatched}")
        b.log(f"[OK] 烽火台任务结束：派遣{dispatched}次，队列满暂缓{skipped_count}个")
        return True

    # ============================================================
    # v1.17 通用：城内/城外、搜索、交互参数
    # ============================================================

    def _opts(self):
        return getattr(self.bot, "runtime_options", {}) or {}

    def _tap_rel(self, xr, yr, label):
        b = self.bot
        fr = b.frame()
        if fr is None:
            return False
        h, w = fr.shape[:2]
        return b.actions.tap_frame(
            int(round(w * xr)),
            int(round(h * yr)),
            fr.shape,
            label,
        )

    def _green_button_center(
        self,
        frame,
        y_min_ratio=0.0,
        y_max_ratio=1.0,
        x_min_ratio=0.0,
        x_max_ratio=1.0,
    ):
        """在限定区域找绿色按钮中心，供模板漏检时做安全兜底。"""
        if frame is None:
            return None

        h, w = frame.shape[:2]
        bgr = frame.astype(np.int16)
        blue = bgr[:, :, 0]
        green = bgr[:, :, 1]
        red = bgr[:, :, 2]
        mask = (
            (green >= 105)
            & ((green - red) >= 32)
            & ((green - blue) >= 22)
            & (red <= 155)
        )

        y1 = max(0, int(round(h * y_min_ratio)))
        y2 = min(h, int(round(h * y_max_ratio)))
        x1 = max(0, int(round(w * x_min_ratio)))
        x2 = min(w, int(round(w * x_max_ratio)))
        if y2 <= y1 or x2 <= x1:
            return None

        row_counts = mask[y1:y2, x1:x2].sum(axis=1)
        rows = np.flatnonzero(row_counts >= max(10, int(round(w * 0.04))))
        if rows.size == 0:
            return None

        runs = []
        run_start = run_prev = int(rows[0])
        for row in rows[1:]:
            row = int(row)
            if row <= run_prev + 3:
                run_prev = row
            else:
                runs.append((run_start, run_prev))
                run_start = run_prev = row
        runs.append((run_start, run_prev))

        candidates = []
        for rel_y1, rel_y2 in runs:
            by1, by2 = y1 + rel_y1, y1 + rel_y2
            height = by2 - by1 + 1
            if height < max(8, int(round(h * 0.012))):
                continue
            if height > int(round(h * 0.10)):
                continue

            cols = np.flatnonzero(mask[by1:by2 + 1].any(axis=0))
            if cols.size == 0:
                continue
            bx1, bx2 = int(cols[0]), int(cols[-1])
            width = bx2 - bx1 + 1
            if bx1 < x1 or bx2 >= x2:
                continue
            if width < max(20, int(round(w * 0.08))):
                continue
            if width > int(round(w * 0.35)):
                continue

            area = int(mask[by1:by2 + 1, bx1:bx2 + 1].sum())
            candidates.append((by2, area, (bx1 + bx2) // 2, (by1 + by2) // 2))

        if not candidates:
            return None

        # 体力弹窗上方还有绿色“使用”，目标“领取”位于更下方；
        # 在限定区域内取最下面的绿色按钮，避免误点道具使用。
        _, _, cx, cy = max(candidates, key=lambda item: (item[0], item[1]))
        return cx, cy

    def _ensure_world(self):
        """采集/流寇/山寨搜索必须在城外世界地图。"""
        b = self.bot
        if not b.ensure_main():
            return False

        fr = b.frame()
        if b.vision.match(fr, "city_exit_button"):
            b.log("[NAV] 当前在城内，先点击右下角‘出城’")
            if not b.tap_template("city_exit_button", 2, "出城"):
                return False
            if not b.wait_template("main_search_glyph", 6):
                b.fail_snapshot("world_after_exit_not_confirmed")
                return False
        return True

    def _finish_resident_event(self, timeout=45):
        """选择第一项后继续推进对话/奖励，直到连续确认返回主界面。"""
        b = self.bot
        deadline = time.monotonic() + timeout
        last_known = time.monotonic()
        main_hits = 0
        clicks = 0
        while time.monotonic() < deadline and clicks < 40:
            fr = b.frame()
            if fr is None:
                time.sleep(0.12)
                continue
            cont = (
                b.vision.match(fr, "resident_continue")
                or b.vision.match(fr, "reward_continue")
            )
            choice = None if cont else b.vision.match(fr, "page_resident_event")
            if cont or choice:
                main_hits = 0
                clicks += 1
                if cont:
                    ok = b.actions.tap_match(cont, fr.shape, f"居民事件-继续#{clicks}")
                else:
                    h, w = fr.shape[:2]
                    ok = b.actions.tap_frame(
                        round(w * .50), round(h * .902), fr.shape,
                        f"居民事件-选择第一项#{clicks}",
                    )
                if not ok:
                    return False
                last_known = time.monotonic()
                time.sleep(0.55)
                continue
            if b.is_main(fr):
                main_hits += 1
                if main_hits >= 2:
                    b.log(f"[OK] 居民事件对话及奖励已结束，共推进{clicks}次")
                    return True
            else:
                main_hits = 0
            # 允许选项消失后的过场动画；不能把短暂漏检当成任务结束。
            if time.monotonic() - last_known > 5:
                break
            time.sleep(0.15)
        b.fail_snapshot("resident_continue_not_finished")
        b.log("[STOP] 居民事件尚未确认回到主界面")
        return False

    def _resident_left_entry(self, fr):
        """Confirm the fixed left HUD icon by its face and exclamation mark."""
        b = self.bot
        name = "resident_event_entry"
        candidate = b.vision.match(fr, name, threshold=0.70)
        if not candidate:
            return None

        h, w = fr.shape[:2]
        x, y = candidate.center
        base_x, base_y = x * 726 / w, y * 1600 / h
        if not (0 <= base_x <= 120 and 880 <= base_y <= 1120):
            return None

        x1, y1, x2, y2 = candidate.bbox
        patch = fr[y1:y2, x1:x2]
        template = b.vision.templates[name]
        if patch.size == 0:
            return None
        patch = cv2.resize(patch, (template.shape[1], template.shape[0]))
        parts = ((15, 25, 48, 75, 0.85), (58, 23, 81, 70, 0.75))
        for left, top, right, bottom, minimum in parts:
            score = cv2.matchTemplate(
                patch[top:bottom, left:right],
                template[top:bottom, left:right],
                cv2.TM_CCOEFF_NORMED,
            )[0, 0]
            if not np.isfinite(score) or score < minimum:
                return None
        return candidate

    def resident_event(self):
        """Process resident events until the city shows no remaining entry."""
        b = self.bot
        b.log("\n[FLOW] Resident events: repeat until the verified entry disappears")

        completed = 0
        max_rounds = 20
        for round_no in range(1, max_rounds + 1):
            # Each dialog returns to the world main screen; re-enter the city
            # before checking whether the resident icon is still present.
            if not self._ensure_city():
                return False

            fr = b.frame()
            if fr is None:
                b.fail_snapshot("resident_event_frame_missing")
                return False
            # Only the verified left HUD portrait is a resident-event entry.
            # Floating bubbles over buildings belong to unrelated map events.
            entry = self._resident_left_entry(fr)

            if not entry:
                # Let the city HUD settle before concluding that the queue is empty.
                time.sleep(0.35)
                fr = b.frame()
                if fr is None:
                    b.fail_snapshot("resident_event_recheck_frame_missing")
                    return False
                entry = self._resident_left_entry(fr)
                if not entry:
                    b.log(f"[OK] Resident queue empty after {completed} completed event(s)")
                    return True

            b.log(
                f"[RESIDENT] event round {round_no} score={entry.score:.3f} "
                f"frame={entry.center}"
            )
            if not b.actions.tap_match(entry, fr.shape, f"Resident event #{round_no}"):
                return False

            if not b.wait_template_any(
                ("page_resident_event", "resident_continue", "reward_continue"), 5
            ):
                b.log("[STOP] Resident entry click did not open a recognized dialog")
                b.fail_snapshot("resident_event_dialog_missing")
                return False

            if not self._finish_resident_event():
                return False
            completed += 1

        b.fail_snapshot("resident_event_round_limit")
        b.log(f"[STOP] Resident event round limit reached ({max_rounds}); entry still present")
        return False

    def _ensure_city(self):
        b = self.bot
        if not b.ensure_main():
            return False
        fr = b.frame()
        if b.vision.match(fr, "city_exit_button"):
            return True
        city = b.vision.match(fr, "world_city_button")
        if city:
            b.actions.tap_match(city, fr.shape, "进入城镇")
            if b.wait_template("city_exit_button", 6):
                return True
        b.fail_snapshot("ensure_city_failed")
        return False

    def _open_search(self):
        b = self.bot
        if not self._ensure_world():
            return False
        entry = b.wait_template("main_search_glyph", 3)
        fr = b.frame()
        if (entry is None or fr is None or not b.actions.tap_match(
                entry, fr.shape, "打开搜索")):
            b.fail_snapshot("search_button_missing")
            return False
        if not b.wait_template("page_search_panel", 4):
            b.fail_snapshot("search_panel_missing")
            return False
        self._record_confirmed_visual("main_search_glyph", fr, entry)
        return True

    def _carousel_left_edge(self):
        b = self.bot
        for _ in range(3):
            fr = b.frame(); h,w=fr.shape[:2]
            b.actions.swipe_frame(
                int(w*0.18), int(h*0.91),
                int(w*0.86), int(h*0.91),
                fr.shape, 260, "搜索类型滑到最左"
            )
            time.sleep(0.15)

    def _carousel_right_edge(self):
        b = self.bot
        for _ in range(3):
            fr = b.frame()
            if fr is None: return False
            h,w=fr.shape[:2]
            if not b.actions.swipe_frame(
                int(w*0.86), int(h*0.91),
                int(w*0.18), int(h*0.91),
                fr.shape, 260, "搜索类型滑到最右"
            ): return False
            time.sleep(0.15)
        return True

    def _set_search_level(self, level):
        """不依赖OCR：先连续点“-”回到最低，再点“+”到目标等级。"""
        b = self.bot
        level = max(1, min(40, int(level)))
        fr = b.frame()
        if fr is None: return False
        h,w=fr.shape[:2]
        minus=(int(w*0.151), int(h*0.735))
        plus=(int(w*0.848), int(h*0.735))
        for _ in range(42):
            if not b.actions.tap_frame(minus[0],minus[1],fr.shape,"搜索等级-"): return False
        for _ in range(level-1):
            if not b.actions.tap_frame(plus[0],plus[1],fr.shape,"搜索等级+"): return False
        time.sleep(0.25)
        b.log(f"[SEARCH] 搜索等级已按安全方式设置为 {level}")
        return True

    def _ensure_full_resource_checked(self):
        b = self.bot
        fr=b.frame()
        if fr is None: return False
        if b.vision.match(fr,"search_full_checked"):
            return True
        b.log("[SEARCH] ‘仅搜索满资源的资源点’未确认勾选，先勾选")
        h,w=fr.shape[:2]
        if not b.actions.tap_frame(int(w*0.37),int(h*0.768),fr.shape,"勾选仅搜索满资源"): return False
        if not b.wait_template("search_full_checked",2):
            b.fail_snapshot("full_resource_checkbox_not_checked")
            return False
        return True

    def _select_resource_type(self, name):
        """右端固定顺序：一键采集、农舍、伐木场、煤矿、铁矿。"""
        b=self.bot
        if not self._carousel_right_edge(): return False
        fr=b.frame()
        if fr is None: return False
        h,w=fr.shape[:2]
        xpos={"farm":0.292,"wood":0.493,"coal":0.695,"iron":0.895}
        labels={"farm":"农舍","wood":"伐木场","coal":"煤矿","iron":"铁矿"}
        if name not in xpos:
            return False
        if not b.actions.tap_frame(int(w*xpos[name]),int(h*0.91),fr.shape,f"选择{labels[name]}"): return False
        time.sleep(0.25)
        return True

    def _select_rogue(self):
        b=self.bot
        self._carousel_left_edge()
        fr=b.frame(); h,w=fr.shape[:2]
        b.actions.tap_frame(int(w*0.09),int(h*0.91),fr.shape,"选择流寇")
        time.sleep(0.25)
        return True

    def _click_search_button(self):
        return self._tap_rel(0.50,0.807,"搜索")

    # ============================================================
    # 爵位每日奖励
    # ============================================================

    def title_daily(self):
        b=self.bot
        b.log("\n[FLOW] 爵位每日奖励")
        if not b.ensure_main(): return False
        # 爵位入口是左上角带“权”和等级数字的金色徽章，
        # 位于顶部状态栏上方；不要点击下方的免战/增益开关区域。
        # 先点徽章中心，若页面动画或缩放造成边缘漏点，再在徽章
        # 内部的几个邻近位置重试，每次都必须确认已进入爵位页。
        # 爵位入口改为动态定位：
        # 旧版固定坐标容易受到刘海、分辨率缩放、顶部状态栏变化影响。
        # 先在顶部区域寻找金色爵位徽章，再以检测中心点击。
        title_opened = False
        fr = b.frame()
        title_points = []

        if fr is not None:
            hsv = cv2.cvtColor(fr, cv2.COLOR_BGR2HSV)
            h, w = fr.shape[:2]

            # 顶部状态栏区域，避开聊天/按钮区域
            roi = hsv[0:int(h*0.18), 0:int(w*0.35)]

            # 金色徽章颜色范围
            mask = cv2.inRange(
                roi,
                np.array([15, 55, 80]),
                np.array([40, 255, 255])
            )
            mask = cv2.morphologyEx(
                mask,
                cv2.MORPH_OPEN,
                np.ones((3,3), np.uint8)
            )

            contours, _ = cv2.findContours(
                mask,
                cv2.RETR_EXTERNAL,
                cv2.CHAIN_APPROX_SIMPLE
            )

            candidates = []
            for c in contours:
                area = cv2.contourArea(c)
                if area < 40 or area > 8000:
                    continue
                x, y, cw, ch = cv2.boundingRect(c)
                ratio = cw / max(ch, 1)
                if 0.45 < ratio < 2.5:
                    cx = (x + cw/2) / w
                    cy = (y + ch/2) / h
                    # 只保留靠左上角的爵位区域
                    if 0.03 < cx < 0.32 and 0.02 < cy < 0.16:
                        candidates.append((area, cx, cy))

            candidates.sort(reverse=True)
            for _, cx, cy in candidates[:5]:
                title_points.append((cx, cy))

            b.log(f"[TITLE] 动态检测爵位候选:{title_points}")

        # 动态检测失败时，使用小范围兜底点击
        if not title_points:
            title_points = [
                (0.205, 0.090),
                (0.205, 0.078),
                (0.188, 0.090),
                (0.222, 0.090),
                (0.205, 0.103),
            ]

        for point_index, (xr, yr) in enumerate(title_points, start=1):
            b.log(
                f"[TITLE] 点击爵位入口#{point_index} "
                f"ratio=({xr:.3f},{yr:.3f})"
            )
            if not self._tap_rel(xr, yr, f"进入爵位#{point_index}"):
                continue

            # 动态点击后等待页面确认，避免误触其它顶部按钮
            if b.wait_template("page_title", 5 if point_index == 1 else 3):
                title_opened = True
                break

        if not title_opened:
            b.fail_snapshot("title_page_missing"); return False

        # 爵位页面加载完成后再识别奖励，避免首帧UI未加载导致漏点
        time.sleep(1.2)
        b.log("[TITLE] 爵位页面稳定，开始检测奖励按钮")

        fr = b.frame()
        if fr is None or not b.vision.match(fr, "page_title"):
            b.log("[TITLE] 奖励检测前爵位页未确认")
            b.fail_snapshot("title_page_lost_before_rewards")
            return False

        claim = b.vision.match(fr, "title_daily_claim")
        if claim:
            b.log(f"[TITLE] 每日礼包领取按钮已识别 score={claim.score:.3f}")
        else:
            claim_score = b.vision.score(fr, "title_daily_claim")
            b.log(
                "[TITLE] 未识别到每日礼包领取按钮 "
                f"score={claim_score:.3f}"
            )

        if claim:
            fr = b.frame()
            if fr is None or not b.actions.tap_match(
                claim, fr.shape, "爵位每日礼包-领取"
            ):
                b.log("[TITLE] 每日礼包领取点击失败")
                b.fail_snapshot("title_daily_claim_tap_failed")
                return False

            cont = b.wait_template("reward_continue", 5)
            if cont:
                time.sleep(0.5)
                fr = b.frame()
                if fr is None or not b.actions.tap_match(
                    cont, fr.shape, "爵位每日礼包-点击继续"
                ):
                    b.log("[TITLE] 每日礼包奖励继续点击失败")
                    b.fail_snapshot("title_daily_reward_continue_tap_failed")
                    return False

                if not b.wait_template_gone("reward_continue", 4):
                    b.log("[TITLE] 每日礼包奖励弹窗未关闭")
                    b.fail_snapshot("title_daily_reward_continue_stuck")
                    return False

                if not b.wait_template("page_title", 4):
                    b.log("[TITLE] 每日礼包领取后未回到爵位页")
                    b.fail_snapshot("title_page_missing_after_daily_claim")
                    return False
            else:
                fr = b.frame()
                if fr is None or not b.vision.match(fr, "page_title"):
                    b.log("[TITLE] 领取后未出现奖励继续按钮，且爵位页未确认")
                    b.fail_snapshot("title_daily_reward_state_unknown")
                    return False

                # 有些奖励只显示提示、不弹奖励页；至少确认领取按钮已消失。
                if not b.wait_template_gone("title_daily_claim", 2):
                    b.log("[TITLE] 领取后奖励按钮仍在，无法确认领取结果")
                    b.fail_snapshot("title_daily_claim_not_completed")
                    return False
                b.log("[TITLE] 奖励继续弹窗未出现，但领取按钮已消失")

        time.sleep(0.5)
        fr = b.frame()
        if fr is None or not b.vision.match(fr, "page_title"):
            b.log("[TITLE] 宝箱检测前爵位页未确认")
            b.fail_snapshot("title_page_lost_before_chest")
            return False

        chest = b.vision.match(fr, "title_login_chest_red")
        if chest:
            b.log(f"[TITLE] 登录宝箱已识别 score={chest.score:.3f}")
        else:
            chest_score = b.vision.score(fr, "title_login_chest_red")
            b.log(f"[TITLE] 未识别到可点击登录宝箱 score={chest_score:.3f}")

        if chest:
            if not b.actions.tap_match(chest, fr.shape, "爵位登录宝箱"):
                b.log("[TITLE] 登录宝箱点击失败")
                b.fail_snapshot("title_login_chest_tap_failed")
                return False

            popup = b.wait_template("title_login_popup", 4)
            popup_continue = None
            popup_indicator = "title_login_popup"
            if not popup:
                # 某些账号/分辨率下奖励弹窗的标题栏样式不同，专用模板
                # 会漏检，但弹窗底部“点击任意处继续”仍可稳定识别。
                popup_continue = b.wait_template("reward_continue", 2)
                if popup_continue:
                    popup_indicator = "reward_continue"
                    b.log(
                        "[TITLE] 专用登录奖励弹窗模板未匹配，"
                        "改由通用继续提示确认奖励弹窗"
                    )

            if popup or popup_continue:
                # 弹窗底部写“点击任意处继续”，点击提示文字或安全底部区域。
                if popup_continue:
                    fr = b.frame()
                    continued = fr is not None and b.actions.tap_match(
                        popup_continue, fr.shape, "爵位经验奖励-点击继续"
                    )
                else:
                    continued = self._tap_rel(
                        0.50, 0.93, "爵位经验奖励-继续"
                    )

                if not continued:
                    b.log("[TITLE] 登录奖励弹窗继续点击失败")
                    b.fail_snapshot("title_login_popup_continue_tap_failed")
                    return False

                if not b.wait_template_gone(popup_indicator, 4):
                    b.log("[TITLE] 登录奖励弹窗未关闭")
                    b.fail_snapshot("title_login_popup_stuck")
                    return False

                if not b.wait_template("page_title", 3):
                    b.log("[TITLE] 关闭登录奖励后未回到爵位页")
                    b.fail_snapshot("title_page_missing_after_login_reward")
                    return False
            else:
                fr = b.frame()
                if fr is None or not b.vision.match(fr, "page_title"):
                    b.log("[TITLE] 点击宝箱后未识别奖励弹窗，且爵位页未确认")
                    b.fail_snapshot("title_login_popup_state_unknown")
                    return False

                b.log("[TITLE] 点击宝箱后未识别奖励弹窗，领取结果无法确认")
                b.fail_snapshot("title_login_popup_missing")
                return False

        if not self._tap_game_back("爵位-返回主界面"):
            b.log("[TITLE] 返回主界面点击失败")
            b.fail_snapshot("title_back_tap_failed")
            return False

        if not b.wait_main(5):
            b.log("[TITLE] 返回后未确认主界面")
            b.fail_snapshot("title_main_missing_after_return")
            return False

        b.log("[OK] 爵位奖励流程结束，已确认回到主界面")
        return True

    # ============================================================
    # 自动加集结
    # ============================================================

    def auto_rally(self):
        b=self.bot
        b.log("\n[FLOW] 同盟自动加集结（8小时）")
        if not b.ensure_main(): return False
        if not b.tap_template("main_alliance",3,"进入同盟"): return False
        if not b.wait_template("alliance_war",5):
            b.fail_snapshot("alliance_war_missing"); return False
        if not b.tap_template("alliance_war",2,"同盟战争"): return False
        if not b.wait_template("page_alliance_war",5):
            b.fail_snapshot("alliance_war_page_missing"); return False
        if not b.tap_template("alliance_auto_join",2,"自动加集结"): return False
        auto_join_page = b.wait_template_any(
            ["page_auto_join_wechat", "page_auto_join"], 4
        )
        if not auto_join_page:
            b.fail_snapshot("auto_join_modal_missing"); return False

        # 奖励次数已满的类型取消勾选，其它勾选和已有编队保持原设置。
        if not self._auto_rally_reward_limits():
            return False
        start_button = b.wait_template_any(
            ["auto_join_start_wechat", "auto_join_start"], 2
        )
        if not start_button:
            return False
        fr = b.frame()
        if fr is None or not b.actions.tap_match(
            start_button, fr.shape, "自动加集结-开启"
        ):
            return False
        time.sleep(0.7)
        b.log("[OK] 已点击开启；游戏会将持续时间重置为约8小时")

        # 右上角X关闭弹窗。
        close_y = 0.204 if auto_join_page.name == "page_auto_join_wechat" else 0.245
        self._tap_rel(0.925,close_y,"自动加集结-X关闭")
        if not b.wait_template("page_alliance_war",4):
            b.fail_snapshot("auto_join_close_not_back")
            return False

        # 弹窗刚关闭时同盟战争页可能仍在过渡动画中。
        # 旧版在识别成功后立刻点击返回，点击偶尔会被动画吞掉。
        time.sleep(0.55)

        # 同盟战争 -> 同盟首页：最多重试3次，并且每次都确认页面真的变化。
        back_ok = False
        for retry in range(1, 4):
            # 如果已经到了同盟首页，直接成功。
            if b.vision.match(b.frame(), "page_alliance_home"):
                back_ok = True
                break

            # 只有仍确认在同盟战争页时才点左下角返回。
            if not b.vision.match(b.frame(), "page_alliance_war"):
                break

            b.log(
                f"[AUTO RALLY NAV] 同盟战争返回同盟首页 "
                f"尝试#{retry}"
            )
            self._tap_game_back(
                f"同盟战争-返回同盟首页#{retry}"
            )

            deadline = time.time() + 1.8
            while time.time() < deadline:
                fr = b.frame()

                if b.vision.match(fr, "page_alliance_home"):
                    b.log(
                        f"[OK] 已回到同盟首页 "
                        f"(第{retry}次返回成功)"
                    )
                    back_ok = True
                    break

                time.sleep(0.10)

            if back_ok:
                break

            # 下一次重试前让页面稳定。
            time.sleep(0.35)

        if not back_ok:
            b.log(
                "[STOP] 自动加集结完成后，"
                "连续3次仍未从同盟战争返回同盟首页"
            )
            b.fail_snapshot(
                "auto_rally_return_alliance_home_failed"
            )
            return False

        # 同盟首页 -> 主界面，同样做确认重试。
        time.sleep(0.40)

        main_ok = False
        for retry in range(1, 4):
            if b.wait_main(0.2):
                main_ok = True
                break

            if not b.vision.match(
                b.frame(),
                "page_alliance_home",
            ):
                break

            b.log(
                f"[AUTO RALLY NAV] 同盟首页返回主界面 "
                f"尝试#{retry}"
            )
            self._tap_game_back(
                f"同盟首页-返回主界面#{retry}"
            )

            if b.wait_main(1.8):
                b.log(
                    f"[OK] 已回到主界面 "
                    f"(第{retry}次返回成功)"
                )
                main_ok = True
                break

            time.sleep(0.35)

        if not main_ok:
            b.fail_snapshot(
                "auto_rally_return_main_failed"
            )
            return False

        return True

    # ============================================================
    # 体力领取 + 交互补体力
    # ============================================================

    def _close_stamina_more_to_profile(self):
        """
        安全关闭“获取更多”弹窗。

        用户实测：
        - 弹窗上方深色遮罩空白区域可点击退出；
        - 弹窗自身右上角X也可退出；
        - 屏幕最右上角悬浮栏圆形“－”绝对禁止点击。

        因此本函数只点击安全空白区，不靠系统BACK，
        也不靠近右上角悬浮栏禁区。
        """
        b = self.bot

        # 447x1001样本中的安全空白区大致为：
        # x=15..430, y=157..250
        # 选择中央偏左安全点，远离顶部悬浮栏和弹窗按钮。
        safe_points = [
            (0.50, 0.205),
            (0.35, 0.205),
            (0.65, 0.205),
        ]

        for i, (xr, yr) in enumerate(safe_points, 1):
            fr = b.frame()
            if fr is None:
                return False

            # 如果已经回个人主页，直接成功。
            if self._is_profile_page(fr):
                b.log("[OK] 已回到个人主页")
                return True

            h, w = fr.shape[:2]
            x = int(round(w * xr))
            y = int(round(h * yr))

            b.log(
                f"[STAMINA NAV] 安全空白区退出#{i} "
                f"frame=({x},{y})"
            )
            b.actions.tap_frame(
                x,
                y,
                fr.shape,
                f"获取更多-安全空白退出#{i}",
            )

            deadline = time.time() + 1.2
            while time.time() < deadline:
                fr2 = b.frame()
                if self._is_profile_page(fr2):
                    b.log(
                        f"[OK] 获取更多弹窗已关闭 "
                        f"(安全空白#{i}成功)"
                    )
                    return True
                time.sleep(0.10)

        # 最后可尝试弹窗自身右上角X，但位置严格限制在弹窗X，
        # 不接近屏幕最右上角圆形“－”禁区。
        fr = b.frame()
        if fr is not None:
            h, w = fr.shape[:2]
            # 样本弹窗X约在447x1001中的(413,291)
            x = int(round(w * (413/447)))
            y = int(round(h * (291/1001)))

            b.log(
                f"[STAMINA NAV] 安全空白未关闭，"
                f"最后尝试弹窗自身X frame=({x},{y})"
            )
            b.actions.tap_frame(
                x,
                y,
                fr.shape,
                "获取更多-弹窗X关闭",
            )

            deadline = time.time() + 1.2
            while time.time() < deadline:
                fr2 = b.frame()
                if self._is_profile_page(fr2):
                    b.log("[OK] 弹窗自身X关闭成功")
                    return True
                time.sleep(0.10)

        b.log(
            "[STOP] 获取更多弹窗未关闭；"
            "不会点击屏幕右上角圆形“-”禁区，也不会继续乱点"
        )
        b.fail_snapshot("stamina_more_safe_exit_failed")
        return False

    def _open_profile(self):
        b=self.bot
        if not b.ensure_main(): return False
        # The actual avatar sits below the top inset. Keep taps inside its
        # portrait and adjust slightly only after a fresh page check.
        profile_page = False
        for attempt, (xr, yr) in enumerate(
                ((.080, .095), (.065, .095), (.095, .095)), 1):
            fr = b.frame()
            if fr is None:
                continue
            if self._is_profile_page(fr):
                profile_page = True
                break
            h, w = fr.shape[:2]
            if not b.actions.tap_frame(round(w*xr), round(h*yr), fr.shape,
                                       f"打开个人主页#{attempt}"):
                continue
            if b.wait_template_any(
                    ["page_profile_wechat", "page_profile", "profile_stamina_plus"],
                    2.0):
                profile_page = True
                break
            # A client may finish drawing the profile just after the template
            # wait expires. Read a fresh frame and its unique bottom ID before
            # sending another avatar tap. The Oct 15 failure screenshot was
            # already on the profile despite the earlier timeout.
            fresh = b.frame()
            if fresh is not None and self._is_profile_page(fresh):
                profile_page = True
                break
            if fresh is not None:
                try:
                    from account_records import profile_identity
                    identity = profile_identity(fresh)
                except (RuntimeError, OSError, ValueError) as exc:
                    b.log(f"[PROFILE] 新帧ID复核异常：{exc}")
                    identity = {}
                if identity.get("id"):
                    b.log(f"[PROFILE] 页面模板未及时命中，但新帧ID={identity['id']}")
                    profile_page = True
                    break
        if not profile_page:
            b.fail_snapshot("profile_page_missing")
            return False
        return True

    def stamina(self):
        b = self.bot
        b.log("[FLOW] 体力：按‘前往仓库’行核对免费领取状态")
        if not self._open_profile():
            return False
        if not b.tap_template("profile_stamina_plus",2,"体力+"):
            b.fail_snapshot("stamina_plus_missing"); return False
        if not b.wait_template("page_stamina_more",4):
            b.fail_snapshot("stamina_more_missing"); return False

        from stamina_warehouse import process
        if not process(self):
            self._close_stamina_more_to_profile()
            self._tap_game_back("体力状态未确认-返回主界面")
            return False

        add=max(0,int(self._opts().get("stamina_add",0) or 0))
        if add>0:
            clicks=int(math.ceil(add/10.0))
            b.log(f"[STAMINA] 用户要求补约{add}体力 -> 使用道具{clicks}次（每次+10）")
            for i in range(clicks):
                fr=b.frame(); use=b.vision.match(fr,"stamina_use_green")
                if not use:
                    b.log(f"[WARN] 第{i+1}次前已找不到绿色‘使用’，停止补体力")
                    break
                b.actions.tap_match(use,fr.shape,f"体力道具-使用#{i+1}")
                time.sleep(0.16)

        # 禁止：购买并使用、一键补齐所有。
        if not self._close_stamina_more_to_profile():
            return False

        self._tap_game_back("个人主页-返回主界面")
        return b.wait_main(5)

    # ============================================================
    # 交互式四资源采集
    # ============================================================

    def _dispatch_one_resource(self, rtype, level):
        b=self.bot
        if not self._ensure_world():
            return False
        try:
            _, before, capacity, _ = self._march_state()
        except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            b.log(f"[STOP] {exc}")
            b.fail_snapshot("gather_queue_unknown")
            return False
        if before >= capacity:
            b.log(f"[INFO] 行军已{before}/{capacity}，不能继续派采集")
            return "full"
        if not self._open_search(): return False
        if not self._select_resource_type(rtype): return False
        if not self._set_search_level(level): return False
        if not self._ensure_full_resource_checked(): return False
        if not self._click_search_button(): return False
        collect=b.wait_template_any(
            ("resource_collect_label", "resource_collect_button_alt",
             "resource_collect_button"), 5
        )
        fr_collect=b.frame()
        try:
            if collect is None or not self._gather_result_available(fr_collect, level):
                b.log("[STOP] 搜索结果等级或空闲状态未确认")
                b.fail_snapshot(f"gather_{rtype}_result_unconfirmed")
                return False
        except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            b.log(f"[STOP] 资源详情识别失败：{exc}")
            b.fail_snapshot(f"gather_{rtype}_result_unconfirmed")
            return False
        if (collect is None or fr_collect is None or not b.actions.tap_match(
                collect,fr_collect.shape,f"{rtype}-采集")):
            b.fail_snapshot(f"gather_{rtype}_result_missing"); return False
        depart=b.wait_template_any(("resource_depart_button", "beacon_depart_text", "generic_depart_button"),5)
        if depart is None:
            try:
                self._gather_purchase_close()
            except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
                b.log(f"[WARN] 队列弹窗识别失败：{exc}")
            b.fail_snapshot(f"gather_{rtype}_depart_missing"); return False
        self._record_confirmed_visual(collect.name,fr_collect,collect)
        fr_depart=b.frame()
        if fr_depart is None or not b.actions.tap_match(
                depart,fr_depart.shape,f"{rtype}-出征"):
            b.fail_snapshot(f"gather_{rtype}_depart_click_failed"); return False
        if not b.wait_main(6):
            b.fail_snapshot(f"gather_{rtype}_after_depart"); return False
        try:
            for _ in range(3):
                _, after, _, _ = self._march_state()
                if after > before:
                    return True
                time.sleep(.4)
        except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            b.log(f"[WARN] {exc}")
        b.log("[STOP] 出征后未确认新增行军队列，停止，避免重复派遣")
        b.fail_snapshot(f"gather_{rtype}_queue_unconfirmed")
        return False

    def gather(self):
        b=self.bot
        b.log("\n[FLOW] 交互式资源采集")
        opts=self._opts()
        level=int(opts.get("gather_level",8) or 8)
        plan=opts.get("gather_plan",{}) or {}
        order=[("farm","农舍"),("wood","伐木场"),("coal","煤矿"),("iron","铁矿")]
        total=sum(max(0,int(plan.get(k,0))) for k,_ in order)
        if total<=0:
            b.log("[INFO] 本次资源采集分配总数=0，跳过")
            b.task_skip_reason = "本次资源采集分配总数=0，未安排采集"
            return True
        if total>6:
            b.log("[STOP] 采集计划超过6队，请重新分配")
            return False
        sent=0
        for key,label in order:
            n=max(0,int(plan.get(key,0)))
            for i in range(n):
                b.log(f"[GATHER] {label} {i+1}/{n}，等级{level}")
                r=self._dispatch_one_resource(key,level)
                if r=="full":
                    b.log(f"[INFO] 队列已满，本次已成功派出{sent}/{total}队")
                    b.task_waiting_reason = (
                        f"采集已派{sent}/{total}队；行军队列已满，剩余{total-sent}队待空位"
                    )
                    return True
                if r is not True: return False
                sent+=1
        b.log(f"[OK] 资源采集已派出 {sent}/{total} 队")
        return True

    # ============================================================
    # 流寇
    # ============================================================

    def _clear_combat_team(self):
        b=self.bot
        fr=b.frame(); h,w=fr.shape[:2]
        b.actions.tap_frame(int(w*0.335),int(h*0.203),fr.shape,"移除默认武将")
        return b.wait_template("rally_slots_empty",4) is not None

    def _select_top_three_heroes(self):
        """祝融不可用时，回列表顶部，选前三个可用武将。"""
        b=self.bot
        for _ in range(6):
            fr=b.frame(); h,w=fr.shape[:2]
            b.actions.swipe_frame(int(w*0.50),int(h*0.56),int(w*0.50),int(h*0.80),fr.shape,300,"武将列表回顶部")
            time.sleep(0.12)
        fr=b.frame(); h,w=fr.shape[:2]
        # 第一排前三张卡片中心。
        for xr in (0.17,0.39,0.61):
            b.actions.tap_frame(int(w*xr),int(h*0.59),fr.shape,"前三武将上阵选择")
            time.sleep(0.18)
        dep=b.vision.match(b.frame(),"hero_deploy_button")
        if not dep: return False
        b.actions.tap_match(dep,b.frame().shape,"前三武将-上阵")
        return True

    def _combat_prepare_prefer_zhurong(self):
        b=self.bot
        # 默认页已有三将；先清空，再尝试祝融。
        if not self._clear_combat_team():
            b.fail_snapshot("rogue_clear_team_failed"); return False
        fr=b.frame(); h,w=fr.shape[:2]
        b.actions.tap_frame(int(w*0.225),int(h*0.278),fr.shape,"武将槽-添加")
        if not b.wait_template("page_hero_select",4): return False

        # 复用烽火台的祝融搜索，但失败不停止，改用前三武将。
        found=False
        for attempt in range(6):
            fr=b.frame(); m=b.vision.match(fr,"hero_zhurong")
            if m:
                b.actions.tap_match(m,fr.shape,"流寇-选择祝融")
                found=True; break
            h,w=fr.shape[:2]
            b.actions.swipe_frame(int(w*0.5),int(h*0.78),int(w*0.5),int(h*0.57),fr.shape,320,"寻找祝融")
            time.sleep(0.25)

        if found:
            if not b.wait_template("hero_zhurong_selected",3): found=False
        if found:
            if not b.tap_template("hero_deploy_button",2,"祝融-上阵"): return False
            b.log("[OK] 流寇优先使用祝融")
            return True

        b.log("[INFO] 祝融当前不可用/未找到，按规则使用武将列表前三个")
        return self._select_top_three_heroes()

    def _dispatch_one_rogue(self, level):
        b=self.bot
        if b.vision.match(b.frame(),"march_full_6of6"):
            return "full"
        if not self._open_search(): return False
        self._select_rogue()
        self._set_search_level(level)
        self._click_search_button()
        atk=b.wait_template("rogue_attack_button",5)
        if not atk:
            b.fail_snapshot("rogue_attack_missing"); return False
        b.actions.tap_match(atk,b.frame().shape,"流寇-攻击")
        if not b.wait_template("generic_depart_button",5):
            b.fail_snapshot("rogue_march_page_missing"); return False
        if not self._combat_prepare_prefer_zhurong(): return False
        dep=b.wait_template("generic_depart_button",5)
        if not dep:
            b.fail_snapshot("rogue_depart_missing"); return False
        b.actions.tap_match(dep,b.frame().shape,"流寇-出征")
        if not b.wait_main(6):
            b.fail_snapshot("rogue_after_depart_missing"); return False
        return True

    def rogue(self):
        b=self.bot
        b.log("\n[FLOW] 打流寇")
        opts=self._opts(); level=int(opts.get("rogue_level",35) or 35); count=max(0,int(opts.get("rogue_count",0) or 0))
        if count<=0:
            b.log("[INFO] 本次流寇次数=0，跳过")
            b.task_skip_reason = "本次流寇次数=0，未安排讨伐流寇"
            return True
        sent=0
        for i in range(count):
            b.log(f"[ROGUE] 第{i+1}/{count}次，等级{level}")
            r=self._dispatch_one_rogue(level)
            if r=="full":
                b.log(f"[INFO] 行军已满，停止继续派流寇；已派{sent}/{count}")
                return True
            if r is not True: return False
            sent+=1
        b.log(f"[OK] 已派流寇 {sent}/{count} 次")
        return True

    # ============================================================
    # 综合队列：训兵领取 + 兵书研习空闲自动启动
    # ============================================================

    def _open_queue_panel(self):
        b = self.bot

        if not b.ensure_main():
            return False

        # 关键修复：
        # 第二次处理训兵时，综合队列可能仍然保持展开。
        # 展开状态下左侧蓝色“>”不存在，右侧会出现蓝色“<”收起把手。
        fr = b.frame()

        collapse = b.vision.match(
            fr,
            "queue_collapse_tab",
        )
        if collapse:
            b.log(
                f"[QUEUE] 检测到蓝色<收起把手 "
                f"score={collapse.score:.3f}，"
                "综合队列已经展开，不重复点>"
            )
            return True

        # 兼容已有面板内容锚点。
        if (
            b.vision.match(fr, "queue_panel_training")
            or b.vision.match(fr, "queue_training_header")
            or b.vision.match(fr, "queue_book_idle")
            or b.vision.match(fr, "queue_train_claim_green_v2")
            or b.vision.match(fr, "queue_train_claim_green")
        ):
            b.log("[QUEUE] 通过队列内容确认综合队列已经展开")
            return True

        # 面板未展开：寻找左侧蓝色“>”。
        for attempt in range(1, 4):
            fr = b.frame()
            tab = b.vision.match(fr, "queue_expand_tab")

            if not tab:
                b.log(
                    f"[QUEUE] 第{attempt}次未找到左侧蓝色>展开把手，"
                    "等待画面稳定后重试"
                )
                time.sleep(0.35)
                continue

            b.log(
                f"[QUEUE] 找到左侧>展开把手 "
                f"score={tab.score:.3f} "
                f"frame={tab.center}"
            )

            b.actions.tap_match(
                tab,
                fr.shape,
                f"综合队列-展开#{attempt}",
            )

            deadline = time.time() + 1.8
            while time.time() < deadline:
                fr2 = b.frame()

                collapse2 = b.vision.match(
                    fr2,
                    "queue_collapse_tab",
                )
                if collapse2:
                    b.log(
                        f"[OK] 综合队列已展开，"
                        f"检测到<收起把手 "
                        f"score={collapse2.score:.3f}"
                    )
                    return True

                if (
                    b.vision.match(fr2, "queue_panel_training")
                    or b.vision.match(fr2, "queue_training_header")
                    or b.vision.match(fr2, "queue_train_claim_green_v2")
                    or b.vision.match(fr2, "queue_book_idle")
                ):
                    b.log(
                        f"[OK] 综合队列已展开 "
                        f"(第{attempt}次点击成功)"
                    )
                    return True

                time.sleep(0.10)

            # 如果>消失，也可认为展开成功。
            fr3 = b.frame()
            if not b.vision.match(fr3, "queue_expand_tab"):
                b.log(
                    "[QUEUE] >展开把手已消失，"
                    "判定综合队列已展开"
                )
                return True

            time.sleep(0.35)

        b.log(
            "[STOP] 无法识别综合队列的展开/已展开状态，"
            "不会猜坐标点击"
        )
        b.fail_snapshot("queue_panel_state_unknown")
        return False

    def _training_claim_candidates(self, fr):
        if fr is None:
            return []
        b = self.bot
        header = b.vision.match(fr, "queue_training_header")
        if not header:
            return []
        research = b.vision.match(fr, "queue_research_header")
        bottom = (research.bbox[1]
                  if research and research.center[1] > header.center[1]
                  else round(fr.shape[0] * .69))
        candidates = self._multi_template_candidates(
            fr, "queue_train_claim_green_v2", .84, nms_distance=32
        )
        if not candidates:
            candidates = self._multi_template_candidates(
                fr, "queue_train_claim_green", .86, nms_distance=32
            )
        return [c for c in candidates if header.bbox[3] < c[2] < bottom]

    def _training_claim_target(self, fr, exclude=(), threshold=.88,
                               near=None):
        if fr is None:
            return None
        b = self.bot
        scale = fr.shape[1] / 726
        candidates = []
        for name in (
                "training_claim_bubble_1", "training_claim_bubble_2",
                "training_claim_bubble_3", "training_claim_cavalry"):
            bubble = b.vision.match(fr, name, threshold=threshold)
            if bubble and all(
                    math.dist(bubble.center, point) > 68 * scale
                    for point in exclude) and (near is None or
                    math.dist(bubble.center, near) < 125 * scale):
                candidates.append(bubble)
        if threshold < .88 and (len(candidates) < 2 or
                not any(math.dist(a.center, c.center) < 42 * scale
                        for a in candidates for c in candidates if a is not c)):
            return None
        if candidates:
            bubble = max(candidates, key=lambda item: item.score)
            b.log(
                f"[QUEUE] 识别兵营待领取气泡 {bubble.name} "
                f"score={bubble.score:.3f} frame={bubble.center}"
            )
            return bubble.center
        return None

    def _training_claim_once(self):
        """None=没有待领取；False=领取失败；True=回队列确认领取成功。"""
        b = self.bot
        fr = b.frame()
        candidates = self._training_claim_candidates(fr)
        if not candidates:
            return None
        _, cx, cy = candidates[0]
        original_header = b.vision.match(fr, "queue_training_header")
        row_offset = cy - original_header.center[1]
        attempted_targets = []
        for attempt in range(1, 4):
            if not b.actions.tap_frame(cx, cy, fr.shape, f"训兵队列-待领取#{attempt}"):
                return False
            time.sleep(.65)
            current = b.frame()
            collapse = b.vision.match(current, "queue_collapse_tab") if current is not None else None
            if collapse:
                if not b.actions.tap_match(collapse, current.shape, "综合队列-收起"):
                    return False
                time.sleep(.35)
            deadline = time.monotonic() + 7
            target = None
            guide_dismissals = 0
            guide_seen = False
            clear_frames = 0
            last_hand_center = None
            while time.monotonic() < deadline:
                current = b.frame()
                if current is None:
                    time.sleep(.12)
                    continue
                hand = (b.vision.match(current, "training_claim_guide_hand",
                                       threshold=.80)
                        or b.vision.match(current, "training_claim_hand",
                                          threshold=.80))
                if hand:
                    # The guide overlays the intended bubble; another visible
                    # barracks bubble is not evidence that the guide is gone.
                    guide_seen = True
                    clear_frames = 0
                    last_hand_center = hand.center
                    if guide_dismissals >= 4:
                        # Some clients keep the tutorial hand until its
                        # pointed bubble is pressed. Require two independent
                        # bubble templates near the hand before using it.
                        target = self._training_claim_target(
                            current, exclude=attempted_targets,
                            threshold=.78, near=last_hand_center)
                        if target:
                            b.log("[QUEUE] 小手未消失；两种模板确认指向同一领取气泡")
                            break
                        time.sleep(.15)
                        continue
                    xr, yr = ((.52, .76), (.48, .70),
                              (.72, .76), (.30, .75))[guide_dismissals]
                    b.log(f"[QUEUE] 领取引导小手 score={hand.score:.3f}；先点地图空白，再截图定位")
                    if not b.actions.tap_frame(
                            round(current.shape[1] * xr),
                            round(current.shape[0] * yr), current.shape,
                            "训兵领取-空白处关闭引导"):
                        return False
                    guide_dismissals += 1
                    time.sleep(.5)
                    deadline = time.monotonic() + 5
                    continue
                if guide_seen:
                    clear_frames += 1
                    if clear_frames < 2:
                        time.sleep(.2)
                        continue
                target = self._training_claim_target(
                    current, exclude=attempted_targets
                )
                if target:
                    break
                time.sleep(.12)
            if not target:
                b.fail_snapshot("training_claim_hint_missing")
                return False
            b.log(f"[QUEUE] 识别兵营领取提示 frame={target}")
            attempted_targets.append(target)
            if not b.actions.tap_frame(*target, current.shape, "兵营-点击领取气泡"):
                return False
            learner = getattr(b, "learning", None)
            claim_ticket = learner.pending if learner else None
            time.sleep(.8)
            current = b.frame()
            cont = b.vision.match(current, "reward_continue") if current is not None else None
            if cont:
                if not b.actions.tap_match(cont, current.shape, "训兵奖励-继续"):
                    return False
                if not b.wait_main(4):
                    return False
            if not self._open_queue_panel():
                return False
            if not b.wait_template("queue_training_header", 3):
                b.fail_snapshot("training_claim_verify_panel_missing")
                return False
            verified = 0
            same_row = []
            for _ in range(2):
                fr = b.frame()
                header = b.vision.match(fr, "queue_training_header") if fr is not None else None
                if not header:
                    time.sleep(.2)
                    continue
                after = self._training_claim_candidates(fr)
                expected_y = header.center[1] + row_offset
                same_row = [c for c in after if abs(c[2]-expected_y) < fr.shape[0]*.025]
                if not same_row:
                    verified += 1
                time.sleep(.2)
            if verified == 2:
                if learner:
                    learner.finish(claim_ticket, "训兵队列同一行连续两帧待领取勾消失")
                b.log("[OK] 已返回训兵队列，两次确认待领取绿色勾消失")
                return True
            if not header:
                b.fail_snapshot("training_claim_verify_frame_missing")
                return False
            if same_row:
                _, cx, cy = same_row[0]
            b.log("[QUEUE] 原兵营仍待领取，重新定位并重试提示气泡")
            if learner:
                learner.finish(claim_ticket, "原兵营仍待领取", success=False, frame=fr)
        b.fail_snapshot("training_claim_not_confirmed")
        return False

    def _queue_panel_motion_score(self, before, after):
        """
        比较左侧综合队列面板在一次滑动前后是否真的移动。

        优先比较队列标题的位置；标题不可见时再比较面板画面。
        """
        if before is None or after is None:
            return 999.0

        for name in ("queue_training_header", "queue_research_header"):
            old = self.bot.vision.match(before, name)
            new = self.bot.vision.match(after, name)
            if old and new:
                return float(abs(new.center[1] - old.center[1]))

        h, w = before.shape[:2]

        # 只比较左侧黑色队列面板内部。
        x1 = int(w * 0.02)
        x2 = int(w * 0.49)
        y1 = int(h * 0.29)
        y2 = int(h * 0.69)

        a = before[y1:y2, x1:x2]
        b = after[y1:y2, x1:x2]

        if a.size == 0 or b.size == 0:
            return 999.0

        ga = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY)
        gb = cv2.cvtColor(b, cv2.COLOR_BGR2GRAY)

        diff = cv2.absdiff(ga, gb)

        # 去掉少量倒计时数字闪动带来的高差异点，
        # 用中位附近的整体差异更稳定。
        score = float(np.mean(diff))
        return score

    def _queue_seek_training_claim(self):
        """Scan the training section one swipe at a time, verifying motion."""
        b = self.bot

        def swipe_and_read(before, direction, longer=False):
            if before is None:
                return None, None
            h, w = before.shape[:2]
            start_y, end_y = (
                (.43, .69) if direction == "down" and longer else
                (.51, .66) if direction == "down" else
                (.68, .38) if longer else (.63, .50)
            )
            label = f"训兵队列-{'下拉' if direction == 'down' else '上推'}{'加长' if longer else '短距'}"
            if not b.actions.swipe_frame(
                    round(w * .24), round(h * start_y),
                    round(w * .24), round(h * end_y),
                    before.shape, 700 if longer else 520, label):
                return None, None
            time.sleep(.45)
            after = b.frame()
            if after is None:
                return None, None
            motion = self._queue_panel_motion_score(before, after)
            b.log(f"[QUEUE] {label} 画面变化={motion:.2f}")
            return after, motion

        # A reopened panel may retain its previous scroll position. Restore
        # the training header before deciding whether any claims remain.
        for step in range(8):
            fr = b.frame()
            if fr is None:
                return None
            header = b.vision.match(fr, "queue_training_header")
            if header and header.center[1] >= fr.shape[0] * .36:
                break
            after, motion = swipe_and_read(fr, "down")
            if after is None:
                return None
            if self._training_claim_candidates(after):
                return True
            if motion < 3.5:
                after, motion = swipe_and_read(after, "down", longer=True)
                if after is None:
                    return None
                if self._training_claim_candidates(after):
                    return True
                if motion < 3.5:
                    header = b.vision.match(after, "queue_training_header")
                    if header:
                        break
                    b.log("[QUEUE] 两种下拉距离都未移动，且未见训兵标题；位置未知")
                    b.fail_snapshot("training_queue_scroll_unknown")
                    return None
        else:
            b.log("[QUEUE] 回顶部达到滑动上限，未确认训兵标题")
            b.fail_snapshot("training_queue_top_unknown")
            return None

        for step in range(8):
            fr = b.frame()
            if fr is None:
                return None
            if self._training_claim_candidates(fr):
                b.log(f"[QUEUE] 第{step + 1}屏找到待领取训兵")
                return True
            header = b.vision.match(fr, "queue_training_header")
            research = b.vision.match(fr, "queue_research_header")
            if header and research and research.center[1] > header.center[1]:
                b.log("[QUEUE] 训兵标题至研究标题全部可见，未发现待领取")
                return False
            after, motion = swipe_and_read(fr, "up")
            if after is None:
                return None
            if self._training_claim_candidates(after):
                b.log(f"[QUEUE] 第{step + 1}次上推后找到待领取训兵")
                return True
            if motion < 3.5:
                after, motion = swipe_and_read(after, "up", longer=True)
                if after is None:
                    return None
                if self._training_claim_candidates(after):
                    b.log("[QUEUE] 加长上推后找到待领取训兵")
                    return True
                if motion < 3.5:
                    if (b.vision.match(after, "queue_training_header")
                            or b.vision.match(after, "queue_research_header")):
                        b.log("[QUEUE] 短距和加长上推均未移动，确认列表底部")
                        return False
                    b.log("[QUEUE] 两种上推距离均未移动且队列标题不见；位置未知")
                    b.fail_snapshot("training_queue_bottom_unknown")
                    return None
            if not b.vision.match(after, "queue_training_header"):
                b.log("[QUEUE] 训兵标题滑出可识别区，无法安全界定剩余行")
                b.fail_snapshot("training_queue_header_lost")
                return None
        b.log("[QUEUE] 训兵扫描达到滑动上限，未确认列表底部")
        b.fail_snapshot("training_queue_scan_limit")
        return None

    def _queue_ocr(self, frame, roi):
        from local_ocr import read_text
        return read_text(frame, roi, scale=2)

    def _queue_idle_training_row(self):
        """Locate an idle barracks only after two independent row scans agree."""
        b = self.bot
        camp_order = ("步兵", "弓兵", "骑兵")
        observations = []
        latest_frame = None
        for sample in range(5):
            fr = b.frame()
            if fr is None:
                time.sleep(.3)
                continue
            latest_frame = fr
            header = b.vision.match(fr, "queue_training_header")
            if not header:
                b.log("[QUEUE TRAIN] 训兵标题未匹配；不依据旧坐标点击")
                time.sleep(.3)
                continue

            sy = fr.shape[0] / 1600
            header_bottom = header.bbox[3] / sy
            row_states = []
            row_diagnostics = []
            for index, camp_name in enumerate(camp_order):
                row_center = header_bottom + 62 + index * 72
                row_top = max(0, round(row_center - 31))
                row_bottom = min(1600, round(row_center + 31))
                try:
                    state, text = self._queue_training_status(fr, row_center)
                except Exception as exc:
                    b.log(f"[QUEUE TRAIN] {camp_name}行OCR异常：{exc}")
                    row_states.append("unknown")
                    row_diagnostics.append(f"{camp_name}=unknown:OCR异常")
                    continue
                row_states.append(state)
                row_diagnostics.append(f"{camp_name}={state}:{text!r}")
            b.log(f"[QUEUE TRAIN] 第{sample + 1}帧逐行状态：{' | '.join(row_diagnostics)}")
            if "unknown" in row_states:
                time.sleep(.3)
                continue
            observations.append(row_states)
            if len(observations) >= 2 and observations[-1] == observations[-2]:
                break
            time.sleep(.35)

        if len(observations) < 2 or observations[-1] != observations[-2]:
            b.log("[QUEUE TRAIN] 多次重读后仍未取得两帧一致的队列状态")
            b.fail_snapshot("queue_training_idle_unstable")
            return "unknown"
        idle_rows = [index for index, state in enumerate(observations[-1]) if state == "idle"]
        if not idle_rows:
            b.log("[QUEUE TRAIN] 两帧均确认三个兵营当前没有空闲状态")
            return None
        if len(idle_rows) > 1:
            b.log(f"[QUEUE TRAIN] 多个兵营同时显示空闲：{idle_rows}；逐个从高置信状态处理")
        index = idle_rows[0]
        # Re-anchor on a fresh frame; one bad OCR frame must not abort a
        # previously stable idle row.
        for attempt in range(3):
            fr = b.frame()
            header = b.vision.match(fr, "queue_training_header") if fr is not None else None
            if fr is not None and header:
                sy = fr.shape[0] / 1600
                row_center = header.bbox[3] / sy + 62 + index * 72
                try:
                    final_state, final_text = self._queue_training_status(fr, row_center)
                    if final_state == "idle":
                        return {"frame": fr, "y": round(row_center * sy),
                                "camp": camp_order[index]}
                    b.log(f"[QUEUE TRAIN] 点击前第{attempt + 1}次复核：{final_text!r}")
                except Exception as exc:
                    b.log(f"[QUEUE TRAIN] 点击前第{attempt + 1}次OCR异常：{exc}")
            time.sleep(.3)
        b.fail_snapshot("queue_training_idle_final_recheck_failed")
        return "unknown"

    def _queue_training_status(self, frame, row_center):
        from local_ocr import read_text
        # Exclude camp name, action arrow and the next row. The status baseline
        # is slightly above the geometric row centre on the current clients.
        box = (90, round(row_center-23), 266, round(row_center+14))
        texts = []
        for scale, mode in ((2, None), (3, "white"), (4, None)):
            text = "".join(read_text(frame, box, scale=scale, mode=mode,
                                     context=True).get("text", "").split())
            texts.append(text)
            if "空闲中" in text:
                return "idle", text
            if (self._training_duration_from_text(text) is not None or
                    any(token in text for token in ("升级", "训练中", "待领取", "解锁"))):
                return "busy", text
        h, w = frame.shape[:2]
        patch = frame[round(box[1]*h/1600):round(box[3]*h/1600),
                      round(82*w/726):round(272*w/726)]
        hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
        green = cv2.inRange(hsv, (35, 80, 80), (95, 255, 255))
        # A training bar plus a partially legible timer establishes BUSY only;
        # never invent an exact finish time from corrupted digits.
        bar = any(cw >= patch.shape[1]*.22 and ch >= 5*h/1600
                  for contour in cv2.findContours(green, cv2.RETR_EXTERNAL,
                                                  cv2.CHAIN_APPROX_SIMPLE)[0]
                  for _, _, cw, ch in [cv2.boundingRect(contour)])
        if bar and any(len(re.findall(r"\d", t)) >= 4 for t in texts):
            return "busy", "进度条+倒计时片段（时长未读清）:" + " | ".join(texts)
        return "unknown", " | ".join(texts)

    @staticmethod
    def _training_duration_from_text(text):
        # Minute precision is sufficient for training batches. Ignore the
        # seconds because OCR sometimes reads "00:56:14" as "00:56:140".
        text = "".join((text or "").split()).replace("：", ":").replace("﹕", ":")
        # Windows OCR often turns the clock separators into arbitrary glyphs,
        # e.g. "07 �� 43 �� 34". Pick the last plausible H/M/S triplet so a
        # troop count printed earlier in the same crop cannot become hours.
        tokens = list(re.finditer(r"\d{1,3}", text))
        triplets = []
        for index in range(len(tokens) - 2):
            a, minute, second = tokens[index:index + 3]
            sep1 = text[a.end():minute.start()]
            sep2 = text[minute.end():second.start()]
            hours, minutes, seconds = int(a.group()), int(minute.group()), int(second.group())
            if (1 <= len(sep1) <= 6 and 1 <= len(sep2) <= 6
                    and not any(ch.isdigit() for ch in sep1 + sep2)
                    and hours <= 99 and minutes <= 59 and seconds <= 59):
                triplets.append((hours, minutes))
        if triplets:
            hours, minutes = triplets[-1]
            return hours * 3600 + minutes * 60
        match = re.search(r"(?<!\d)(\d{1,3}):([0-5]\d)(?!\d)", text or "")
        if not match:
            return None
        hours, minutes = map(int, match.groups())
        if hours > 99:
            return None
        return hours * 3600 + minutes * 60

    def _queue_training_modal_read(self, frame):
        """Read the duration and the training button from the selection modal."""
        # The train button and its timer are in the MIDDLE of the sheet.
        # The former x=420..720 crop mostly covered the 晋升 button, so the
        # obvious 16:00:17 timer in the saved failure frame read as empty.
        data = self._queue_ocr(frame, (245, 1335, 485, 1455))
        # Windows OCR may insert spaces inside Chinese button labels or timer
        # digits ("训 练", "16 : 35 : 01"). Keep raw text for diagnostics and
        # use a whitespace-free copy for state recognition.
        data["normalized_text"] = "".join(data.get("text", "").split())
        duration = self._training_duration_from_text(data["normalized_text"])
        data["sheet_visible"] = self._queue_training_sheet_visible(frame)
        if duration is None:
            from local_ocr import read_text
            # The timer sits on a blue textured button. Windows OCR can return
            # an empty result on the color crop, so retry contrast-enhanced
            # grayscale/binary versions of that same small timer region.
            h, w = frame.shape[:2]
            scale_x, scale_y = w / 726, h / 1600
            x1, x2 = (round(x * scale_x) for x in (285, 465))
            y1, y2 = (round(y * scale_y) for y in (1380, 1450))
            timer = frame[y1:y2, x1:x2]
            variants = [frame]
            if timer.size:
                gray = cv2.cvtColor(timer, cv2.COLOR_BGR2GRAY)
                clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(4, 4)).apply(gray)
                _, binary = cv2.threshold(clahe, 0, 255,
                                          cv2.THRESH_BINARY + cv2.THRESH_OTSU)
                for processed in (cv2.cvtColor(clahe, cv2.COLOR_GRAY2BGR),
                                  cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR)):
                    candidate = frame.copy()
                    candidate[y1:y2, x1:x2] = processed
                    variants.append(candidate)
            for variant in variants:
                try:
                    focused = read_text(variant, (285, 1380, 465, 1450), scale=4,
                                        context=True)
                    focused_text = "".join(focused.get("text", "").split())
                    parsed = self._training_duration_from_text(focused_text)
                    if parsed is not None:
                        duration = parsed
                        data["normalized_text"] += focused_text
                        data["timer_text"] = focused_text
                        break
                except (OSError, RuntimeError, ValueError) as exc:
                    data["timer_ocr_error"] = str(exc)
        count_words = [word for line in data.get("lines", [])
                       for word in line.get("words", [])
                       if (frame.shape[1] * .66 <= word["x"] <= frame.shape[1] * .84
                           and frame.shape[0] * .79 <= word["y"] <= frame.shape[0] * .85)]
        count_text = "".join(word.get("text", "") for word in
                             sorted(count_words, key=lambda word: word["x"]))
        count_match = re.search(r"\d{1,5}", count_text)
        data["count"] = int(count_match.group()) if count_match else None
        return duration, data

    @staticmethod
    def _queue_training_sheet_visible(frame):
        """Recognize the troop training sheet by its large blue train button."""
        # 晋升 on the right has a slightly narrower blue face than the old
        # .32-screen-width minimum, so allow .28 here.
        h, w = frame.shape[:2]
        x1, x2 = round(w * .49), round(w * .99)
        y1, y2 = round(h * .835), round(h * .91)
        patch = frame[y1:y2, x1:x2]
        if patch.size == 0:
            return False
        hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
        blue = cv2.inRange(hsv, (85, 55, 65), (135, 255, 255))
        contours, _ = cv2.findContours(blue, cv2.RETR_EXTERNAL,
                                       cv2.CHAIN_APPROX_SIMPLE)
        min_width, min_height = round(w * .28), round(h * .025)
        max_height = round(h * .085)
        for contour in contours:
            bx, by, bw, bh = cv2.boundingRect(contour)
            if (bw >= min_width and min_height <= bh <= max_height
                    and by + bh > (y2 - y1) * .45):
                return True
        return False

    def _queue_training_duration_retry(self, previous=None, max_change=None):
        """Retry uncertain timer OCR on fresh frames before changing controls."""
        last_data = None
        for attempt in range(4):
            frame = self.bot.frame()
            if frame is not None:
                try:
                    duration, data = self._queue_training_modal_read(frame)
                except (OSError, RuntimeError, ValueError) as exc:
                    self.bot.log(f"[QUEUE TRAIN] 第{attempt + 1}帧OCR失败：{exc}")
                    time.sleep(.3)
                    continue
                last_data = data
                plausible = (duration is not None and
                             (previous is None or max_change is None or
                              abs(duration - previous) <= max_change))
                if plausible:
                    if attempt:
                        self.bot.log(f"[QUEUE TRAIN] 时长重读第{attempt + 1}帧成功："
                                     f"{duration // 60}分钟")
                    return duration, data
                self.bot.log(f"[QUEUE TRAIN] 时长读数待复核 "
                             f"attempt={attempt + 1} value={duration} "
                             f"OCR={data.get('text', '')!r}")
            time.sleep(.3)
        return None, last_data

    @staticmethod
    def _queue_training_slider_thumb(frame):
        """Find the blue slider handle, excluding the minus and plus buttons."""
        h, w = frame.shape[:2]
        x1, x2 = round(w * .14), round(w * .59)
        y1, y2 = round(h * .795), round(h * .85)
        patch = frame[y1:y2, x1:x2]
        if patch.size == 0:
            return None
        blue = cv2.inRange(cv2.cvtColor(patch, cv2.COLOR_BGR2HSV),
                           (85, 65, 70), (125, 255, 255))
        contours, _ = cv2.findContours(blue, cv2.RETR_EXTERNAL,
                                       cv2.CHAIN_APPROX_SIMPLE)
        candidates = []
        for contour in contours:
            bx, by, bw, bh = cv2.boundingRect(contour)
            cx = x1 + bx + bw / 2
            if (cv2.contourArea(contour) > 180 * (w / 726) * (h / 1600)
                    and 15 * w / 726 <= bw <= 65 * w / 726
                    and 27 * h / 1600 <= bh <= 75 * h / 1600
                    and .15 * w <= cx <= .57 * w):
                candidates.append((cv2.contourArea(contour), round(cx),
                                   round(y1 + by + bh / 2)))
        if not candidates:
            return None
        _, cx, cy = max(candidates)
        return cx, cy

    def _queue_save_training_schedule(self, camp, started_at, duration, midnight,
                                      batch_count=None, remaining_count=None,
                                      planned_total_count=None):
        path = str(getattr(self.bot, "training_schedule_path",
                           os.path.join("runtime", "training_schedule.json")))
        record = {
            "camp": camp,
            "started_at": started_at.isoformat(timespec="seconds"),
            "expected_finish": (started_at + datetime.timedelta(seconds=duration)).isoformat(timespec="seconds"),
            "duration_seconds": int(duration),
            "midnight_deadline": midnight.isoformat(timespec="seconds"),
            "batch_count": batch_count,
            "remaining_count": remaining_count,
            "planned_total_count": planned_total_count,
        }
        try:
            try:
                with open(path, "r", encoding="utf-8") as stream:
                    state = json.load(stream)
            except (OSError, json.JSONDecodeError):
                state = {"plans": []}
            plans = state.get("plans", [])
            plans.append(record)
            state = {"updated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                     "plans": plans[-30:]}
            temporary = path + ".tmp"
            with open(temporary, "w", encoding="utf-8") as stream:
                json.dump(state, stream, ensure_ascii=False, indent=2)
            os.replace(temporary, path)
            self.bot.log(f"[QUEUE TRAIN] 已记录本地计划：{record['expected_finish']} {camp} "
                         f"本批={batch_count} 剩余={remaining_count} file={path}")
            return True
        except OSError as exc:
            self.bot.log(f"[QUEUE TRAIN] 本地训练时间保存失败：{exc}")
            return False

    def _queue_latest_training_schedule(self):
        path = str(getattr(self.bot, "training_schedule_path",
                           os.path.join("runtime", "training_schedule.json")))
        try:
            with open(path, "r", encoding="utf-8") as stream:
                plans = json.load(stream).get("plans", [])
            return plans[-1] if plans else None
        except (OSError, json.JSONDecodeError, TypeError, AttributeError):
            return None

    def _queue_pending_training_schedule(self, camp):
        path = str(getattr(self.bot, "training_schedule_path",
                           os.path.join("runtime", "training_schedule.json")))
        try:
            with open(path, "r", encoding="utf-8") as stream:
                plans = json.load(stream).get("plans", [])
            for plan in reversed(plans):
                if plan.get("camp") == camp:
                    return plan if int(plan.get("remaining_count") or 0) > 0 else None
        except (OSError, json.JSONDecodeError, TypeError, ValueError, AttributeError):
            pass
        return None

    def _queue_training_defer(self):
        """Leave the troop sheet via its visible back button and restore queues."""
        b = self.bot
        fr = b.frame()
        if fr is None or not b.actions.tap_frame(round(fr.shape[1] * .11),
                                                  round(fr.shape[0] * .96),
                                                  fr.shape, "训练暂缓-返回城镇"):
            return False
        b.training_sheet_expected = False
        time.sleep(.5)
        if not self._open_queue_panel():
            b.fail_snapshot("queue_training_defer_return_failed")
            return False
        return "deferred"

    def _reset_city_world_for_entry(self, label="入口重识别", cycles=2):
        """Refresh the world/city camera before a task's entry-template retry."""
        b = self.bot
        if not b.ensure_main():
            return False
        for cycle in range(cycles):
            fr = b.frame()
            if fr is None:
                return False
            exit_button = b.vision.match(fr, "city_exit_button")
            if exit_button:
                if not b.actions.tap_match(exit_button, fr.shape,
                                           f"{label}-复位{cycle + 1}-出城"):
                    return False
                if not b.wait_template("world_city_button", 6):
                    b.fail_snapshot(f"{label}_world_after_exit_missing")
                    return False
            city_button = b.wait_template("world_city_button", 4)
            fr = b.frame()
            if city_button is None or fr is None:
                b.fail_snapshot(f"{label}_city_entry_missing")
                return False
            # Re-anchor the control on the latest frame before tapping.
            city_button = b.vision.match(fr, "world_city_button")
            if city_button is None or not b.actions.tap_match(
                    city_button, fr.shape, f"{label}-复位{cycle + 1}-入城"):
                return False
            if not b.wait_template("city_exit_button", 6):
                b.fail_snapshot(f"{label}_city_after_entry_missing")
                return False
            b.log(f"[NAV] {label}视图复位 {cycle + 1}/{cycles} 完成")
        return True

    def _queue_training_started_on_sheet(self, frame):
        """Confirm the active training row on the troop sheet itself."""
        data = self._queue_ocr(frame, (170, 1150, 690, 1360))
        visible = "".join(data.get("text", "").split())
        return "训练中" in visible and self._training_duration_from_text(visible) is not None, visible

    def _queue_training_building_detail(self, frame, camp):
        """Recognize the barracks detail popover that precedes its training sheet."""
        title = self._queue_ocr(frame, (250, 610, 470, 700))
        controls = self._queue_ocr(frame, (420, 850, 560, 965))
        title_text = "".join(title.get("text", "").split())
        controls_text = "".join(controls.get("text", "").split())
        return camp in title_text and "营" in title_text and "训练" in controls_text, (
            title_text, controls_text)

    def _queue_promotion_count(self, frame):
        """Read the selected low-tier troop count beneath the green arrow."""
        from local_ocr import read_text
        if frame is None:
            return None
        for scale in (2, 3):
            try:
                text = read_text(frame, (485, 990, 565, 1050), scale=scale,
                                 context=True).get("text", "")
            except (OSError, RuntimeError, ValueError):
                continue
            numbers = re.findall(r"\d{1,7}", text)
            if len(numbers) == 1:
                return int(numbers[0])
        return None

    def _queue_promote_first(self, row, frame):
        """Prefer a confirmed low-tier promotion; never repeat an unknown tap."""
        b = self.bot
        promote = b.vision.match(frame, "training_promote_button")
        arrows = [b.vision.match(frame, name) for name in
                  ("training_promote_arrow_inner", "training_promote_arrow")]
        arrow = max((match for match in arrows if match),
                    key=lambda match: match.score, default=None)
        if not promote or not arrow:
            return None
        before = self._queue_promotion_count(frame)
        b.log(f"[QUEUE PROMOTE] {row['camp']}低级兵可晋升："
              f"按钮={promote.score:.3f} 绿箭头={arrow.score:.3f} "
              f"原数量={before}")
        if not b.actions.tap_match(promote, frame.shape,
                                   f"{row['camp']}-优先晋升"):
            return False
        deadline = time.monotonic() + 7
        while time.monotonic() < deadline:
            time.sleep(.4)
            fresh = b.frame()
            if fresh is None:
                continue
            sheet = b.vision.match(fresh, "training_sheet_back")
            after = self._queue_promotion_count(fresh) if sheet else None
            if before is not None and after is not None and after < before:
                b.log(f"[OK] {row['camp']}晋升后低级兵数量 {before} -> {after}")
                if not self._tap_game_back(f"{row['camp']}-晋升后返回"):
                    return False
                b.training_sheet_expected = False
                if not b.wait_main(5) or not self._open_queue_panel():
                    b.fail_snapshot("queue_promotion_panel_restore_failed")
                    return False
                return "started"
        b.fail_snapshot("queue_promotion_result_unknown")
        b.log("[QUEUE PROMOTE] 已点一次晋升，但数量未确认减少；"
              "不重复晋升，也不改点训练")
        return False

    def _queue_start_idle_training(self):
        """Start an idle barracks; trim a crossing batch to finish after midnight."""
        b = self.bot
        now = datetime.datetime.now().astimezone()
        previous_plan = self._queue_latest_training_schedule()
        if previous_plan:
            try:
                due = datetime.datetime.fromisoformat(previous_plan["expected_finish"])
                relation = "预计已完成" if due <= now else "预计仍在训练"
                b.log(f"[QUEUE TRAIN] 本地记录{previous_plan.get('camp', '兵营')}"
                      f"{relation}：{due:%Y-%m-%d %H:%M:%S}；以队列当前画面复核")
            except (KeyError, ValueError, TypeError):
                b.log("[QUEUE TRAIN] 本地计划时间格式异常，将以当前队列画面判断")
        row = self._queue_idle_training_row()
        if row == "unknown":
            return False
        if not row:
            return "none"
        fr = row["frame"]
        h, w = fr.shape[:2]
        if not b.actions.tap_frame(round(w * .43), row["y"], fr.shape,
                                   f"{row['camp']}-空闲行前往训练"):
            return False
        b.training_sheet_expected = True

        duration = None
        modal_data = None
        deadline = time.monotonic() + 8
        building_train_taps = 0
        while time.monotonic() < deadline:
            fr = b.frame()
            if fr is not None:
                promoted = self._queue_promote_first(row, fr)
                if promoted is not None:
                    return promoted
                duration, modal_data = self._queue_training_modal_read(fr)
                if (duration is not None and
                        ("训练" in modal_data.get("normalized_text", "") or
                         modal_data.get("sheet_visible"))):
                    break
                # Some barracks first open a building action popover. Confirm
                # both its matching camp title and the visible 训练 control.
                if building_train_taps < 3:
                    tutorial_hand = None
                    for hand_name in ("barracks_training_hand_alt",
                                      "barracks_training_hand"):
                        tutorial_hand = b.vision.match(fr, hand_name)
                        if tutorial_hand:
                            break
                    if tutorial_hand:
                        detail = True
                        detail_text = (f"{row['camp']}营小手提示", "训练")
                    else:
                        try:
                            detail, detail_text = self._queue_training_building_detail(
                                fr, row["camp"])
                        except Exception as exc:
                            detail, detail_text = False, ("", str(exc))
                    if detail:
                        b.log(f"[QUEUE TRAIN] 已确认{detail_text[0]}详情页训练入口，继续进入训练")
                        if not b.actions.tap_frame(round(fr.shape[1] * .67),
                                                   round(fr.shape[0] * .565),
                                                   fr.shape,
                                                   f"{row['camp']}-详情页进入训练#{building_train_taps + 1}"):
                            return False
                        building_train_taps += 1
                        time.sleep(.45)
                        continue
            time.sleep(.35)
        if (duration is None or modal_data is None or not (
                "训练" in modal_data.get("normalized_text", "") or
                modal_data.get("sheet_visible"))):
            b.log("[QUEUE TRAIN] 未确认训练弹窗时长和训练按钮，停止点击；"
                  f"duration={duration} OCR={modal_data.get('text', '') if modal_data else ''!r} "
                  f"sheet={modal_data.get('sheet_visible') if modal_data else None} "
                  f"timer={modal_data.get('timer_text') if modal_data else None}")
            b.fail_snapshot("queue_training_modal_unconfirmed")
            return False

        now = datetime.datetime.now().astimezone()
        midnight = (now + datetime.timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        def time_to_midnight():
            return max(0, int((midnight - datetime.datetime.now().astimezone()).total_seconds()))

        def target_duration():
            # Allow a few minutes beyond midnight for OCR and adjustment time.
            return time_to_midnight() + 4 * 60

        target = target_duration()
        b.log(f"[QUEUE TRAIN] {row['camp']}当前时长约{duration // 60}分钟，"
              f"距午夜约{time_to_midnight() // 60}分钟；"
              f"跨午夜时目标约{target // 60}分钟")
        needs_split = duration > time_to_midnight()

        # The blue handle spans roughly x=0.155..0.548 of the game frame.
        # Drag it proportionally first; re-read the displayed time after every
        # drag because the game rounds the selected troop count.
        for attempt in range(3 if needs_split and duration > target + 5 * 60 else 0):
            target = target_duration()
            if abs(duration - target) <= 5 * 60:
                break
            fr = b.frame()
            if fr is None:
                return False
            thumb = self._queue_training_slider_thumb(fr)
            if thumb is None:
                b.log("[QUEUE TRAIN] 未定位蓝色滑块，改用减号微调")
                break
            thumb_x, thumb_y = thumb
            left = round(fr.shape[1] * .155)
            right = round(fr.shape[1] * .548)
            next_x = max(left, min(right, round(left + (thumb_x - left) * target / duration)))
            if abs(thumb_x - next_x) < 9:
                break
            if not b.actions.swipe_frame(thumb_x, thumb_y, next_x, thumb_y,
                                         fr.shape, 650,
                                         f"{row['camp']}-拖动训练进度条#{attempt + 1}"):
                return False
            time.sleep(.45)
            new_duration, modal_data = self._queue_training_duration_retry()
            if new_duration is None:
                b.log(f"[QUEUE TRAIN] 多次重读后仍未确认拖动时长："
                      f"{modal_data.get('text', '') if modal_data else ''!r}")
                b.fail_snapshot("queue_training_drag_duration_unknown")
                return self._queue_training_defer()
            b.log(f"[QUEUE TRAIN] 拖动后时长约{new_duration // 60}分钟，"
                  f"目标{target // 60}分钟")
            if ((next_x < thumb_x and new_duration >= duration)
                    or (next_x > thumb_x and new_duration <= duration)):
                b.log("[QUEUE TRAIN] 进度条拖动未按预期改变时长，改用加减号微调")
                break
            duration = new_duration

        # Plus and minus change only a small amount. Use them around the
        # target; minute-level agreement is enough and avoids excessive taps.
        unchanged = 0
        for adjustment in range(45 if needs_split else 0):
            target = target_duration()
            if time_to_midnight() + 60 <= duration <= target + 5 * 60:
                break
            decrease = duration > target + 5 * 60
            fr = b.frame()
            if fr is None:
                return False
            button_x = .105 if decrease else .615
            label = "减" if decrease else "加"
            if not b.actions.tap_frame(round(fr.shape[1] * button_x),
                                       round(fr.shape[0] * .82), fr.shape,
                                       f"{row['camp']}-{label}号微调#{adjustment + 1}"):
                return False
            time.sleep(.35)
            new_duration, modal_data = self._queue_training_duration_retry(
                previous=duration, max_change=10 * 60)
            if new_duration is None:
                b.log(f"[QUEUE TRAIN] 多次重读后仍未确认微调时长："
                      f"{modal_data.get('text', '') if modal_data else ''!r}")
                b.fail_snapshot("queue_training_duration_unreadable")
                return self._queue_training_defer()
            if new_duration == duration:
                unchanged += 1
                if unchanged >= 2:
                    if not decrease and duration > time_to_midnight():
                        b.log("[QUEUE TRAIN] 已无法继续增加训练量，使用当前跨午夜时长")
                        break
                    b.log("[QUEUE TRAIN] 加减号连续两次未改变分钟数")
                    b.fail_snapshot("queue_training_adjust_no_effect")
                    return False
            else:
                unchanged = 0
            duration = new_duration
            b.log(f"[QUEUE TRAIN] {label}号后约{duration // 60}分钟，"
                  f"目标{target // 60}分钟")
        if duration < 60 or (needs_split and not (
                time_to_midnight() < duration <= target_duration() + 5 * 60)):
            b.log(f"[QUEUE TRAIN] 调整后时长{duration // 60}分钟未落在跨午夜窗口，暂缓启动")
            b.fail_snapshot("queue_training_not_past_midnight")
            return self._queue_training_defer()

        button_text = modal_data.get("normalized_text", "") if modal_data else ""
        if ("训练" not in button_text and
                not modal_data.get("sheet_visible", False)):
            b.fail_snapshot("queue_training_start_button_unconfirmed")
            return False
        fr = b.frame()
        if fr is None:
            return False
        if not b.actions.tap_frame(round(fr.shape[1] * .73), round(fr.shape[0] * .88),
                                  fr.shape, f"{row['camp']}-开始训练"):
            return False
        confirmed = False
        visible = ""
        for _ in range(5):
            time.sleep(.5)
            fr = b.frame()
            if fr is None:
                continue
            confirmed, visible = self._queue_training_started_on_sheet(fr)
            if confirmed:
                break
        if not confirmed:
            b.log(f"[QUEUE TRAIN] 当前训兵界面未确认‘训练中’及倒计时：{visible!r}")
            b.fail_snapshot("queue_training_start_unconfirmed")
            return False
        learner = getattr(b, "learning", None)
        if learner:
            learner.verified(f"{row['camp']}训练中及倒计时OCR: {visible}")
        started_at = datetime.datetime.now().astimezone()
        saved = self._queue_save_training_schedule(row["camp"], started_at, duration, midnight)
        if not saved:
            b.log("[QUEUE TRAIN] 训练已启动，计划记录失败；仍先返回游戏主界面")
        b.log(f"[OK] {row['camp']}已开始训练，"
              f"预计完成 {started_at + datetime.timedelta(seconds=duration):%Y-%m-%d %H:%M:%S}")
        if not self._tap_game_back(f"{row['camp']}-训练成功返回"):
            b.fail_snapshot("queue_training_success_return_failed")
            return False
        b.training_sheet_expected = False
        time.sleep(.5)
        if not self._open_queue_panel():
            b.fail_snapshot("queue_training_success_panel_restore_failed")
            return False
        return "started"

    def _queue_find_book_idle_full_scan(self):
        """
        v8 综合队列扫描：

        重点修复：
        1. 兵书研习队列 和 研究队列不是同一个东西。
        2. 不再使用“时间条/绿色区域”直接判断。
        3. 当前屏、向上推、向下拉都扫描。
        4. 每次滑动后立即截图识别，避免滑过目标。
        """

        b = self.bot

        def detect(tag):
            fr = b.frame()
            if fr is None:
                return None

            # 第一优先：精确兵书队列模板
            for name in (
                "queue_book_idle",
                "queue_book_idle_v2",
                "queue_study_queue",
            ):
                try:
                    m = b.vision.match(fr, name)
                except Exception:
                    m = None
                if m:
                    b.log(
                        f"[QUEUE V8] {tag} 命中兵书研习模板 "
                        f"{name} score={m.score:.3f}"
                    )
                    return m

            # 第二优先：兵书专属图标
            for name in (
                "queue_book_icon",
                "queue_book_scroll",
                "book_idle_icon",
            ):
                try:
                    m = b.vision.match(fr, name)
                except Exception:
                    m = None
                if m:
                    b.log(
                        f"[QUEUE V8] {tag} 命中兵书图标 {name} "
                        f"score={m.score:.3f}"
                    )
                    return m

            # 明确排除普通研究队列
            try:
                research = b.vision.match(fr, "research_queue")
                if research:
                    b.log(
                        f"[QUEUE V8] {tag} 发现研究队列，忽略 "
                        f"score={research.score:.3f}"
                    )
            except Exception:
                pass

            return None

        # 当前屏
        hit = detect("当前屏")
        if hit:
            return hit

        fr = b.frame()
        if fr is None:
            return None

        h, w = fr.shape[:2]

        # 在黑色综合队列框内部滑动
        # 上推=看下面项目
        # 下拉=回顶部确认
        actions = [
            ("向上推第1次", .24,.64,.24,.42),
            ("向上推第2次", .24,.64,.24,.42),
            ("向上推第3次", .24,.64,.24,.42),
            ("向下回扫第1次", .24,.42,.24,.64),
            ("向下回扫第2次", .24,.42,.24,.64),
        ]

        for name,x1,y1,x2,y2 in actions:
            b.log(f"[QUEUE V8] {name}")

            before = b.frame()
            b.actions.swipe_frame(
                int(w*x1), int(h*y1),
                int(w*x2), int(h*y2),
                before.shape,
                900,
                f"综合队列-{name}"
            )

            time.sleep(0.7)

            hit = detect(name)
            if hit:
                return hit

        b.log("[QUEUE V8] 双向扫描结束，没有确认兵书研习队列")
        return None

    def _close_queue_panel_after_scan(self):
        """
        扫描完成后安全收起综合队列。

        优先识别蓝色“<”；
        若模板偶尔漏识别，但已经明确处于展开面板中，
        则点击已确认的固定收起把手区域。
        """
        b = self.bot
        fr = b.frame()

        collapse = b.vision.match(
            fr,
            "queue_collapse_tab",
        )

        if collapse:
            b.log(
                f"[QUEUE] 使用<收起综合队列 "
                f"score={collapse.score:.3f}"
            )
            b.actions.tap_match(
                collapse,
                fr.shape,
                "综合队列-<收起",
            )
        else:
            # 用户截图中收起把手长期固定在：
            # raw 约 x=400, y=780；
            # 即 x≈55%, y≈49%。
            h, w = fr.shape[:2]
            x = int(w * 0.55)
            y = int(h * 0.49)

            b.log(
                "[QUEUE] <模板未命中，但已完成面板扫描；"
                f"点击已确认收起把手安全区 frame=({x},{y})"
            )
            b.actions.tap_frame(
                x,
                y,
                fr.shape,
                "综合队列-固定安全区收起",
            )

        time.sleep(0.55)
        return True

    def capture_queue_scene(self, out_dir="templates_capture/queue"):
        """
        采集综合队列场景。

        使用正式任务同一套逻辑：
        1. 打开综合队列
        2. 扫描兵书研习队列
        3. 保存关键帧

        不点击“前往”、不进入建筑、不启动研究。
        用于模板采集和排查识别遗漏。
        """
        b = self.bot

        os.makedirs(out_dir, exist_ok=True)
        b.log("\\n[CAPTURE] 开始采集兵书研习队列场景")

        if not self._ensure_city():
            return False

        if not self._open_queue_panel():
            b.log("[CAPTURE] 综合队列打开失败")
            return False

        def save(name):
            fr = b.frame()
            if fr is None:
                return
            path = os.path.join(out_dir, name)
            cv2.imwrite(path, fr)
            b.log(f"[CAPTURE] 保存 {path}")

        save("00_queue_open.png")

        hit = self._queue_find_book_idle_full_scan()

        save("01_queue_scan_finished.png")

        if hit:
            fr = b.frame()
            h, w = fr.shape[:2]
            x, y = hit.center
            x1=max(0, int(x-120)); x2=min(w, int(x+120))
            y1=max(0, int(y-80)); y2=min(h, int(y+80))
            crop=fr[y1:y2, x1:x2]
            cv2.imwrite(os.path.join(out_dir, "02_book_idle_hit.png"), crop)
            b.log(f"[CAPTURE] 命中兵书队列 center={hit.center}")
        else:
            b.log("[CAPTURE] 未命中兵书队列")

        self._close_queue_panel_after_scan()
        return bool(hit)

    def queue_maintenance(self, capture_only=False):
        b=self.bot
        b.log("\n[FLOW] 综合队列：训兵领取 + 兵书研习")
        if not self._ensure_city(): return False

        # 每次领取后队列可能回到不同滚动位置，重新从顶部逐屏扫描。
        for idx in range(6):
            if not self._open_queue_panel():
                return False
            pending = self._queue_seek_training_claim()
            if pending is None:
                return False
            if not pending:
                b.log("[INFO] 已逐屏确认没有更多‘待领取’训兵")
                break
            claimed = self._training_claim_once()
            if claimed is not True:
                b.log("[QUEUE] 首次领取未确认；安全返回主城、重进队列后重新定位一次")
                if not b.ensure_main() or not self._open_queue_panel():
                    return False
                pending = self._queue_seek_training_claim()
                if pending is True and self._training_claim_once() is True:
                    b.log("[QUEUE] 复位后领取成功，继续扫描")
                    continue
                b.log("[QUEUE] 复位后仍无法确认原领取状态，保留未知")
                b.fail_snapshot("training_claim_after_reset_unknown")
                return False
        else:
            b.log("[QUEUE] 训兵领取达到安全上限，仍需复查")
            b.fail_snapshot("training_claim_limit")
            return False

        # Start idle training; a batch that crosses midnight ends just after it.
        # Re-read the panel after each batch so another idle barracks is not
        # confused with the row just started.
        for _ in range(3):
            started = self._queue_start_idle_training()
            if started == "none" or started == "deferred":
                break
            if started is not True and started != "started":
                return False

        # 训兵处理完成后，不再重新找“>”。
        # 直接在当前左侧综合队列面板里做双向完整扫描：
        # 每滑一步就识别一次；一个方向到边界后反向继续扫。
        idle = self._queue_find_book_idle_full_scan()

        if not idle:
            b.log(
                "[INFO] 综合队列没有确认‘兵书研习队列’，"
                "不根据相似研究队列猜测点击"
            )

            self._close_queue_panel_after_scan()

            # 备用路线：
            # 后续可以从主城寻找研究建筑，
            # 不在这里误点其它研究入口。
            return b.ensure_main()

        if capture_only:
            b.log("[CAPTURE] 已确认兵书研习队列，采集模式停止点击前往")
            return True

        fr=b.frame(); h,w=fr.shape[:2]
        # 行右侧蓝色按钮固定在面板右沿；y取idle中心。
        b.actions.tap_frame(
            int(w*0.43),
            idle.center[1],
            fr.shape,
            "兵书研习队列-前往"
        )

        # 等待城市定位到研究建筑。
        # 不同赛季研究建筑名称可能变化：
        # 济世堂 / 太学院 等均可能承担兵书入口。
        time.sleep(0.85)

        # 新逻辑：
        # 1. 优先识别济世堂
        # 2. 再识别通用研究建筑页面
        # 3. 最后才使用旧按钮模板兜底
        anchor = b.wait_template(
            "study_building_anchor_v2",
            3
        )

        if not anchor:
            b.log("[QUEUE] 未找到备用建筑模板，跳过备用模板")

        if not anchor:
            # 兼容旧模板作为兜底。
            study = b.wait_template(
                "building_study_button",
                1.5
            )
            if study:
                b.log(
                    "[QUEUE] 新济世堂锚点未命中，"
                    "旧研习按钮模板命中，使用旧模板"
                )
                b.actions.tap_match(
                    study,
                    b.frame().shape,
                    "济世堂-研习(旧模板兜底)"
                )
            else:
                # 某些赛季建筑气泡/按钮布局变化，
                # 不直接失败，尝试固定右侧研习区域。
                fr_retry = b.frame()
                hr, wr = fr_retry.shape[:2]

                sx = int(wr * 0.68)
                sy = int(hr * 0.58)

                b.log(
                    "[QUEUE] 未识别建筑标题，尝试研究建筑右侧研习按钮"
                    f" frame=({sx},{sy})"
                )

                b.actions.tap_frame(
                    sx,
                    sy,
                    fr_retry.shape,
                    "研究建筑-研习兜底"
                )

                time.sleep(1)

                if not b.wait_template("page_study_home",3):
                    b.log(
                        "[STOP] 研究建筑入口无法确认"
                    )
                    b.fail_snapshot(
                        "study_building_anchor_missing"
                    )
                    return False
        else:
            b.log(
                f"[QUEUE] 已定位济世堂 "
                f"score={anchor.score:.3f}"
            )

            # 用户实测页面中三个圆形按钮：
            # 左=详情，中=升级，右=研习。
            # “研习”中心约在 raw frame (493, 930)。
            fr2 = b.frame()
            h2, w2 = fr2.shape[:2]

            study_x = int(w2 * 0.68)
            study_y = int(h2 * 0.58)

            b.log(
                f"[QUEUE] 点击济世堂右侧“研习” "
                f"frame=({study_x},{study_y})"
            )
            b.actions.tap_frame(
                study_x,
                study_y,
                fr2.shape,
                "济世堂-研习"
            )

        # 点击后必须确认真正进入兵书研习主页面。
        if not b.wait_template("page_study_home",5):
            # 手势动画偶尔可能吞掉第一次点击，再安全重试一次。
            anchor2 = b.vision.match(
                b.frame(),
                "study_building_anchor_v2"
            )

            if anchor2:
                fr3 = b.frame()
                h3, w3 = fr3.shape[:2]
                study_x = int(w3 * 0.68)
                study_y = int(h3 * 0.58)

                b.log(
                    "[QUEUE] 第一次“研习”点击后仍在济世堂，"
                    "安全重试一次"
                )
                b.actions.tap_frame(
                    study_x,
                    study_y,
                    fr3.shape,
                    "济世堂-研习重试"
                )

                if b.wait_template("page_study_home",4):
                    b.log(
                        "[OK] 第2次点击后进入兵书研习主页面"
                    )
                else:
                    b.fail_snapshot(
                        "study_home_missing_after_retry"
                    )
                    return False
            else:
                b.fail_snapshot("study_home_missing")
                return False

        # 赛季标题会变，只点当前已解锁卷轴中心区域。
        self._tap_rel(0.497,0.307,"当前已解锁卷轴")
        if not b.wait_template("page_study_tree",5):
            b.fail_snapshot("study_tree_missing"); return False

        # 从左到右、从上到下尝试节点。只要出现蓝色“研究”就使用。
        candidates=[
            (0.125,0.435),(0.295,0.435),(0.47,0.435),(0.64,0.435),(0.83,0.435),
            (0.125,0.57),(0.295,0.57),(0.47,0.57),(0.64,0.57),(0.83,0.57),
            (0.50,0.72),
            (0.125,0.85),(0.295,0.85),(0.47,0.85),(0.83,0.85),
        ]
        started=False
        for xr,yr in candidates:
            self._tap_rel(xr,yr,"兵书节点-从左到右尝试")
            time.sleep(0.25)

            # 新增：节点详情弹窗优先处理。
            # 实际页面中点击节点后会弹出“炮丁之道”等详情，
            # 蓝色【研究】按钮在弹窗底部，不一定能被旧模板识别。
            # 先确认弹窗，再寻找研究按钮。
            popup = b.vision.match(b.frame(),"study_popup")
            if popup:
                b.log("[STUDY] 检测到兵书节点详情弹窗")

                if b.wait_template("study_research_blue",1.5):
                    b.tap_template("study_research_blue",1,"兵书弹窗-研究")
                    started=True
                    break

                # 兜底：根据已采集弹窗，研究按钮位于底部右侧。
                frp=b.frame()
                hp,wp=frp.shape[:2]
                rx=int(wp*0.73)
                ry=int(hp*0.92)
                b.log(f"[STUDY] 模板未命中，点击弹窗研究安全区 frame=({rx},{ry})")
                b.actions.tap_frame(rx,ry,frp.shape,"兵书弹窗-研究安全区")
                time.sleep(0.8)

                # 点击后如果弹窗消失，认为启动成功。
                if not b.vision.match(b.frame(),"study_popup"):
                    started=True
                    break

                # 仍在弹窗，关闭继续尝试其它节点。
                self._tap_rel(0.924,0.307,"兵书节点弹窗-X")
                time.sleep(0.2)
                continue

            # 无弹窗的旧页面逻辑保留。
            if b.wait_template("study_research_blue",0.8):
                # 绝不点黄色“立刻完成”。
                b.tap_template("study_research_blue",1,"兵书-研究")
                started=True
                break
        if not started:
            b.log("[WARN] 没找到可研究节点，未消耗任何资源")

        # 启动后按游戏内返回路径退回城内主界面。
        for _ in range(4):
            if b.vision.match(b.frame(),"city_exit_button"):
                break
            self._tap_game_back("兵书研习-返回")
            time.sleep(0.45)
        return b.ensure_main()

    # ============================================================
    # 同盟商队：每次最多加入一个，金色优先、蓝色其次
    # ============================================================

    def alliance_caravan(self):
        b = self.bot
        b.log("\n[FLOW] 同盟商队：金色优先、蓝色其次；本次最多加入一个")
        if not b.ensure_main():
            return False
        entry = None
        for _ in range(3):
            frame = b.frame()
            entry = b.vision.match(frame, "caravan_city_icon") if frame is not None else None
            if entry:
                break
            time.sleep(.3)
        if not entry:
            b.log("[CARAVAN] 主城未确认同盟商队入口，保留等待")
            b.task_waiting_reason = "主城未出现已知同盟商队入口"
            return True
        b.log(f"[CARAVAN] 入口 score={entry.score:.3f} frame={entry.center}")
        if not b.actions.tap_match(entry, frame.shape, "同盟商队-进入"):
            return False
        if not b.wait_template("caravan_page_title", 6):
            b.fail_snapshot("caravan_page_missing")
            return False
        frame = b.frame()
        title = b.vision.match(frame, "caravan_page_title")
        if title is None:
            b.fail_snapshot("caravan_page_unstable")
            return False
        candidates = self._multi_template_candidates(
            frame, "caravan_plus", .80, nms_distance=85)
        # Supplied page has three cart rows. Relative to the page title, the
        # first row is gold and the second blue; the third is lower priority.
        scale = frame.shape[0] / 1600
        gold = [c for c in candidates if 475*scale < c[2]-title.center[1] < 610*scale]
        blue = [c for c in candidates if 665*scale < c[2]-title.center[1] < 790*scale]
        chosen = max(gold or blue, key=lambda item: item[0], default=None)
        b.log("[CARAVAN] 空位候选=" + ", ".join(
            f"({x},{y})/{score:.3f}" for score, x, y in candidates))
        if chosen is None:
            b.task_waiting_reason = "未确认可加入的金色或蓝色商队空位"
            return bool(self._tap_game_back("同盟商队-无可加入空位返回")
                        and b.wait_main(5))
        score, x, y = chosen
        kind = "金色" if gold else "蓝色"
        b.log(f"[CARAVAN] 选择{kind}空位 score={score:.3f} frame=({x},{y})")
        if not b.actions.tap_frame(x, y, frame.shape, f"同盟商队-加入{kind}空位"):
            return False
        # One plus opens the gift dialog. The user confirmed the LEFT action,
        # 点赞并赠言, is the one to use. Never click the right 送出礼物 button.
        if not b.wait_template("caravan_dialog_title", 5):
            b.fail_snapshot("caravan_dialog_missing")
            return False
        dialog = b.frame()
        left = b.vision.match(dialog, "caravan_like_message") if dialog is not None else None
        if left is None:
            b.fail_snapshot("caravan_like_message_missing")
            return False
        b.log(f"[CARAVAN] 赠礼弹窗已确认；点赞并赠言 score={left.score:.3f} "
              f"frame={left.center}")
        if not b.actions.tap_match(left, dialog.shape, "同盟商队-点赞并赠言"):
            return False
        # Verify the selected row on a fresh frame. A disappearing plus alone
        # could be a transient animation; the green pin is membership evidence.
        deadline = time.time() + 6
        while time.time() < deadline:
            after = b.frame()
            if after is None:
                time.sleep(.2)
                continue
            if b.vision.match(after, "caravan_dialog_title"):
                time.sleep(.25)
                continue
            if not b.vision.match(after, "caravan_page_title"):
                time.sleep(.25)
                continue
            remaining = self._multi_template_candidates(
                after, "caravan_plus", .80, nms_distance=85)
            same_slot = any(abs(cx-x) < 35*frame.shape[1]/726 and
                            abs(cy-y) < 45*frame.shape[0]/1600
                            for _, cx, cy in remaining)
            pins = self._multi_template_candidates(
                after, "caravan_joined_pin", .80, nms_distance=22)
            pin = next(((score, px, py) for score, px, py in pins
                        if abs(py-y) < 55*frame.shape[0]/1600 and
                        25*frame.shape[1]/726 < px-x < 100*frame.shape[1]/726),
                       None)
            pin_at_row = pin is not None
            if pin_at_row and not same_slot:
                b.log(f"[OK] 已加入一个{kind}商队：选中空位消失，绿色定位标记 "
                      f"score={pin[0]:.3f} frame=({pin[1]},{pin[2]})")
                b.task_completion_evidence = f"{kind}商队绿色定位标记"
                break
            time.sleep(.3)
        else:
            b.fail_snapshot("caravan_join_not_confirmed")
            b.log("[CARAVAN] 点赞并赠言后未确认绿色定位标记；本次不再点击其他加号")
            return False
        if not self._tap_game_back("同盟商队-返回主城"):
            return False
        return b.wait_main(5)

    # ============================================================
    # 行商商店：只买材料价，免费刷新到消失
    # ============================================================

    def _merchant_entry_match(self, fr):
        if fr is None:
            return None
        for name in ("merchant_icon_inner", "merchant_bubble", "merchant_name"):
            m = self.bot.vision.match(fr, name)
            if m:
                return m
        return None

    def _record_merchant_position(self, match, frame, scan_number, source):
        """Persist the merchant screen coordinate and compare later scans."""
        height, width = frame.shape[:2]
        x, y = match.center
        record = {
            "time": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "scan": int(scan_number), "source": source,
            "template": match.name, "score": round(float(match.score), 4),
            "frame_size": [int(width), int(height)],
            "center": [int(x), int(y)],
            "normalized": [round(x / width, 5), round(y / height, 5)],
        }
        path = getattr(self, "merchant_entry_log_path", os.path.join(
            "runtime", "debug", "merchant_entry_coords.jsonl"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        previous = None
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip(): previous = json.loads(line)
        except (OSError, json.JSONDecodeError):
            previous = None
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        if previous and previous.get("normalized"):
            dx = record["normalized"][0] - previous["normalized"][0]
            dy = record["normalized"][1] - previous["normalized"][1]
            self.bot.log(
                f"[MERCHANT POS] scan={scan_number} score={match.score:.3f} "
                f"frame=({x},{y}) previous=({previous['center'][0]},{previous['center'][1]}) "
                f"delta=({dx:+.4f},{dy:+.4f}) source={source}"
            )
        else:
            self.bot.log(
                f"[MERCHANT POS] ???????? frame=({x},{y}) "
                f"score={match.score:.3f} source={source} file={path}"
            )

    @staticmethod
    def _merchant_scan_patterns():
        """Scan in all four directions across both sides of the city view."""
        return [
            (.72, .76, .72, .34),  # up
            (.76, .50, .28, .50),  # left
            (.28, .34, .28, .76),  # down
            (.28, .72, .76, .72),  # right
            (.28, .76, .28, .34),  # up, opposite lane
            (.76, .34, .28, .34),  # left, upper lane
            (.72, .34, .72, .76),  # down, opposite lane
            (.28, .50, .76, .50),  # right, middle lane
        ]

    def _merchant_try_entry(self, m, fr):
        b = self.bot
        for attempt in range(2):
            b.log(
                f"[MERCHANT] 商行入口 {m.name} score={m.score:.3f} "
                f"frame={m.center} attempt={attempt + 1}"
            )
            if not b.actions.tap_match(m, fr.shape, "商行入口"):
                return False
            if b.wait_template("page_merchant", 2.8):
                b.log("[OK] 已进入行商商店")
                return True
            # 只能重试当前画面中仍存在的入口，不能沿用移动前的坐标。
            fr = b.frame()
            m = self._merchant_entry_match(fr)
            if not m:
                break
        b.log("[WARN] 商行候选点击后未确认商店页面")
        return False

    def _merchant_drag_name(self, fr, pattern, label, template_name="merchant_name"):
        """拖动后按住不动，读取短暂出现的建筑名；松手后才允许点击。"""
        if fr is None:
            return None
        b = self.bot
        h, w = fr.shape[:2]
        x1, y1, x2, y2 = (
            round(w * pattern[0]), round(h * pattern[1]),
            round(w * pattern[2]), round(h * pattern[3]),
        )
        x, y = x1, y1
        candidate = None
        try:
            if not b.actions.down_frame(x, y, fr.shape, label):
                raise RuntimeError("商行定位：触点 DOWN 失败")
            for step in range(1, 5):
                x = round(x1 + (x2 - x1) * step / 4)
                y = round(y1 + (y2 - y1) * step / 4)
                if not b.actions.move_frame(x, y, fr.shape, label):
                    raise RuntimeError("商行定位：触点 MOVE 失败")
                time.sleep(0.04)

            # 先消除地图惯性和视频延迟，再要求连续两次文字坐标稳定。
            time.sleep(0.20)
            previous = None
            for _ in range(6):
                current = b.frame()
                m = b.vision.match(current, template_name) if current is not None else None
                if m and previous and max(
                    abs(m.center[0] - previous.center[0]),
                    abs(m.center[1] - previous.center[1]),
                ) <= 3:
                    candidate = (m, current)
                    break
                previous = m
                time.sleep(0.08)
        finally:
            # 即使截图/识别/拖动失败，也不能把触点留在按下状态。
            if not b.actions.up_frame(x, y, fr.shape, label):
                raise RuntimeError("商行定位：触点 UP 失败，停止后续点击")

        time.sleep(0.10)
        if candidate:
            # 若名称还在，用松手后的新坐标；已隐藏则使用停稳时的坐标。
            current = b.frame()
            m = b.vision.match(current, template_name) if current is not None else None
            if m:
                candidate = (m, current)
        return candidate

    def _find_merchant(self):
        """
        在城内寻找商行。

        旧版使用整个白色气泡模板，容易把其它建筑气泡误认成商行。
        优先识别气泡内部独特的“商铺+金元宝”图标；
        最后一轮气泡消失时，拖动并按住地图，识别“商行”建筑名。

        点击后必须确认真正进入行商商店；
        如果点击被吞或候选无效，不会直接结束，而是继续寻找。
        """
        b = self.bot

        initial = b.frame()
        direct = self._merchant_entry_match(initial)
        if direct and self._merchant_try_entry(direct, initial):
            return True
        b.log("[MERCHANT] 当前视野未确认入口，出城/入城复位，再沿养马场相对位置寻找")
        if not self._reset_city_world_for_entry("商行入口复位", cycles=2):
            return False
        if not self._ensure_city():
            return False
        if self._merchant_seek_from_mount():
            return True
        if not self._ensure_city():
            return False

        # 以蛇形路线逐块平移城镇视野。旧路线连续做相反方向的拖动，
        # 视野基本回到原处，商行建筑在屏幕外时会被反复漏掉。
        patterns = self._merchant_scan_patterns()

        for i in range(len(patterns)):
            fr = b.frame()
            if fr is None:
                time.sleep(0.08)
                continue
            m = self._merchant_entry_match(fr)
            if m:
                self._record_merchant_position(m, fr, i + 1, "visible bubble")
                if self._merchant_try_entry(m, fr):
                    return True
                if not self._ensure_city():
                    return False

            # 先轻推当前视野，避免无气泡的商行就在眼前却被大幅拖出屏幕。
            if i == 0:
                candidate = self._merchant_drag_name(
                    b.frame(), (0.55, 0.72, 0.55, 0.69), "商行-显示建筑名"
                )
                if candidate:
                    if self._merchant_try_entry(*candidate):
                        return True
                    if not self._ensure_city():
                        return False

            # 没识别到，或候选点击失败：按蛇形路线移动城内视野继续寻找。
            fr = b.frame()
            p = patterns[i % len(patterns)]
            candidate = self._merchant_drag_name(
                fr, p, f"城内寻找商行-扫描{i + 1}"
            )
            if candidate:
                self._record_merchant_position(candidate[0], candidate[1], i + 1, "drag reveal")
                if self._merchant_try_entry(*candidate):
                    return True
                if not self._ensure_city():
                    return False

        b.log(
            "[STOP] 多次移动城内视野后仍未找到有效商行入口"
        )
        b.fail_snapshot(
            "merchant_not_found"
        )
        return False

    def _merchant_seek_from_mount(self):
        """Use the stable as a camera anchor, never as a blind shop tap."""
        b = self.bot
        # Confirmed together in the saved 726x1600 mount reference on Oct 14:
        # stable bubble (295,702), stable name (291,817), merchant name (187,324).
        # Different accounts can shift/scale buildings: use these offsets only
        # to pan the camera, then re-detect the actual merchant entry.
        for index, pattern in enumerate(((.55,.72,.55,.69), (.72,.76,.72,.34),
                                         (.55,.72,.55,.69))):
            frame = b.frame()
            if frame is None:
                continue
            merchant = self._merchant_entry_match(frame)
            if merchant and self._merchant_try_entry(merchant, frame):
                return True
            if merchant:
                if not self._ensure_city():
                    return False
                frame = b.frame()
                if frame is None:
                    return False
            anchor = self._mount_label_match(frame)
            offset = (-104, -493)
            if anchor is None:
                anchor = self._mount_cached_match(frame)
                if anchor is None:
                    anchor = b.vision.match(frame, "mount_entry_bubble")
                offset = (-108, -378)
            if anchor:
                h, w = frame.shape[:2]
                ex = anchor.center[0] + offset[0]*w/726
                ey = anchor.center[1] + offset[1]*h/1600
                dx = max(-.25*w, min(.25*w, .42*w-ex))
                dy = max(-.28*h, min(.28*h, .44*h-ey))
                b.log(f"[MERCHANT ANCHOR] 养马场={anchor.center}；"
                      f"商行预计方向=左上，平移=({dx:.0f},{dy:.0f})；点击仍需入口确认")
                pan = (.50, .43, .50+dx/w, .43+dy/h)
                candidate = self._merchant_drag_name(frame, pan, "养马场左上-寻找商行")
            else:
                candidate = self._merchant_drag_name(frame, pattern,
                                                     f"商行-返回养马场视野#{index+1}")
            if candidate:
                self._record_merchant_position(candidate[0], candidate[1], index+1,
                                               "mount-relative confirmed merchant")
                if self._merchant_try_entry(*candidate):
                    return True
                if not self._ensure_city():
                    return False
            # Next iteration uses a fresh frame after the camera has moved.
        return False

    def _open_mount_visit(self):
        b = self.bot
        if not self._ensure_city():
            return False
        # Use the stable's unique map label first. The horse stable is below-left
        # of the arena and left of the mile marker, so broad repeated sweeps are
        # unnecessary once this label is visible.
        patterns = [
            (.55, .72, .55, .69), (.72, .76, .72, .34),
            (.76, .50, .28, .50), (.28, .34, .28, .76),
            (.28, .76, .72, .76), (.72, .34, .28, .34),
            (.72, .72, .72, .48), (.28, .52, .72, .52),
        ]
        for i in range(len(patterns)):
            fr = b.frame()
            if fr is None:
                continue
            label = self._mount_label_match(fr)
            if label:
                m = self._mount_entry_from_label(fr)
                alignment = None
                if m is None:
                    if self._mount_confirm_completed(fr, None):
                        return "complete"
                    # The stable is in view. Re-sample it rather than clicking
                    # an unrelated horse-like bubble elsewhere on the map.
                    time.sleep(.25)
                    continue
            else:
                m, alignment = self._mount_cached_match(fr, return_alignment=True)
                if m is None:
                    m = b.vision.match(fr, "mount_entry_bubble")
                if m is None:
                    # The supplied city view places the stable bubble about
                    # (+108,+378) pixels from the merchant name at 447x981.
                    # Recheck the bubble itself before tapping; if offscreen,
                    # pan toward that location and take a new frame.
                    merchant = b.vision.match(fr, "merchant_name")
                    if merchant:
                        h, w = fr.shape[:2]
                        target = (round(merchant.center[0] + 108*w/447),
                                  round(merchant.center[1] + 378*h/981))
                        b.log(f"[MOUNT] 商行名称={merchant.center}，推算马头像={target}")
                        if 30 < target[0] < w-30 and 30 < target[1] < h-80:
                            m = self._mount_match_near(fr, target,
                                                       radius=80, threshold=.72)
                        else:
                            dx = max(-.32, min(.32, .5-target[0]/w))
                            dy = max(-.32, min(.32, .5-target[1]/h))
                            if abs(dx) > .08 or abs(dy) > .08:
                                guided = self._merchant_drag_name(
                                    fr, (.5, .5, .5+dx, .5+dy),
                                    "养马场-按商行相对位置移动",
                                    template_name="mount_entry_bubble")
                                if guided:
                                    m, fr = guided
                                else:
                                    fr = b.frame()
                                    if fr is None:
                                        continue
                if m is None and self._mount_confirm_completed(fr, alignment):
                    return "complete"
            if not m:
                p = patterns[i]
                candidate = self._merchant_drag_name(
                    fr, p, f"养马场-显示入口#{i+1}",
                    template_name="mount_entry_bubble",
                )
                if candidate:
                    m, fr = candidate
            if m:
                if not b.actions.tap_match(m, fr.shape, "养马场-马头像"):
                    return False
                if b.wait_template("page_mount_visit", 4):
                    self._record_mount_entry_position(m, fr, i + 1)
                    return True
                if not self._ensure_city():
                    return False
        b.log("[MOUNT] 未定位到马匹入口，也无法确认养马场图标已消失；状态未知")
        b.fail_snapshot("mount_entry_not_found")
        return False

    def _mount_entry_from_label(self, frame):
        """Locate the horse bubble relative to the unique ‘养马场’ nameplate."""
        if frame is None:
            return None
        label = self._mount_label_match(frame)
        if not label:
            return None
        h, w = frame.shape[:2]
        # In the supplied city screenshots the bubble is centered about 90
        # canonical pixels above the building nameplate.
        expected = (label.center[0], label.center[1] - round(h * 90 / 1600))
        bubble = self._mount_match_near(
            frame, expected, radius=65, threshold=.72
        )
        if bubble:
            self.bot.log(
                f"[MOUNT] 由养马场建筑名定位并确认马头像 score={bubble.score:.3f} "
                f"frame={bubble.center}"
            )
        return bubble

    def _mount_label_match(self, frame):
        """Try nameplate appearances captured at two camera positions."""
        matches = [self.bot.vision.match(frame, name) for name in
                   ("mount_name", "mount_name_2")]
        matches = [match for match in matches if match]
        return max(matches, key=lambda match: match.score) if matches else None

    def _mount_match_near(self, frame, center, radius=90, threshold=.86):
        """Confirm a cached or map-aligned guess with the horse bubble itself."""
        if frame is None:
            return None
        h, w = frame.shape[:2]
        x, y = map(int, center)
        radius = round(radius * w / 726)
        x1, y1 = max(0, x-radius), max(0, y-radius)
        x2, y2 = min(w, x+radius), min(h, y+radius)
        roi = frame[y1:y2, x1:x2]
        base = self.bot.vision.templates["mount_entry_bubble"]
        best = None
        for factor in (.96, .98, 1.0, 1.02, 1.04):
            tw = round(base.shape[1] * w / 726 * factor)
            th = round(base.shape[0] * h / 1600 * factor)
            if tw >= roi.shape[1] or th >= roi.shape[0]:
                continue
            template = cv2.resize(base, (tw, th), interpolation=cv2.INTER_AREA)
            _, score, _, loc = cv2.minMaxLoc(
                cv2.matchTemplate(roi, template, cv2.TM_CCOEFF_NORMED)
            )
            if best is None or score > best[0]:
                best = (score, x1+loc[0], y1+loc[1], tw, th)
        if best is None or best[0] < threshold:
            return None
        score, bx, by, tw, th = best
        return Match("mount_entry_bubble", float(score),
                     (bx, by, bx+tw, by+th), (bx+tw//2, by+th//2))

    def _mount_cached_match(self, frame, return_alignment=False):
        """Reuse the last confirmed bubble and align city buildings if panned."""
        def result(found, alignment=None):
            return (found, alignment) if return_alignment else found

        path = getattr(self, "mount_entry_log_path",
                       os.path.join("runtime", "debug", "mount_entry_coords.jsonl"))
        try:
            with open(path, "r", encoding="utf-8") as stream:
                records = [json.loads(line) for line in stream if line.strip()]
        except (OSError, json.JSONDecodeError):
            return result(None)
        if not records:
            return result(None)
        latest = records[-1]
        norm = latest.get("normalized")
        if not isinstance(norm, list) or len(norm) != 2:
            return result(None)
        h, w = frame.shape[:2]
        old_center = (round(norm[0]*w), round(norm[1]*h))
        found = self._mount_match_near(frame, old_center)
        if found is not None:
            self.bot.log(f"[MOUNT] 上次记录位置附近确认马匹入口 {found.center}")
            return result(found)

        reference_path = latest.get("reference")
        if not reference_path:
            return result(None)
        reference = cv2.imread(str(reference_path))
        if reference is None:
            return result(None)
        reference = cv2.resize(reference, (w, h))
        mask = np.zeros((h, w), np.uint8)
        mask[round(h*.16):round(h*.75), round(w*.09):round(w*.85)] = 255
        orb = cv2.ORB_create(nfeatures=1800)
        points_old, desc_old = orb.detectAndCompute(
            cv2.cvtColor(reference, cv2.COLOR_BGR2GRAY), mask
        )
        points_new, desc_new = orb.detectAndCompute(
            cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), mask
        )
        if desc_old is None or desc_new is None:
            return result(None)
        pairs = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(
            desc_old, desc_new, k=2
        )
        good = [a for pair in pairs if len(pair) == 2
                for a, second in [pair] if a.distance < .72*second.distance]
        if len(good) < 18:
            return result(None)
        source = np.float32([points_old[m.queryIdx].pt for m in good])
        target = np.float32([points_new[m.trainIdx].pt for m in good])
        affine, inliers = cv2.estimateAffinePartial2D(
            source, target, method=cv2.RANSAC,
            ransacReprojThreshold=5, maxIters=2000
        )
        if affine is None or inliers is None:
            return result(None)
        count = int(inliers.sum())
        scale = math.hypot(affine[0, 0], affine[1, 0])
        if count < 14 or count/len(good) < .35 or not .9 <= scale <= 1.1:
            return result(None)
        predicted = affine @ np.array([*old_center, 1.0])
        px, py = round(predicted[0]), round(predicted[1])
        if not (20 <= px < w-20 and 20 <= py < h-20):
            return result(None)
        found = self._mount_match_near(frame, (px, py), radius=100)
        if found is not None:
            self.bot.log(f"[MOUNT MAP] 建筑画面对齐{count}个特征，"
                         f"预测({px},{py})，确认马匹入口{found.center}")
        return result(found, ((px, py), count))

    def _mount_confirm_completed(self, first_frame, first_alignment):
        """Confirm a vanished icon from a stable view of the horse stable."""
        b = self.bot
        city = b.vision.match(first_frame, "city_exit_button")
        label = self._mount_label_match(first_frame)
        # A visible, unique stable nameplate is stronger evidence than the
        # saved map transform and remains usable after city pans.
        if label and city:
            h, w = first_frame.shape[:2]
            expected = (label.center[0], label.center[1] - round(h * 90 / 1600))
            observations = [label.center]
            last_frame = first_frame
            for _ in range(2):
                time.sleep(.3)
                last_frame = b.frame()
                if (last_frame is None
                        or not b.vision.match(last_frame, "city_exit_button")):
                    return False
                current_label = self._mount_label_match(last_frame)
                if not current_label:
                    return False
                if math.dist(label.center, current_label.center) > 24 * w / 726:
                    return False
                current_expected = (
                    current_label.center[0],
                    current_label.center[1] - round(last_frame.shape[0] * 90 / 1600),
                )
                if self._mount_match_near(
                        last_frame, current_expected, radius=65, threshold=.72):
                    return False
                observations.append(current_label.center)
            stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            evidence = b.debug_dir / f"{stamp}_mount_icon_absent.png"
            cv2.imwrite(str(evidence), last_frame)
            b.log(
                "[MOUNT] 养马场名称连续3帧稳定，马头像均不存在；"
                f"判定本轮已完成，证据：{evidence}"
            )
            return True
        if first_alignment is None or not city:
            return False
        h, w = first_frame.shape[:2]
        center, count = first_alignment
        # The horse bubble and the building name must both fit on screen.
        if not (round(w * .22) < center[0] < round(w * .78)
                and round(h * .23) < center[1] < round(h * .68)):
            return False
        if self._mount_match_near(first_frame, center, radius=110, threshold=.75):
            return False
        observations = [(center, count)]
        last_frame = first_frame
        for _ in range(2):
            time.sleep(.35)
            last_frame = b.frame()
            if last_frame is None or not b.vision.match(last_frame, "city_exit_button"):
                return False
            found, alignment = self._mount_cached_match(last_frame, return_alignment=True)
            if found or b.vision.match(last_frame, "mount_entry_bubble") or alignment is None:
                return False
            new_center, feature_count = alignment
            if math.dist(center, new_center) > 25 * w / 726:
                return False
            if self._mount_match_near(last_frame, new_center, radius=110, threshold=.75):
                return False
            observations.append((new_center, feature_count))
        # The label under the icon distinguishes the stable from nearby buildings.
        x, y = observations[-1][0]
        roi = (max(0, x - round(w * .16)), y + round(h * .065),
               min(w, x + round(w * .16)), min(h, y + round(h * .16)))
        canonical_roi = tuple(round(v * scale) for v, scale in zip(
            roi, (726 / w, 1600 / h, 726 / w, 1600 / h)))
        try:
            from local_ocr import read_text
            label = read_text(last_frame, canonical_roi, scale=3, context=True)["text"]
        except (OSError, RuntimeError, ValueError, KeyError) as exc:
            b.log(f"[MOUNT] 养马场文字确认失败：{exc}")
            return False
        if "养马场" not in "".join(label.split()):
            b.log(f"[MOUNT] 建筑已对齐但未识别到养马场文字：{label!r}")
            return False
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        evidence = b.debug_dir / f"{stamp}_mount_icon_absent.png"
        cv2.imwrite(str(evidence), last_frame)
        b.log(f"[MOUNT] 养马场文字已确认，3帧建筑对齐且马头像消失；"
              f"本轮互动已完成或入口已关闭，证据：{evidence}")
        return True

    def _record_mount_entry_position(self, match, frame, scan_number):
        """Persist horse-bubble coordinates so the next scan can be compared."""
        height, width = frame.shape[:2]
        x, y = match.center
        record = {
            "time": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "scan": int(scan_number),
            "score": round(float(match.score), 4),
            "frame_size": [int(width), int(height)],
            "center": [int(x), int(y)],
            "normalized": [round(x / width, 5), round(y / height, 5)],
        }
        path = getattr(
            self,
            "mount_entry_log_path",
            os.path.join("runtime", "debug", "mount_entry_coords.jsonl"),
        )
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Keep each bubble coordinate paired with the exact map frame it came
        # from. A shared filename let later scans overwrite the reference while
        # old records still pointed at it, shifting the predicted stable.
        reference_path = os.path.abspath(os.path.join(
            os.path.dirname(path), f"mount_entry_reference_{record['time'].replace(':', '').replace('+', '_')}_{scan_number}.png"
        ))
        if cv2.imwrite(reference_path, frame):
            record["reference"] = reference_path
        previous = None
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        previous = json.loads(line)
        except (OSError, json.JSONDecodeError):
            previous = None
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        if previous and previous.get("normalized"):
            dx = record["normalized"][0] - previous["normalized"][0]
            dy = record["normalized"][1] - previous["normalized"][1]
            self.bot.log(
                f"[MOUNT POS] scan={scan_number} score={match.score:.3f} "
                f"frame=({x},{y}) previous=({previous['center'][0]}"
                f",{previous['center'][1]}) normalized_delta=({dx:+.4f},{dy:+.4f})"
            )
        else:
            self.bot.log(
                f"[MOUNT POS] 首次记录坐骑气泡坐标 frame=({x},{y}) "
                f"score={match.score:.3f} file={path}"
            )

    def mount_visit(self):
        """发起每日坐骑邀约；等待盟友时退出，下一次任务回访再检查。"""
        b = self.bot
        b.log("\n[FLOW] 养马场：检查每日次数及结伴同游状态")
        opened = self._open_mount_visit()
        if opened == "complete":
            return True
        if not opened:
            return False
        deadline = time.time() + 12
        while time.time() < deadline:
            fr = b.frame()
            if fr is None:
                time.sleep(.2)
                continue
            cont = b.vision.match(fr, "reward_continue")
            if cont:
                if not b.actions.tap_match(cont, fr.shape, "坐骑互动奖励-继续"):
                    return False
                time.sleep(.55)
                continue
            if not b.vision.match(fr, "page_mount_visit"):
                time.sleep(.2)
                continue
            zero = b.vision.match(fr, "mount_reward_zero")
            if zero:
                # The joined-horses page has no invite button when today's
                # invitation rewards are exhausted. Confirm on a new frame;
                # a single match must not be treated as an invitation sent.
                time.sleep(.25)
                check = b.frame()
                if (check is not None
                        and b.vision.match(check, "page_mount_visit")
                        and b.vision.match(check, "mount_reward_zero")):
                    b.log(f"[MOUNT] 结伴同游页连续两帧确认今日奖励次数为0 "
                          f"score={zero.score:.3f}；本轮无可发起的奖励邀约")
                    b.task_skip_reason = "结伴同游页确认今日邀约奖励次数为0"
                    break
            if b.vision.match(fr, "mount_waiting"):
                b.log("[MOUNT] 正在等待盟友加入，退出，后续再检查")
                b.task_waiting_reason = "养马场邀约已发出，等待盟友加入"
                break
            invite = b.vision.match(fr, "mount_invite_button")
            if invite:
                # Reward count can be 1, 2, or more, and it stays unchanged
                # while an invitation waits for an ally. The active button,
                # followed by the waiting state, is the reliable transition.
                if not b.actions.tap_match(invite, fr.shape, "结伴同游-发起邀约"):
                    return False
                if not b.wait_template("mount_waiting", 5):
                    b.fail_snapshot("mount_invite_not_confirmed")
                    return False
                b.log("[OK] 已发起坐骑邀约，并确认等待盟友加入")
                b.task_waiting_reason = "已确认发起养马场邀约，等待盟友加入"
                break
            time.sleep(.3)
        else:
            b.fail_snapshot("mount_visit_unknown_state")
            return False
        if not self._tap_game_back("结伴同游-返回主界面"):
            return False
        return b.wait_main(5)

    def _patch_template_score(self, fr, patch, tmpl_name):
        b=self.bot
        t=b.vision.templates[tmpl_name]
        # canonical模板按当前frame比例缩放；patch已经是当前frame坐标。
        scale=fr.shape[1]/726.0
        tw=max(4,round(t.shape[1]*scale)); th=max(4,round(t.shape[0]*scale))
        tt=cv2.resize(t,(tw,th),interpolation=cv2.INTER_AREA)
        if patch.shape[0]<=th or patch.shape[1]<=tw: return -1.0
        return float(cv2.minMaxLoc(cv2.matchTemplate(patch,tt,cv2.TM_CCOEFF_NORMED))[1])

    def _merchant_card_patch(self, fr, idx):
        """
        返回指定商品卡的价格区域 patch 及购买点击坐标。
        idx: 1..12
        """
        fh, fw = fr.shape[:2]
        xs = [12, 121, 229, 337]
        ys = [331, 494, 655]

        r = (idx - 1) // 4
        c = (idx - 1) % 4
        x = xs[c]
        y = ys[r]

        x1 = int(fw * x / 447)
        x2 = int(fw * (x + 102) / 447)
        y1 = int(fh * (y + 96) / 1001)
        y2 = int(fh * (y + 132) / 1001)

        patch = fr[y1:y2, x1:x2]

        # 真正购买热区：卡片底部价格行。
        cx = int(fw * (x + 51) / 447)
        cy = int(fh * (y + 112) / 1001)

        return patch, cx, cy

    def _merchant_card_state(self, fr, idx):
        """
        精确判断商品卡状态：

        material = 普通材料兑换，可购买
        gold     = 元宝价格，绝对禁止点击
        sold     = 已售完
        forbidden = 命中用户指定的绿色循环箭头图标，绝对禁止兑换
        unknown  = 暂时不能可靠判断
        """
        patch, cx, cy = self._merchant_card_patch(fr, idx)

        # 用户标注的绿色循环箭头商品禁止兑换。检查每张卡片上半部的
        # 商品图标区域，不依赖卡片序号，因为商店刷新后商品位置会变化。
        fh, fw = fr.shape[:2]
        xs = [12, 121, 229, 337]
        ys = [331, 494, 655]
        row = (idx - 1) // 4
        col = (idx - 1) % 4
        scale_x = fw / 447.0
        scale_y = fh / 1001.0
        x, y = xs[col], ys[row]
        ix1 = max(0, int(round((x + 15) * scale_x)))
        ix2 = min(fw, int(round((x + 87) * scale_x)))
        iy1 = max(0, int(round((y + 18) * scale_y)))
        iy2 = min(fh, int(round((y + 76) * scale_y)))
        icon = fr[iy1:iy2, ix1:ix2]
        if icon.size:
            icon_hsv = cv2.cvtColor(icon, cv2.COLOR_BGR2HSV)
            green_mask = cv2.inRange(
                icon_hsv,
                np.array([35, 65, 65]),
                np.array([100, 255, 255]),
            )
            forbidden_icon_score = float(
                np.count_nonzero(green_mask) / green_mask.size
            )
        else:
            forbidden_icon_score = 0.0

        if forbidden_icon_score >= 0.08:
            return {
                "idx": idx,
                "state": "forbidden",
                "material_name": None,
                "material_score": 0.0,
                "gold_score": 0.0,
                "sold_score": 0.0,
                "forbidden_icon_score": forbidden_icon_score,
                "x": cx,
                "y": cy,
            }

        material_names = [
            "merchant_price_meat",
            "merchant_price_wood",
            "merchant_price_coal",
            "merchant_price_iron",
        ]

        material_scores = {
            name: self._patch_template_score(fr, patch, name)
            for name in material_names
        }

        material_name = max(material_scores, key=material_scores.get)
        material_score = material_scores[material_name]

        gold_score = self._patch_template_score(
            fr, patch, "merchant_price_gold"
        )
        sold_score = self._patch_template_score(
            fr, patch, "merchant_soldout"
        )

        # 已售完优先。
        if sold_score >= 0.82:
            state = "sold"

        # 元宝：真实样本通常gold >0.95，
        # 且会明显高于材料模板的假相似度。
        elif (
            gold_score >= 0.88
            and gold_score >= material_score + 0.08
        ):
            state = "gold"

        # 材料：真实样本最低约0.80，
        # 并且必须明显胜过元宝模板。
        elif (
            material_score >= 0.80
            and material_score >= gold_score + 0.10
            and sold_score < 0.78
        ):
            state = "material"

        else:
            state = "unknown"

        return {
            "idx": idx,
            "state": state,
            "material_name": material_name,
            "material_score": float(material_score),
            "gold_score": float(gold_score),
            "sold_score": float(sold_score),
            "forbidden_icon_score": forbidden_icon_score,
            "x": cx,
            "y": cy,
        }

    def _merchant_scan_cards_once(self, fr=None):
        b = self.bot
        fr = b.frame() if fr is None else fr
        return {
            idx: self._merchant_card_state(fr, idx)
            for idx in range(1, 13)
        }

    def _merchant_scan_cards_consensus(self, samples=3, interval=0.14):
        """
        连续多帧扫描12张商品卡。

        同一卡必须在全部样本中状态一致，
        否则标记unknown。
        """
        b = self.bot
        scans = []

        for i in range(samples):
            scans.append(self._merchant_scan_cards_once())
            if i + 1 < samples:
                time.sleep(interval)

        result = {}

        for idx in range(1, 13):
            states = [s[idx]["state"] for s in scans]
            stable = states[0] if len(set(states)) == 1 else "unknown"

            item = dict(scans[-1][idx])
            item["state"] = stable
            item["samples"] = states
            result[idx] = item

        return result

    def _merchant_log_scan(self, scan, prefix="[SHOP SCAN]"):
        b = self.bot

        groups = {
            "material": [],
            "gold": [],
            "sold": [],
            "forbidden": [],
            "unknown": [],
        }

        for idx, item in scan.items():
            groups[item["state"]].append(idx)

        b.log(
            f"{prefix} "
            f"材料={groups['material']} "
            f"元宝={groups['gold']} "
            f"禁兑图标={groups['forbidden']} "
            f"已售完={groups['sold']} "
            f"未知={groups['unknown']}"
        )

        return groups

    def _merchant_confirm_card_sold(self, idx, timeout=2.0):
        """
        购买后必须明确看到“已售完”。
        连续2次识别为sold才算成功。
        """
        b = self.bot
        deadline = time.time() + timeout
        consecutive = 0

        while time.time() < deadline:
            fr = b.frame()
            if b.vision.match(fr, "page_stamina_more"):
                return "need_fill"
            if b.vision.match(fr, "reward_continue"):
                return "reward"

            item = self._merchant_card_state(fr, idx)

            if item["state"] == "sold":
                consecutive += 1
                if consecutive >= 2:
                    b.log(
                        f"[OK] card#{idx} 连续确认“已售完”"
                    )
                    return True
            else:
                consecutive = 0

            time.sleep(0.14)

        return False

    def _merchant_fill_shortage_popup(self):
        """Use the user's requested blue '一键补齐所有' action."""
        b = self.bot
        fr = b.frame()
        fill = b.vision.match(fr, "merchant_fill_all")
        if not fill:
            b.log("[SHOP] 获取更多弹窗中未识别到一键补齐所有按钮")
            b.fail_snapshot("merchant_fill_all_button_missing")
            return False

        b.log(f"[SHOP] 点击一键补齐所有 score={fill.score:.3f}")
        if not b.actions.tap_match(
            fill,
            fr.shape,
            "商行资源不足-一键补齐所有",
        ):
            return False

        if not b.wait_template_gone("page_stamina_more", 6):
            b.log("[SHOP] 点击一键补齐所有后获取更多弹窗仍未关闭")
            b.fail_snapshot("merchant_fill_all_popup_stuck")
            return False

        b.log("[OK] 一键补齐所有已执行，获取更多弹窗已关闭")
        return True

    def _merchant_free_refresh_target(self, fr):
        """
        判断右上角是否仍存在绿色“免费刷新”。

        双重判断：
        1) 先做模板匹配；
        2) 模板没命中时，对固定按钮区域做HSV绿色占比检测。

        返回：
            ("template", match)  模板命中
            ("green", (x,y))     绿色按钮命中
            (None, None)         确认无免费刷新
        """
        b = self.bot

        m = b.vision.match(
            fr,
            "merchant_free_refresh",
        )
        if m:
            b.log(
                f"[MERCHANT] 免费刷新模板命中 "
                f"score={m.score:.3f}"
            )
            return "template", m

        h, w = fr.shape[:2]

        # 对应447x1001样本中大约：
        # x=345..435, y=184..232
        x1 = int(round(w * 345 / 447))
        x2 = int(round(w * 435 / 447))
        y1 = int(round(h * 184 / 1001))
        y2 = int(round(h * 232 / 1001))

        roi = fr[y1:y2, x1:x2]
        if roi.size == 0:
            return None, None

        hsv = cv2.cvtColor(
            roi,
            cv2.COLOR_BGR2HSV,
        )

        # 商行“免费刷新”按钮是高饱和绿色。
        mask = cv2.inRange(
            hsv,
            np.array([35, 90, 70]),
            np.array([95, 255, 255]),
        )

        green_ratio = float(
            np.count_nonzero(mask)
        ) / float(mask.size)

        b.log(
            f"[MERCHANT] 免费刷新绿色检测 ratio={green_ratio:.3f}"
        )

        # 按钮占这块ROI的较大面积；不存在时该区域基本是棕/黑背景。
        if green_ratio >= 0.18:
            cx = (x1 + x2) // 2
            cy = (y1 + y2) // 2
            return "green", (cx, cy)

        return None, None

    def merchant(self):
        b = self.bot
        b.log(
            "\n[FLOW] 行商商店材料兑换 + 免费刷新（精准多帧版）"
        )

        if not self._ensure_city():
            return False

        if not self._find_merchant():
            return False

        refresh_round = 0

        while refresh_round < 12:
            # ========================================================
            # A. 当前页先把所有普通材料商品买完
            # ========================================================
            buy_round = 0

            while buy_round < 20:
                scan = self._merchant_scan_cards_consensus(
                    samples=3,
                    interval=0.14,
                )
                groups = self._merchant_log_scan(
                    scan,
                    "[SHOP PREBUY]",
                )

                # 状态不稳定：再做一整轮3帧复核。
                if groups["unknown"]:
                    b.log(
                        "[SHOP] 存在未知商品，等待并重新做3帧识别"
                    )
                    time.sleep(0.45)

                    scan = self._merchant_scan_cards_consensus(
                        samples=3,
                        interval=0.16,
                    )
                    groups = self._merchant_log_scan(
                        scan,
                        "[SHOP RECHECK]",
                    )

                    if groups["unknown"]:
                        b.log(
                            "[STOP] 两轮多帧比较后仍有未知商品；"
                            "为防误点元宝，停止并截图"
                        )
                        b.fail_snapshot(
                            "merchant_card_state_unknown"
                        )
                        return False

                if not groups["material"]:
                    b.log(
                        "[SHOP] 多帧确认：当前页已经没有普通材料商品"
                    )
                    break

                # 一次只买一个，并重新扫描后再买下一个。
                idx = groups["material"][0]
                item = scan[idx]

                b.log(
                    f"[SHOP] 候选 card#{idx} "
                    f"material={item['material_score']:.3f} "
                    f"gold={item['gold_score']:.3f} "
                    f"sold={item['sold_score']:.3f} "
                    f"samples={item['samples']}"
                )

                # 点击前再连续3次单卡确认。
                final_states = []

                for confirm_i in range(3):
                    ci = self._merchant_card_state(
                        b.frame(),
                        idx,
                    )
                    final_states.append(ci["state"])
                    if confirm_i < 2:
                        time.sleep(0.13)

                if final_states != ["material", "material", "material"]:
                    b.log(
                        f"[STOP] card#{idx} 点击前3次状态="
                        f"{final_states}，不是全material；禁止点击"
                    )
                    b.fail_snapshot(
                        f"merchant_card_{idx}_preclick_unstable"
                    )
                    return False

                purchase_state = False
                for purchase_attempt in range(2):
                    fr = b.frame()
                    _, cx, cy = self._merchant_card_patch(fr, idx)
                    b.log(
                        f"[SHOP] card#{idx} 点击价格区域"
                        f"尝试#{purchase_attempt + 1} frame=({cx},{cy})"
                    )
                    b.actions.tap_frame(
                        cx,
                        cy,
                        fr.shape,
                        f"材料兑换商品#{idx}-价格区域#{purchase_attempt + 1}",
                    )
                    purchase_state = self._merchant_confirm_card_sold(
                        idx,
                        timeout=2.2,
                    )

                    if purchase_state == "reward":
                        cont = b.vision.match(b.frame(), "reward_continue")
                        if cont:
                            b.actions.tap_match(
                                cont,
                                b.frame().shape,
                                "商行兑换奖励-点击继续",
                            )
                            b.wait_template_gone("reward_continue", 3)
                        purchase_state = self._merchant_confirm_card_sold(
                            idx,
                            timeout=1.0,
                        )

                    if purchase_state is True:
                        break

                    if purchase_state == "need_fill" and purchase_attempt == 0:
                        if not self._merchant_fill_shortage_popup():
                            return False
                        # 补齐后商品可能已自动成交；否则只在重新确认商品
                        # 仍是材料价后重试一次。补资源动画和页面刷新有
                        # 延迟，给状态检测更长的稳定窗口，避免误判后退出。
                        purchase_state = self._merchant_confirm_card_sold(
                            idx,
                            timeout=2.0,
                        )
                        if purchase_state == "reward":
                            cont = b.vision.match(
                                b.frame(),
                                "reward_continue",
                            )
                            if cont:
                                fr_reward = b.frame()
                                b.actions.tap_match(
                                    cont,
                                    fr_reward.shape,
                                    "商行补齐资源奖励-点击继续",
                                )
                                b.wait_template_gone(
                                    "reward_continue",
                                    3,
                                )
                            purchase_state = self._merchant_confirm_card_sold(
                                idx,
                                timeout=1.5,
                            )
                        if purchase_state is True:
                            break
                        stable_material = 0
                        retry_deadline = time.time() + 4.0
                        while time.time() < retry_deadline:
                            fr_retry = b.frame()
                            if b.vision.match(fr_retry, "page_stamina_more"):
                                stable_material = 0
                                time.sleep(0.25)
                                continue
                            state = self._merchant_card_state(fr_retry, idx)["state"]
                            if state == "sold":
                                purchase_state = self._merchant_confirm_card_sold(
                                    idx,
                                    timeout=1.0,
                                )
                                if purchase_state is True:
                                    break
                            if state == "material":
                                stable_material += 1
                                if stable_material >= 3:
                                    b.log(
                                        f"[SHOP] card#{idx} 补齐后仍稳定为材料价，重试兑换"
                                    )
                                    break
                            else:
                                stable_material = 0
                            time.sleep(0.25)
                        if purchase_state is True:
                            break
                        if stable_material >= 3:
                            continue
                    break

                if purchase_state is not True:
                    after = self._merchant_card_state(
                        b.frame(),
                        idx,
                    )
                    b.log(
                        f"[STOP] card#{idx} 购买后未稳定变成已售完："
                        f"state={after['state']} "
                        f"material={after['material_score']:.3f} "
                        f"gold={after['gold_score']:.3f} "
                        f"sold={after['sold_score']:.3f}"
                    )
                    b.fail_snapshot(
                        f"merchant_card_{idx}_not_confirmed_sold"
                    )
                    return False

                buy_round += 1
                time.sleep(0.20)

            else:
                b.log(
                    "[STOP] 当前页购买次数超过20，停止"
                )
                b.fail_snapshot(
                    "merchant_buy_loop_overflow"
                )
                return False

            # ========================================================
            # B. 免费刷新前：要求连续3轮门禁全部通过
            # ========================================================
            consecutive_gate = 0

            for gate_round in range(1, 4):
                scan = self._merchant_scan_cards_consensus(
                    samples=3,
                    interval=0.16,
                )
                groups = self._merchant_log_scan(
                    scan,
                    f"[SHOP GATE#{gate_round}]",
                )

                classified_count = (
                    len(groups["gold"])
                    + len(groups["sold"])
                    + len(groups["forbidden"])
                )

                passed = (
                    not groups["material"]
                    and not groups["unknown"]
                    and classified_count == 12
                )

                if passed:
                    consecutive_gate += 1
                    b.log(
                        f"[SHOP] 门禁#{gate_round}通过："
                        "12张全部是元宝/已售完/禁兑商品"
                    )
                else:
                    consecutive_gate = 0
                    b.log(
                        f"[SHOP] 门禁#{gate_round}未通过，"
                        "禁止刷新"
                    )
                    break

                if gate_round < 3:
                    time.sleep(0.30)

            if consecutive_gate != 3:
                b.log(
                    "[STOP] 免费刷新前没有连续3轮确认"
                    "“材料=0、未知=0、其余均已分类（元宝/已售完/禁兑）”；禁止刷新"
                )
                b.fail_snapshot(
                    "merchant_refresh_gate_failed"
                )
                return False

            b.log(
                "[SHOP] 连续3轮精准门禁通过，"
                "现在才允许检查/点击免费刷新"
            )

            # ========================================================
            # C. 当前页材料都买完后，才处理免费刷新
            # ========================================================
            fr = b.frame()
            refresh_kind, refresh_target = (
                self._merchant_free_refresh_target(fr)
            )

            if refresh_kind is None:
                # 再复核一次，避免瞬时漏识别。
                time.sleep(0.35)
                fr = b.frame()
                refresh_kind, refresh_target = (
                    self._merchant_free_refresh_target(fr)
                )

            if refresh_kind is None:
                b.log(
                    "[OK] 两次确认右上角已无“免费刷新”，"
                    "行商任务结束"
                )
                break

            if refresh_kind == "template":
                b.actions.tap_match(
                    refresh_target,
                    fr.shape,
                    "行商-免费刷新",
                )
            else:
                cx, cy = refresh_target
                b.log(
                    f"[MERCHANT] 绿色检测确认免费刷新仍存在，"
                    f"点击 frame=({cx},{cy})"
                )
                b.actions.tap_frame(
                    cx,
                    cy,
                    fr.shape,
                    "行商-免费刷新(绿色检测)",
                )

            refresh_round += 1
            b.log(
                f"[SHOP] 已执行免费刷新 #{refresh_round}"
            )

            # 等新商品页稳定。
            time.sleep(1.00)

        # ============================================================
        # D. 返回主界面
        # ============================================================
        self._tap_game_back("商行-返回主页面")

        if not b.wait_main(5):
            if not b.ensure_main():
                return False

        b.log("[OK] 行商任务完成")
        return True
