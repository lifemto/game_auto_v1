"""Free-only recruitment across awakening and regular tabs."""
import time

import cv2

from local_ocr import read_text


def page_state(tasks, frame):
    if frame is None:
        return "unknown"
    vision = tasks.bot.vision
    if any(vision.match(frame, name) for name in
           ("page_recruit_result", "page_daily_total_reward")):
        return "result"
    if vision.match(frame, "recruit_awaken_heading"):
        return "awaken"
    if any(vision.match(frame, name) for name in
           ("recruit_regular_heading", "page_recruit", "page_recruit_wechat")):
        return "regular"
    return "unknown"


def wait_state(tasks, timeout=5, target=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = page_state(tasks, tasks.bot.frame())
        if state != "unknown" and (target is None or state == target):
            return state
        time.sleep(.15)
    return "unknown"


def free_buttons(frame):
    """Find green controls, then read FREE inside each actual control."""
    h, w = frame.shape[:2]
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (35, 90, 70), (90, 255, 255))
    mask[:round(h*950/1600)] = 0
    mask[round(h*1370/1600):] = 0
    candidates, unknown = [], []
    for contour in cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0]:
        x, y, cw, ch = cv2.boundingRect(contour)
        if not (cw >= 105*w/726 and ch >= 30*h/1600):
            continue
        box = (round(x*726/w), round(y*1600/h),
               round((x+cw)*726/w), round((y+ch)*1600/h))
        readings = []
        for scale, mode in ((3, None), (2, "white")):
            text = "".join(read_text(frame, box, scale=scale, mode=mode,
                                     context=True).get("text", "").split()).replace("費", "费")
            readings.append(text)
            if "免费" in text:
                candidates.append({"center": (x+cw//2, y+ch//2), "text": text,
                                   "box": box})
                break
        else:
            # The two-line button can make OCR drop its second line. Read the
            # lower FREE label separately without the larger first line.
            lower = (box[0], (box[1]+box[3])//2, box[2], box[3]+3)
            text = "".join(read_text(frame, lower, scale=2, context=True)
                           .get("text", "").split()).replace("費", "费")
            if "免费" in text:
                candidates.append({"center": (x+cw//2, y+ch//2),
                                   "text": text, "box": box})
            else:
                unknown.append({"box": box, "text": readings+[text]})
    return sorted(candidates, key=lambda item: item["center"][0]), unknown


def switch_tab(tasks, target):
    b = tasks.bot
    frame = b.frame()
    if page_state(tasks, frame) == target:
        return True
    for name in (f"recruit_tab_{target}", f"recruit_tab_{target}_active"):
        hit = b.vision.match(frame, name) if frame is not None else None
        if hit:
            if not b.actions.tap_match(hit, frame.shape, f"招募-切换{target}"):
                return False
            if wait_state(tasks, target=target) == target:
                learner = getattr(b, "learning", None)
                if learner:
                    learner.verified(f"招募页签已切换，{target}专属标题已确认")
                return True
            return False
    return False


def claim_tab(tasks, target):
    b = tasks.bot
    empty_frames = 0
    for attempt in range(10):
        frame = b.frame()
        if page_state(tasks, frame) != target:
            b.fail_snapshot("recruit_tab_changed_unexpectedly")
            return False
        buttons, unknown = free_buttons(frame)
        if unknown:
            b.log(f"[RECRUIT] 绿色控件文字未读清：{unknown}")
            b.fail_snapshot("recruit_green_text_unknown")
            return False
        if not buttons:
            empty_frames += 1
            if empty_frames >= 2:
                b.log(f"[RECRUIT] {target}两帧确认无绿色免费控件，本页领取结束")
                return True
            time.sleep(.3)
            continue
        empty_frames = 0
        chosen = buttons[0]
        time.sleep(.25)
        fresh = b.frame()
        if page_state(tasks, fresh) != target:
            continue
        verified, uncertain = free_buttons(fresh)
        same = next((item for item in verified if
                     abs(item["center"][0]-chosen["center"][0]) < fresh.shape[1]*24/726 and
                     abs(item["center"][1]-chosen["center"][1]) < fresh.shape[0]*24/1600), None)
        if same is None or uncertain:
            b.fail_snapshot("recruit_free_changed_before_click")
            return False
        x, y = same["center"]
        b.log(f"[RECRUIT] {target}免费按钮 OCR={same['text']!r} frame=({x},{y})")
        if not b.actions.tap_frame(x, y, fresh.shape, f"{target}-免费招募"):
            return False
        learner = getattr(b, "learning", None)
        ticket = learner.pending if learner else None
        result = b.wait_template_any(("page_recruit_result", "page_daily_total_reward"), 8)
        if not result:
            b.fail_snapshot("recruit_result_unknown")
            return False
        if learner:
            learner.finish(ticket, f"{target}免费招募后已确认奖励结果页")
        if not tasks._exit_recruit_result_safely() or wait_state(tasks, target=target) != target:
            b.fail_snapshot("recruit_return_tab_unconfirmed")
            return False
        b.daily_state["free_recruit_count"] = b.daily_state.get("free_recruit_count", 0) + 1
        b.save_daily_state()
        # The next iteration re-reads every control. The local daily count is
        # a record, never a reason to skip advanced/awakening free recruitment.
    b.fail_snapshot("recruit_free_loop_limit")
    return False


def run(tasks):
    b = tasks.bot
    b.log("[FLOW] 免费招募：觉醒（如有）→常规普通/高级；每次重新确认免费")
    if not b.ensure_main() or not tasks.handle_main_extras():
        return False
    if not b.tap_template("main_generals", 3, "进入武将") or not b.wait_template("page_generals", 5):
        return False
    if not b.tap_template("recruit_entry", 5, "武将招募"):
        return False
    state = wait_state(tasks)
    if state == "result":
        if not tasks._exit_recruit_result_safely():
            return False
        state = wait_state(tasks)
    if state not in ("regular", "awaken"):
        b.fail_snapshot("recruit_page_unknown")
        return False
    frame = b.frame()
    has_awaken = state == "awaken" or any(b.vision.match(frame, name) for name in
                      ("recruit_tab_awaken", "recruit_tab_awaken_active"))
    if has_awaken:
        if not switch_tab(tasks, "awaken") or not claim_tab(tasks, "awaken"):
            return False
    if not switch_tab(tasks, "regular") or not claim_tab(tasks, "regular"):
        return False
    return tasks._return_recruit_to_main()
