"""Timed activity battles, congratulations, and rally reward limits."""
import re
import time

import cv2
import numpy as np

from local_ocr import read_text


def compact(text):
    return re.sub(r"\s+", "", text)


def read_ratios(frame):
    """Read all three percentage fields together, including single digits."""
    frame = cv2.resize(frame, (726, 1600))
    canvas = np.full((1600, 726, 3), 255, np.uint8)
    for i, (left, right) in enumerate(((90, 217), (318, 445), (541, 669))):
        patch = cv2.cvtColor(frame[779:820, left:right], cv2.COLOR_BGR2GRAY)
        patch = cv2.cvtColor(np.where(patch > 200, 0, 255).astype("uint8"), cv2.COLOR_GRAY2BGR)
        y = 20 + i*80
        cv2.putText(canvas, chr(65+i)+" VALUE", (8,y+30), cv2.FONT_HERSHEY_SIMPLEX, .8, (0,0,0), 2)
        canvas[y:y+41, 150:150+patch.shape[1]] = patch
        cv2.putText(canvas, "END", (310,y+30), cv2.FONT_HERSHEY_SIMPLEX, .8, (0,0,0), 2)
    data = read_text(canvas, (0,0,500,260), scale=3)
    words = [word for line in data["lines"] for word in line["words"]]
    values = []
    for i in range(3):
        candidates = sorted((w for w in words if 150 <= w["x"] < 280
                             and 20+i*80 <= w["y"] < 70+i*80), key=lambda w: w["x"])
        text = "".join(w["text"] for w in candidates).replace("%", "")
        if not re.fullmatch(r"\d{1,3}", text):
            raise ValueError(f"无法确认第{i+1}项兵力比例：{text!r}")
        values.append(int(text))
    if any(n > 100 for n in values) or sum(values) > 100:
        raise ValueError(f"兵力比例读数无效：{values}")
    return tuple(values)


