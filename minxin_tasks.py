"""民心政令技能：按当前状态激活，确认生效后退出。"""
import re
import time

import cv2

from local_ocr import read_text


class MinxinTasks:
    @staticmethod
    def _minxin_red_heart_visible(frame):
        if frame is None:
            return False
        h, w = frame.shape[:2]
        x1, x2 = round(w*.42), round(w*.58)
        y1, y2 = round(h*.025), round(h*.145)
        patch = frame[y1:y2, x1:x2]
        if patch.size == 0:
            return False
        hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
        red = (cv2.inRange(hsv, (0, 90, 80), (12, 255, 255)) |
               cv2.inRange(hsv, (168, 90, 80), (180, 255, 255)))
        return cv2.countNonZero(red) / red.size >= .055

    def _minxin_is_page(self, frame):
        return frame is not None and bool(self.bot.vision.match(frame, "page_minxin"))

    def _minxin_is_skill(self, frame):
        return self._minxin_is_page(frame) and bool(
            self.bot.vision.match(frame, "minxin_skill_cost"))

    def _minxin_tap(self, frame, x, y, label):
        return self.bot.actions.tap_frame(round(x * frame.shape[1] / 726),
                                          round(y * frame.shape[0] / 1600),
                                          frame.shape, label)

    @staticmethod
    def _minxin_color_ratio(frame, box, green=False):
        h, w = frame.shape[:2]
        x1, y1, x2, y2 = [round(v * s) for v, s in
                          zip(box, (w/726, h/1600, w/726, h/1600))]
        patch = frame[y1:y2, x1:x2]
        if not patch.size:
            return 0
        hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
        low, high = ((35, 100, 100), (90, 255, 255)) if green else (
            (8, 65, 180), (40, 255, 255))
        return cv2.countNonZero(cv2.inRange(hsv, low, high)) / (patch.shape[0] * patch.shape[1])

    def _minxin_card_state(self, frame, box):
        text = self._minxin_status_text(frame, box)
        if "生效" in text:
            return "active", text
        if "冷却" in text or re.search(r"\d+[：:]\d{2}", text):
            return "timer", text
        if any(word in text for word in ("睡眠时段", "未解锁", "不可用")):
            return "unavailable", text
        # The known 半夜鸡叫 card is visually desaturated while its
        # "睡眠时段可用" label is easy for OCR to miss. Its lit state has a
        # strong gold highlight; a dim card with no active timer is unavailable.
        if self._minxin_color_ratio(frame, box) < .08:
            return "unavailable", text or "卡片灰暗，无激活高亮"
        if self._minxin_color_ratio(frame, box) > .30:
            return "ready", text
        return "unknown", text

    def _minxin_status_text(self, frame, box):
        texts = []
        for scale, mode in ((2, None), (3, "white"), (3, "green")):
            text = "".join(read_text(frame, box, scale=scale, mode=mode,
                                     context=True).get("text", "").split())
            texts.append(text)
            if any(word in text for word in ("生效", "冷却", "睡眠时段", "不可用")):
                return text
        return " | ".join(texts)

    def _minxin_detail_status(self, frame):
        text = self._minxin_status_text(frame, (190, 990, 545, 1090))
        return ("active" if "生效" in text else
                "timer" if "冷却" in text else "unknown"), text

    def _minxin_exit(self):
        b = self.bot
        for _ in range(4):
            frame = b.frame()
            if frame is None:
                time.sleep(.3)
                continue
            if self._minxin_is_skill(frame):
                if not self._minxin_tap(frame, 673, 504, "民心-关闭技能弹窗"):
                    return False
            elif self._minxin_is_page(frame):
                if not self._minxin_tap(frame, 673, 322, "民心-关闭面板"):
                    return False
            else:
                return b.ensure_main()
            time.sleep(.5)
        return b.ensure_main()

    def _minxin_process_skill(self, name, box):
        b = self.bot
        state = "unknown"
        for attempt in range(4):
            frame = b.frame()
            if not self._minxin_is_page(frame) or self._minxin_is_skill(frame):
                time.sleep(.3)
                continue
            try:
                state, text = self._minxin_card_state(frame, box)
            except Exception as exc:
                b.log(f"[MINXIN] {name}状态重读#{attempt + 1}：{exc}")
                time.sleep(.3)
                continue
            b.log(f"[MINXIN] {name} state={state} OCR={text!r}")
            if state in ("active", "timer", "unavailable"):
                return True
            if state == "ready":
                # Small offsets remain inside the same freshly located card.
                cx, cy = (box[0]+box[2])//2, (box[1]+box[3])//2
                if not self._minxin_tap(frame, cx + (attempt % 2)*8, cy,
                                        f"民心-{name}-打开#{attempt + 1}"):
                    return False
                time.sleep(.5)
                frame = b.frame()
                if self._minxin_is_skill(frame):
                    break
            time.sleep(.3)
        else:
            b.fail_snapshot("minxin_skill_open_unconfirmed")
            return False

        # Confirm the skill name and enabled activation control before use.
        for attempt in range(4):
            frame = b.frame()
            if not self._minxin_is_skill(frame):
                time.sleep(.3)
                continue
            try:
                title = "".join(read_text(frame, (215, 475, 510, 535), scale=3)
                                .get("text", "").split())
            except Exception as exc:
                b.log(f"[MINXIN] 技能标题重读#{attempt + 1}：{exc}")
                time.sleep(.35)
                continue
            activate = b.vision.match(frame, "minxin_activate")
            detail_state, detail_text = self._minxin_detail_status(frame)
            if name in title and detail_state in ("active", "timer"):
                b.log(f"[MINXIN] {name}详情确认已生效/冷却：{detail_text}")
                self._minxin_tap(frame, 673, 504, "民心-关闭已生效详情")
                return bool(b.wait_template_gone("minxin_skill_cost", 3))
            if name in title and activate and self._minxin_color_ratio(
                    frame, (205, 1000, 515, 1075), green=True) > .40:
                if not b.actions.tap_match(activate, frame.shape, f"民心-{name}-激活技能"):
                    return False
                learner = getattr(b, "learning", None)
                activation_ticket = learner.pending if learner else None
                break
            b.log(f"[MINXIN] 激活前复核#{attempt + 1} title={title!r}")
            time.sleep(.35)
        else:
            b.fail_snapshot("minxin_activate_unconfirmed")
            return False

        # Never send a second activation while waiting for confirmation.
        for attempt in range(6):
            time.sleep(.5)
            frame = b.frame()
            if frame is not None and b.is_main(frame):
                # Activation on this client closes the whole panel. Reopen it
                # and inspect the SAME skill instead of timing out on the city.
                hits = [b.vision.match(frame, key) for key in
                        ("minxin_entry_tip", "minxin_entry_tip_alt")]
                hit = max((m for m in hits if m), key=lambda m: m.score, default=None)
                if hit:
                    b.actions.tap_frame(hit.center[0],
                                        max(0, hit.center[1]-round(28*frame.shape[0]/1600)),
                                        frame.shape, "民心-激活自动关闭后重新打开核对")
                    time.sleep(.4)
                continue
            if self._minxin_is_skill(frame):
                state, text = self._minxin_detail_status(frame)
                if state in ("active", "timer"):
                    if learner:
                        learner.finish(activation_ticket, f"民心{name}详情生效OCR: {text}")
                    b.log(f"[OK] 民心-{name}详情已确认生效：{text}")
                    self._minxin_tap(frame, 673, 504, "民心-关闭已生效详情")
                    return bool(b.wait_template_gone("minxin_skill_cost", 3))
                if attempt >= 1:
                    # Some clients leave the detail open after activation.
                    # Close it and inspect the card before judging the result.
                    if not self._minxin_tap(frame, 673, 504, "民心-激活后关闭详情复核"):
                        return False
                continue
            if self._minxin_is_page(frame):
                try:
                    state, text = self._minxin_card_state(frame, box)
                except Exception as exc:
                    b.log(f"[MINXIN] 生效状态重读#{attempt + 1}：{exc}")
                    continue
                if state in ("active", "timer"):
                    if learner:
                        learner.finish(activation_ticket, f"民心{name}生效OCR: {text}")
                    b.log(f"[OK] 民心-{name}已激活：{text}")
                    return True
                if attempt >= 1:
                    cx, cy = (box[0]+box[2])//2, (box[1]+box[3])//2
                    self._minxin_tap(frame, cx, cy, "民心-重新打开原技能详情确认生效")
        b.fail_snapshot("minxin_activation_result_unknown")
        if learner:
            learner.finish(activation_ticket, "民心生效状态未确认", success=False, frame=frame)
        return False

    def minxin(self):
        b = self.bot
        b.log("[FLOW] 民心政令技能")
        if not b.ensure_main():
            return False
        opened = False
        for attempt in range(4):
            frame = b.frame()
            if self._minxin_is_page(frame):
                opened = True
                break
            hits = []
            if frame is not None:
                for template_name in ("minxin_entry_tip", "minxin_entry_tip_alt"):
                    hit = b.vision.match(frame, template_name)
                    if hit:
                        hits.append(hit)
            entry = max(hits, key=lambda hit: hit.score) if hits else None
            if entry:
                # Template contains only the lower heart outline, no number.
                # The crop is below the numeric value; tap the heart's visual
                # center above that crop rather than its lower-tip center.
                if not b.actions.tap_frame(
                        entry.center[0], max(0, entry.center[1] - 28), frame.shape,
                        f"民心-红心入口#{attempt + 1}"):
                    return False
            else:
                b.log(f"[MINXIN] 红心入口第{attempt + 1}帧仍未命中，重取画面")
            time.sleep(.5)
        if not opened:
            opened = self._minxin_is_page(b.frame())
        if not opened:
            b.log("[MINXIN] 初次未找到入口，先出城/入城复位视图再重找")
            if not self._reset_city_world_for_entry("民心入口复位", cycles=2):
                b.log("[MINXIN] 城内外视图复位未完成，继续基于当前画面重试入口")
            for attempt in range(4):
                frame = b.frame()
                if self._minxin_is_page(frame):
                    opened = True
                    break
                hits = []
                if frame is not None:
                    for template_name in ("minxin_entry_tip", "minxin_entry_tip_alt"):
                        hit = b.vision.match(frame, template_name)
                        if hit:
                            hits.append(hit)
                entry = max(hits, key=lambda hit: hit.score) if hits else None
                if entry:
                    if not b.actions.tap_frame(entry.center[0],
                                               max(0, entry.center[1] - 28),
                                               frame.shape,
                                               f"民心-复位后红心入口#{attempt + 1}"):
                        return False
                time.sleep(.5)
            opened = opened or self._minxin_is_page(b.frame())
        if not opened:
            # The new season may expose 民心 through the blue 天命 control.
            # Missing the legacy heart does not prove that 民心 is unavailable.
            b.log("[MINXIN] 红心入口未确认；新赛季可能经天命进入，保留未知状态")
            b.fail_snapshot("minxin_entry_missing")
            return False
        completed = False
        learner = getattr(b, "learning", None)
        if learner:
            learner.outcome(b.frame(), target="page_minxin")
        try:
            completed = all(self._minxin_process_skill(name, box) for name, box in (
                ("画饼充饥", (55, 1110, 240, 1240)),
                ("半夜鸡叫", (265, 1110, 450, 1240)),
            ))
        finally:
            exited = self._minxin_exit()
        return completed and exited
