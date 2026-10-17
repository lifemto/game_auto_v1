"""Find warehouse stamina by row label; never substitute the item-use button."""
import re
import time

import cv2

from local_ocr import read_text, text_box


def warehouse_state(frame):
    if frame is None:
        return {"state": "unknown", "text": "no frame"}
    h, w = frame.shape[:2]
    label = None
    for scale in (2, 3):
        data = read_text(frame, (135,555,510,1370), scale=scale)
        label = text_box(data, "前往仓库")
        if label:
            break
    if label is None:
        return {"state": "unknown", "text": "前往仓库行未定位"}
    top = round(label[1]*1600/h)
    box = (490, max(0,top-8), 698, min(1600,top+108))
    patch = frame[round(box[1]*h/1600):round(box[3]*h/1600),
                  round(box[0]*w/726):round(box[2]*w/726)]
    hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
    green = cv2.inRange(hsv, (35,90,70), (90,255,255))
    ratio = cv2.countNonZero(green)/green.size
    texts = []
    for scale, mode in ((3,None),(2,"white"),(4,None)):
        text = "".join(read_text(frame, box, scale=scale, mode=mode,
                                 context=True).get("text", "").split())
        texts.append(text)
        if any(token in text for token in ("已领取", "已领完")):
            return {"state":"waiting", "text":text, "row":box}
        if re.search(r"\d+时\d+分|\d+小时|\d+[：:]\d{2}|\d+分",text):
            return {"state":"waiting", "text":text, "row":box}
        if "领取" in text and "使用" not in text and ratio > .15:
            return {"state":"ready", "text":text, "row":box,
                    "center":(round(594*w/726),round((top+50)*h/1600))}
    return {"state":"unknown", "text":" | ".join(texts), "row":box,
            "green_ratio":round(ratio,3)}


def process(tasks):
    b = tasks.bot
    previous = None
    current = None
    for _ in range(4):
        frame = b.frame()
        current = warehouse_state(frame)
        b.log(f"[STAMINA WAREHOUSE] {current}")
        if current["state"] != "unknown" and previous and current["state"] == previous["state"]:
            break
        previous = current
        time.sleep(.25)
    else:
        b.fail_snapshot("stamina_warehouse_state_unknown")
        return False
    if current["state"] == "waiting":
        b.task_waiting_reason = "仓库体力当前不可领：" + current["text"]
        b.log("[WAITING] " + b.task_waiting_reason)
        return True
    # Re-read the named row immediately before the only claim action.
    fresh = b.frame()
    checked = warehouse_state(fresh)
    if checked["state"] != "ready":
        b.fail_snapshot("stamina_warehouse_changed_before_claim")
        return False
    if not b.actions.tap_frame(*checked["center"],fresh.shape,"仓库体力-已确认领取"):
        return False
    learner = getattr(b,"learning",None)
    ticket = learner.pending if learner else None
    for _ in range(5):
        time.sleep(.4)
        frame = b.frame()
        if frame is None:
            continue
        reward = b.vision.match(frame,"reward_continue")
        if reward:
            b.actions.tap_match(reward,frame.shape,"仓库体力奖励-继续")
            continue
        if tasks._is_profile_page(frame) and not b.vision.match(frame,"page_stamina_more"):
            if not b.tap_template("profile_stamina_plus",2,"仓库体力-重开验证"):
                return False
            b.wait_template("page_stamina_more",3)
            continue
        after = warehouse_state(frame)
        if after["state"] == "waiting":
            if learner:
                learner.finish(ticket,"仓库领取后按钮转为倒计时/已领取："+after["text"])
            b.task_completion_evidence = "仓库体力已领取，后置状态："+after["text"]
            b.log("[OK] " + b.task_completion_evidence)
            return True
    b.fail_snapshot("stamina_warehouse_claim_unconfirmed")
    if learner:
        learner.finish(ticket,"仓库体力领取未确认",success=False,frame=b.frame())
    return False