class ActivityTasks:
    def _chest_activity_visible(self, frame=None):
        if frame is None:
            frame = self.bot.frame()
        return bool(frame is not None and (
            self.bot.vision.match(frame, "page_chest_activity") or
            self.bot.vision.match(frame, "chest_go")
        ))

    def _activity_tap(self, name, label, timeout=3):
        b = self.bot
        names = (name, "chest_equipment_blue") if name == "chest_equipment" else (name,)
        if not b.wait_template_any(names, timeout):
            return False
        fr = b.frame()
        m = next((m for n in names if (m := b.vision.match(fr, n))), None) if fr is not None else None
        return bool(m and b.actions.tap_match(m, fr.shape, label))

    def _open_chest_activity(self):
        b = self.bot
        if not b.ensure_main():
            return False
        if not self._activity_tap("activity_entry", "打开活动"):
            return False
        if not b.wait_template("page_activity", 5):
            return False
        for index in range(12):
            fr = b.frame()
            if fr is None:
                continue
            if self._chest_activity_visible(fr):
                return True
            tab = b.vision.match(fr, "chest_activity_tab")
            if tab:
                if not b.actions.tap_match(tab, fr.shape, "群雄混战-活动页签"):
                    return False
                deadline = time.monotonic() + 4
                while time.monotonic() < deadline:
                    current = b.frame()
                    if self._chest_activity_visible(current):
                        b.log("[CHEST] 通过整页模板或唯一‘前往’按钮确认群雄混战页")
                        return True
                    time.sleep(.15)
                b.fail_snapshot("chest_activity_page_unconfirmed")
                return False
            # 浏览底部右侧的活动，手指向左推；扫到末端后反向复查。
            h, w = fr.shape[:2]
            start, end = (.86, .32) if index < 8 else (.32, .86)
            if not b.actions.swipe_frame(round(w*start), round(h*.946),
                                         round(w*end), round(h*.946), fr.shape,
                                         500, "活动栏-寻找群雄混战"):
                return False
            time.sleep(.45)
        b.log("[CHEST] 当前活动栏未发现群雄混战，本轮跳过")
        if not self._tap_rel(.086, .942, "活动-返回") or not b.wait_main(4):
            return False
        return None

    def _chest_strengthened_faction(self, fr):
        # 读取加强说明，不能用标题的魏/吴/蜀（那是本次敌人）选我方阵营。
        text = compact(read_text(fr, (305,433,568,475), scale=3, context=True)["text"])
        match = re.search(r"([蜀吴魏])国阵", text)
        if match:
            return match.group(1)
        if self.bot.vision.match(fr, "chest_strength_shu"):
            return "蜀"
        return None

    def _chest_selected_faction(self, fr):
        for faction, name in (("蜀", "chest_formation_shu"), ("吴", "chest_formation_wu")):
            if self.bot.vision.match(fr, name):
                return faction
        text = compact(read_text(fr, (245,75,490,140), scale=3, context=True)["text"])
        match = re.search(r"([蜀吴魏])阵营", text)
        return match.group(1) if match else None

    def _chest_set_ratios(self):
        b = self.bot
        goal = (5, 5, 90)
        current = read_ratios(b.frame())
        clicked = 0
        unchanged = 0
        for _ in range(45):
            if current == goal:
                time.sleep(.2)
                if read_ratios(b.frame()) != goal:
                    return False
                b.log("[CHEST] 两次确认兵力配比：步兵5% 骑兵5% 弓兵90%")
                return self._activity_tap("chest_ratio_confirm", "兵力配比-确认5/5/90")
            # 总配比不能超过100：先减掉超额，再增加不足项。
            excess = [i for i in range(3) if current[i] > goal[i]]
            index = max(excess, key=lambda i: current[i]-goal[i]) if excess else max(
                range(3), key=lambda i: goal[i]-current[i])
            decrease = bool(excess)
            count = min(8, abs(current[index]-goal[index]))
            if not decrease:
                count = min(count, 100-sum(current))
            if count <= 0 or clicked+count > 220:
                return False
            fr = b.frame()
            if fr is None or not b.vision.match(fr, "page_chest_ratio"):
                return False
            # 已确认弹窗后使用每列的减/加按钮，不改底部默认比例勾选框。
            x = ((86, 312, 539) if decrease else (187, 414, 640))[index]
            h, w = fr.shape[:2]
            for _ in range(count):
                if not b.actions.tap_frame(round(w*x/726), round(h*873/1600), fr.shape,
                                            f"兵力配比-第{index+1}项{'减少' if decrease else '增加'}"):
                    return False
                time.sleep(.04)
            clicked += count
            time.sleep(.2)
            after = read_ratios(b.frame())
            b.log(f"[CHEST] 配比 {current} -> {after}，目标{goal}")
            unchanged = unchanged+1 if after == current else 0
            if unchanged >= 2:
                return False
            current = after
        return False

    def _chest_prepare_formation(self, faction):
        b = self.bot
        if not b.wait_template_any(("chest_equipment", "chest_equipment_blue"), 5):
            return False
        # 第3/4/5编队分别为蜀/吴/魏；点击后必须确认顶部阵营实际切换。
        positions = {"蜀": 277, "吴": 333, "魏": 387}
        if faction not in positions:
            return False
        if not self._tap_rel(positions[faction]/726, 1370/1600, f"打宝箱-选择{faction}阵营"):
            return False
        time.sleep(.6)
        if self._chest_selected_faction(b.frame()) != faction:
            b.fail_snapshot("chest_faction_not_confirmed")
            return False
        if not self._activity_tap("chest_equipment", "打宝箱-最强装备"):
            return False
        time.sleep(.4)
        if not self._activity_tap("chest_ratio_open", "打宝箱-兵力配比"):
            return False
        if not b.wait_template("page_chest_ratio", 3) or not self._chest_set_ratios():
            b.fail_snapshot("chest_ratio_not_confirmed")
            return False
        if not b.wait_template_gone("page_chest_ratio", 3):
            return False
        return bool(b.wait_template_any(("chest_equipment", "chest_equipment_blue"), 3))

    def _chest_remaining_attacks(self, fr):
        text = compact(read_text(fr, (242,512,485,550), context=True)["text"])
        found = re.search(r"(\d+)\s*/\s*(\d+)", text)
        if not found:
            return None
        remaining, maximum = map(int, found.groups())
        return remaining if 0 <= remaining <= maximum <= 99 else None

    def _chest_wait_for_next(self, departed_at):
        b = self.bot
        deadline = departed_at+60
        while time.monotonic() < deadline:
            remaining = deadline-time.monotonic()
            b.log(f"[CHEST] 下一次讨伐还需等待约{max(1,round(remaining))}秒")
            if b.is_main() and not self.handle_main_extras():
                return False
            time.sleep(min(5, max(0, deadline-time.monotonic())))
        return True

    def chest_battles(self):
        b = self.bot
        limit = max(0, min(5, int(self._opts().get("chest_count", 2))))
        b.log(f"\n[FLOW] 群雄混战打宝箱，本轮最多{limit}次，间隔至少一分钟")
        for index in range(limit):
            opened = self._open_chest_activity()
            if opened is None:
                return True
            if not opened:
                return False
            waiting = []
            for _ in range(2):
                current = b.frame()
                if current is None or b.vision.match(current, "chest_go"):
                    break
                # The unopen event uses bright yellow lettering. Windows OCR
                # read the title as gibberish in the saved Oct 15 frame, while
                # the title itself is visually stable and the timer is numeric.
                next_cao = b.vision.match(current, "chest_next_cao_cao")
                if next_cao:
                    timer = compact(read_text(
                        current, (385, 1340, 560, 1415), scale=4,
                        context=True)["text"])
                    groups = [int(part) for part in re.findall(r"\d+", timer)]
                    if (len(groups) == 3 and groups[0] <= 24
                            and groups[1] < 60 and groups[2] < 60):
                        waiting.append("距离曹操出现 " + ":".join(
                            f"{part:02d}" for part in groups))
                        b.log(f"[CHEST] 未开放标题 score={next_cao.score:.3f} "
                              f"倒计时={waiting[-1]}")
                        time.sleep(.25)
                        continue
                text = compact(read_text(current, (155,1300,585,1430),
                                         scale=3, context=True)["text"])
                timer_text = re.sub(r"(?<=\d)[．.](?=\d)", ":", text)
                if ("距离" in text and "出现" in text and
                        re.search(r"\d{1,2}[：:]\d{2}[：:]\d{2}", timer_text)):
                    waiting.append(text)
                else:
                    break
                time.sleep(.25)
            if len(waiting) == 2:
                b.log(f"[WAITING] 群雄混战尚未开放：{waiting[-1]}；本轮等待，不计失败")
                b.task_waiting_reason = waiting[-1]
                return bool(self._tap_rel(.086, .942, "群雄混战-未开放返回")
                            and b.wait_main(5))
            faction = self._chest_strengthened_faction(b.frame())
            if faction is None:
                b.fail_snapshot("chest_strength_unknown")
                return False
            b.log(f"[CHEST] 当次加强阵营：{faction}")
            if not self._activity_tap("chest_go", "群雄混战-前往"):
                b.fail_snapshot("chest_go_unavailable")
                return False
            if not b.wait_template("page_chest_target", 5):
                return False
            remaining = self._chest_remaining_attacks(b.frame())
            if remaining is None:
                b.fail_snapshot("chest_remaining_unknown")
                return False
            if remaining == 0:
                b.log("[CHEST] 当天剩余讨伐次数为0，结束")
                return b.ensure_main()
            if not self._activity_tap("chest_attack", "群雄混战-攻击"):
                return False
            if not self._chest_prepare_formation(faction):
                return False
            if not self._activity_tap("chest_depart", f"打宝箱-第{index+1}次出征"):
                return False
            if not b.wait_main(6):
                b.fail_snapshot("chest_depart_not_confirmed")
                return False
            departed_at = time.monotonic()
            b.log(f"[OK] 第{index+1}次打宝箱已出征")
            if index+1 < limit and remaining > 1:
                if not self._chest_wait_for_next(departed_at):
                    return False
            else:
                break
        return True

    def _handle_congratulations(self, already_open=False):
        b = self.bot
        fr = b.frame()
        if not already_open:
            entry = b.vision.match(fr, "congrats_entry") if fr is not None else None
            if not entry:
                return True
            if not b.actions.tap_match(entry, fr.shape, "黄色小手-打开祝贺"):
                return False
            if not b.wait_template("page_congrats", 4):
                return False
        for _ in range(12):
            fr = b.frame()
            if fr is None:
                time.sleep(.2)
                continue
            cont = b.vision.match(fr, "reward_continue")
            if cont:
                if not b.actions.tap_match(cont, fr.shape, "祝贺奖励-继续"):
                    return False
                time.sleep(.5)
                continue
            if not b.vision.match(fr, "page_congrats"):
                if b.is_main(fr):
                    return True
                time.sleep(.2)
                continue
            button = b.vision.match(fr, "congrats_all")
            label = "全部祝贺"
            if not button:
                button = b.vision.match(fr, "congrats_thumb")
                label = "单人祝贺-黄色小手"
            if button:
                if not b.actions.tap_match(button, fr.shape, label):
                    return False
                time.sleep(.7)
                # 点赞后按钮可能保留原样；本条处理后关窗，再从入口检查下一条。
                fr = b.frame()
                if b.vision.match(fr, "reward_continue"):
                    continue
            if b.vision.match(fr, "page_congrats"):
                if not self._tap_rel(.90, .131, "祝贺弹窗-X关闭"):
                    return False
                if not b.wait_template_gone("page_congrats", 3):
                    return False
            if not b.wait_main(3):
                return False
            fr = b.frame()
            entry = b.vision.match(fr, "congrats_entry") if fr is not None else None
            if not entry:
                return True
            if not b.actions.tap_match(entry, fr.shape, "祝贺-检查下一条"):
                return False
            if not b.wait_template("page_congrats", 3):
                return False
        b.fail_snapshot("congrats_not_finished")
        return False

    def _auto_rally_reward_limits(self):
        """未满奖励勾选、已满奖励取消，并确认每次切换结果。"""
        b = self.bot
        fr = b.frame()
        if fr is None:
            return False
        data = read_text(fr, (115,710,650,980), scale=2)
        rows = []
        for line in data["lines"]:
            found = re.search(r"(\d+)/(\d+)", compact(line["text"]))
            if found and line["words"]:
                used, maximum = map(int, found.groups())
                if maximum <= 0 or used > maximum:
                    return False
                y = min(word["y"] for word in line["words"])
                rows.append((used, maximum, y))
        if len(rows) != 2:
            b.fail_snapshot("auto_rally_reward_count_unknown")
            return False
        def checkbox_state(frame, row_y):
            if frame is None:
                return None, None
            candidates = []
            for name, state in (("auto_join_checked", "checked"),
                                ("auto_join_unchecked", "unchecked")):
                for score, x, cy in self._multi_template_candidates(
                    frame, name, .84, nms_distance=15
                ):
                    if abs(cy - row_y) < frame.shape[0] * .025:
                        candidates.append((-score, abs(cy - row_y), state, x, cy))
            if not candidates:
                return None, None
            _, _, state, x, cy = min(candidates)
            return state, (x, cy)

        for used, maximum, y in rows:
            desired = "checked" if used < maximum else "unchecked"
            fr = b.frame()
            state, position = checkbox_state(fr, y)
            if state is None:
                b.log(f"[STOP] 自动集结奖励{used}/{maximum}的勾选状态未识别")
                b.fail_snapshot("auto_rally_checkbox_unknown")
                return False
            if state == desired:
                b.log(f"[AUTO RALLY] 奖励{used}/{maximum}勾选状态正确：{state}")
                continue
            x, cy = position
            label = "勾选未满奖励类型" if desired == "checked" else "取消已满奖励类型"
            b.log(f"[AUTO RALLY] 奖励{used}/{maximum}，{label}")
            if not b.actions.tap_frame(x, cy, fr.shape, f"自动集结-{label}"):
                return False
            confirmed = False
            for _ in range(6):
                time.sleep(.15)
                current = b.frame()
                now, current_position = checkbox_state(current, y)
                if (now == desired and current_position is not None
                        and abs(current_position[0] - x) < 15
                        and abs(current_position[1] - cy) < 20):
                    confirmed = True
                    break
            if not confirmed:
                b.fail_snapshot("auto_rally_checkbox_change_unconfirmed")
                return False
            learner = getattr(b, "learning", None)
            if learner:
                learner.verified(f"自动集结{used}/{maximum}勾选状态已变为{desired}")
        return True
