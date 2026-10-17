"""Bounded Three Heroes vs Lv Bu rally workflow, verified against user video."""
import re
import time
import json
import subprocess
import datetime as dt
from pathlib import Path

import cv2

from local_ocr import read_text


def clean(text):
    return re.sub(r"\s+", "", text)


def army_capacity(text):
    """OCR can mistake the small troop type icon for a Chinese character."""
    text = clean(text)
    match = re.search(r"容量[:：]?[^0-9]{0,4}(\d{2,9})/[^0-9]{0,4}(\d{2,9})", text)
    if not match:
        return None
    current, maximum = map(int, match.groups())
    return (current, maximum) if 0 < current <= maximum else None


class LubuTasks:
    def _lubu_match(self, frame, name):
        return None if frame is None else self.bot.vision.match(frame, name)

    def _lubu_tap(self, name, label, timeout=4):
        b = self.bot
        if not b.wait_template(name, timeout):
            return False
        fr = b.frame()
        match = self._lubu_match(fr, name)
        return bool(match and b.actions.tap_match(match, fr.shape, label))

    def _lubu_count(self, frame):
        # Use the same current-screen queue reader as gathering and beacon.
        # The old lubu_header art is absent on some accounts even though the
        # numeric queue header (for example 0/5) is clearly visible.
        last_error = None
        for attempt in range(3):
            try:
                _, used, maximum, _ = self._march_state(frame if attempt == 0 else None)
                return used, maximum
            except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
                last_error = exc
                self.bot.log(f"[LUBU] 行军数量第{attempt + 1}帧未读清：{exc}")
        raise ValueError(f"连续3帧未确认吕布行军数量：{last_error}")

    def _lubu_ratios(self, frame):
        anchor = self._lubu_match(frame, "lubu_ratio")
        if not anchor:
            raise ValueError("兵力配比弹窗未确认")
        dy = round(anchor.center[1]*1600/frame.shape[0]-516)
        data = read_text(frame, (60,815+dy,665,870+dy), scale=3)
        groups = [[], [], []]
        for line in data["lines"]:
            for word in line["words"]:
                text = clean(word["text"]).rstrip("%")
                x = word["x"]*726/frame.shape[1]
                if re.fullmatch(r"\d{1,3}", text):
                    column = 0 if x < 250 else 1 if x < 475 else 2
                    groups[column].append(int(text))
        if any(len(g) != 1 for g in groups):
            raise ValueError("三项配比读数不完整")
        result = tuple(g[0] for g in groups)
        if sum(result) > 100 or any(x > 100 for x in result):
            raise ValueError("配比读数无效")
        return result, dy

    def _lubu_set_ratios(self):
        b = self.bot
        goal = (10,10,80)
        previous = None
        unchanged = 0
        clicks = 0
        for _ in range(45):
            fr = b.frame()
            values, dy = self._lubu_ratios(fr)
            if values == goal:
                time.sleep(.2)
                if self._lubu_ratios(b.frame())[0] != goal:
                    return False
                return self._lubu_tap("lubu_ratio_confirm", "吕布配比确认10/10/80")
            unchanged = unchanged+1 if values == previous else 0
            if unchanged >= 2:
                return False
            previous = values
            excess = [i for i in range(3) if values[i] > goal[i]]
            i = max(excess, key=lambda n: values[n]-goal[n]) if excess else max(range(3), key=lambda n: goal[n]-values[n])
            decrease = bool(excess)
            count = min(5, abs(values[i]-goal[i]))
            if not decrease:
                count = min(count, 100-sum(values))
            if count <= 0 or clicks+count > 220:
                return False
            x = ((86,312,539) if decrease else (187,414,640))[i]
            for _ in range(count):
                if not b.actions.tap_frame(round(x*fr.shape[1]/726), round((911+dy)*fr.shape[0]/1600), fr.shape, "吕布调整兵力配比"):
                    return False
                time.sleep(.08)
            clicks += count
            time.sleep(.25)
        return False

    def _lubu_prepare(self, slot):
        b = self.bot
        if not b.wait_template("lubu_march_controls", 5):
            return False
        # Preset slot positions are fixed; skip presets with no available troops.
        if not self._tap_rel((64+34*(slot-1))/444, 855/980, f"吕布选择编队{slot}"):
            return False
        time.sleep(.5)
        if not b.wait_template_any(("lubu_equipment","chest_equipment","chest_equipment_blue"),4):
            return False
        fr = b.frame()
        equipment = next((b.vision.match(fr,name) for name in ("lubu_equipment","chest_equipment_blue","chest_equipment") if fr is not None and b.vision.match(fr,name)),None)
        if fr is None or not equipment or not b.actions.tap_match(equipment,fr.shape,"吕布最强装备"):
            return False
        if not self._lubu_tap("lubu_march_controls", "吕布兵力配比"):
            return False
        if not b.wait_template("lubu_ratio", 4) or not self._lubu_set_ratios():
            return False
        if not b.wait_template_gone("lubu_ratio", 4):
            return False
        fr = b.frame()
        text = clean(read_text(fr, (0,1395,405,1450), scale=3)["text"])
        if not army_capacity(text):
            b.log("[LUBU] 编队兵力未确认，停止出征："+text)
            return False
        return bool(self._lubu_match(fr, "lubu_march_controls"))

    def _lubu_candidates(self, frame):
        if not self._lubu_match(frame, "lubu_war"):
            return []
        targets = self._multi_template_candidates(frame, "lubu_card", .87, nms_distance=60)
        joins = self._multi_template_candidates(frame, "lubu_join", .80, nms_distance=35)
        sx, sy = frame.shape[1]/726, frame.shape[0]/1600
        result = []
        for score,x,y in joins:
            # Pair the button with the typed target in the same card only.
            if not any(80*sx < tx-x < 175*sx and 25*sy < y-ty < 85*sy for _,tx,ty in targets):
                continue
            if not 310*sy < y < 1250*sy:
                continue
            # Disabled/already joined buttons are muted; active buttons are green.
            x1,x2 = max(0,round(x-28*sx)),min(frame.shape[1],round(x+29*sx))
            y1,y2 = max(0,round(y-18*sy)),min(frame.shape[0],round(y+19*sy))
            hsv = cv2.cvtColor(frame[y1:y2,x1:x2],cv2.COLOR_BGR2HSV)
            green = cv2.inRange(hsv,(35,65,60),(95,255,255))
            if cv2.countNonZero(green)/max(1,green.size) < .35:
                continue
            result.append((x,y))
        return sorted(result,key=lambda p:p[1])

    def _lubu_join_seconds(self, frame, x, y):
        # Countdown sits immediately above the Join control in the matching card.
        roi = (round((x-120)*726/frame.shape[1]), round((y-185)*1600/frame.shape[0]),
               round((x+130)*726/frame.shape[1]), round((y-130)*1600/frame.shape[0]))
        data = read_text(frame, roi, scale=3)
        left = (x-120)*726/frame.shape[1]
        tokens = []
        for line in data["lines"]:
            for word in sorted(line["words"], key=lambda item:item["x"]):
                token = clean(word["text"]).replace("O","0").replace("o","0")
                if word["x"] >= left+95:
                    tokens.extend(re.findall(r"\d{1,2}",token))
        if len(tokens) < 3:
            return None
        hours, minutes, seconds = map(int, tokens[-3:])
        if minutes > 59 or seconds > 59:
            return None
        return hours*3600+minutes*60+seconds

    def _lubu_open_war(self):
        b = self.bot
        if self._lubu_match(b.frame(), "lubu_war"):
            return True
        if not b.ensure_main() or not b.tap_template("main_alliance",3,"吕布进入同盟"):
            return False
        if not b.wait_template("alliance_war",5) or not b.tap_template("alliance_war",3,"吕布同盟战争"):
            return False
        return bool(b.wait_template("lubu_war",5))

    def _lubu_join(self):
        """Return ready / empty / stale; never cache a scrolling-list click."""
        b = self.bot
        if not self._lubu_open_war():
            return "error"
        for scan in range(6):
            fr = b.frame()
            if not self._lubu_match(fr, "lubu_war"):
                return "error"
            candidates = self._lubu_candidates(fr)
            if candidates:
                # A fresh frame avoids clicking a card that moved during matching.
                fr = b.frame()
                candidates = self._lubu_candidates(fr)
                if not candidates:
                    continue
                x,y = candidates[0]
                remaining = self._lubu_join_seconds(fr,x,y)
                if remaining is None or remaining < 25:
                    b.log("[LUBU] 集结倒计时不足以完成编队和抵达，跳过该队")
                    # Do not join an expiring or unreadable rally.
                    candidates = candidates[1:]
                    if candidates:
                        x,y = candidates[0]
                        remaining = self._lubu_join_seconds(fr,x,y)
                    else:
                        return "stale"
                    if remaining is None or remaining < 25:
                        return "stale"
                if not b.actions.tap_frame(x,y,fr.shape,"加入吕布活动集结"):
                    return "error"
                if not b.wait_template("lubu_march_target",4):
                    return "stale"
                return "ready"
            h,w = fr.shape[:2]
            if not b.actions.swipe_frame(round(w*.48),round(h*.73),round(w*.48),round(h*.30),fr.shape,450,"寻找吕布集结"):
                return "error"
            time.sleep(.4)
        return "empty"

    def _lubu_start_own(self):
        """Only start from an identified event marker/detail; never arbitrary cities."""
        b = self.bot
        fr = b.frame()
        if not self._lubu_match(fr,"lubu_map_detail"):
            label = self._lubu_match(fr,"lubu_event_label")
            if not label:
                return "unavailable"
            if not b.actions.tap_match(label,fr.shape,"选择三英战吕布目标"):
                return "error"
            if not b.wait_template("lubu_map_detail",4):
                return "unavailable"
        if not self._lubu_tap("lubu_map_rally","吕布发起集结"):
            return "error"
        if not b.wait_template("lubu_start_dialog",4) or not b.wait_template("lubu_start_three",3):
            return "error"
        if not self._lubu_tap("lubu_start","吕布3分钟集结"):
            return "error"
        return "ready" if b.wait_template("lubu_march_controls",5) else "error"

    def _lubu_wait(self, seconds, deadline):
        end = min(deadline,time.monotonic()+seconds)
        while time.monotonic() < end:
            time.sleep(min(2,end-time.monotonic()))

    def lubu(self):
        try:
            return self._lubu_run()
        except (ValueError, RuntimeError, OSError, cv2.error, subprocess.TimeoutExpired) as exc:
            self.bot.log(f"[STOP] 吕布状态无法安全识别：{exc}")
            self.bot.fail_snapshot("lubu_state_unconfirmed")
            return False

    def _lubu_run(self):
        from workflow_state import WorkflowState, LUBU_TRANSITIONS
        b = self.bot
        state = WorkflowState(b, "lubu", LUBU_TRANSITIONS)
        opts = self._opts()
        duration = float(opts.get("lubu_minutes",20))
        if not 0 < duration <= 120:
            raise ValueError("吕布运行时长必须在0到120分钟之间")
        deadline = time.monotonic()+duration*60
        slots = opts.get("lubu_formations", [1,2,3,4,5,6])
        if not slots or any(s not in range(1,7) for s in slots):
            raise ValueError("吕布编队必须为1到6")
        state_path = Path(__file__).parent / "runtime" / "lubu_state.json"
        sent = 0
        if state_path.exists():
            try:
                saved = json.loads(state_path.read_text(encoding="utf-8"))
                stamp = dt.datetime.strptime(saved["last_confirmed_at"], "%Y-%m-%d %H:%M:%S")
                if stamp.date() == dt.datetime.now().date() and saved.get("event") == "三英战吕布":
                    sent = max(0,int(saved.get("confirmed_departures",0)))
                    b.log(f"[LUBU] 接续本日记录：已确认出征{sent}次")
            except (ValueError, TypeError, KeyError, OSError, json.JSONDecodeError):
                b.log("[LUBU] 本地进度记录无效，从当前游戏画面重新判断")
        attempted_own = False
        misses = 0
        b.log(f"[FLOW] 三英战吕布，最多运行{duration:g}分钟；兵力10/10/80")
        while time.monotonic() < deadline:
            if not self._ensure_world():
                state.move("unknown", "未确认世界地图")
                return False
            fr = b.frame()
            state.move("observe", "新帧确认世界地图，读取活动阶段与动态行军队列")
            if self._lubu_match(fr,"lubu_cooldown"):
                state.move("complete", "当前画面确认活动冷却")
                b.log(f"[OK] 吕布活动进入冷却，停止派遣；已确认出征{sent}次")
                return True
            before, capacity = self._lubu_count(fr)
            if before >= capacity:
                state.move("waiting", f"当前行军队列={before}/{capacity}，无空位")
                b.log(f"[LUBU] 行军{before}/{capacity}已满，等待空位")
                self._lubu_wait(8,deadline)
                continue
            own = not attempted_own and not self._lubu_match(fr,"lubu_activity_header")
            result = "unavailable"
            if own:
                state.move("own", "当前地图有空位，检查自建吕布集结入口")
                attempted_own = True
                result = self._lubu_start_own()
            if result == "unavailable":
                own = False
                state.move("join", "自建集结不可用，检查当前同盟战争列表")
                result = self._lubu_join()
            if result in {"empty","stale"}:
                misses += 1
                b.log(f"[LUBU] 无可加入目标或列表已刷新，第{misses}次；回地图复查")
                if misses >= 8:
                    b.fail_snapshot("lubu_no_available_rally")
                    return False
                if not b.ensure_main():
                    return False
                self._lubu_wait(8,deadline)
                continue
            if result != "ready":
                state.move("unknown", f"集结入口未确认: {result}")
                b.fail_snapshot("lubu_open_failed")
                return False
            state.move("prepare", "已确认目标编队页，准备并复核本次兵力")
            if not self._lubu_prepare(slots[sent % len(slots)]):
                state.move("unknown", "编队或兵力配比未确认")
                b.fail_snapshot("lubu_formation_unconfirmed")
                return False
            if time.monotonic() >= deadline:
                b.log("[STOP] 吕布运行时限已到，未点击出征")
                recovered = b.ensure_main()
                state.move("complete" if recovered else "unknown",
                           "时限已到，未出征，主界面已确认" if recovered else "时限已到，主界面未确认")
                return recovered
            state.move("depart", "兵力容量已复核，点击当前编队出征")
            if not self._lubu_tap("lubu_depart","吕布出征"):
                state.move("unknown", "出征按钮未确认或点击失败")
                return False
            state.move("verify", "出征点击后重新读取世界地图行军数")
            # A dispatched join usually returns to the war list. Navigate back,
            # then require actual queue evidence; do not retry an ambiguous send.
            time.sleep(.8)
            if self._lubu_match(b.frame(),"lubu_march_controls"):
                b.fail_snapshot("lubu_depart_rejected")
                return False
            if not b.ensure_main():
                return False
            confirmed = False
            for _ in range(3):
                fr = b.frame()
                after, _ = self._lubu_count(fr)
                if after > before:
                    confirmed = True
                    break
                time.sleep(.5)
            if not confirmed:
                state.move("unknown", "出征后未确认队列增加或活动标志")
                b.fail_snapshot("lubu_depart_ambiguous")
                return False
            sent += 1
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps({"confirmed_departures":sent,
                                                "last_confirmed_at":time.strftime("%Y-%m-%d %H:%M:%S"),
                                                "event":"三英战吕布"},ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
            misses = 0
            b.log(f"[LUBU] 已确认第{sent}次出征，继续检查空闲队列")
        b.log(f"[INFO] 吕布运行时限已到，已确认出征{sent}次；未据此认定活动完成")
        recovered = b.ensure_main()
        state.move("complete" if recovered else "unknown",
                   "时限已到，主界面已确认；活动完成状态未声明" if recovered else "时限已到，主界面未确认")
        return recovered
