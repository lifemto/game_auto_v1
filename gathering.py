"""Resource dispatch and recall checks against the left march panel."""
import re
import time
import subprocess
from pathlib import Path

import cv2

from local_ocr import read_text


def compact(text):
    return re.sub(r"\s+", "", text)


def parse_march_count(text):
    """Parse the queue header, including Windows OCR's common '在' for 4."""
    text = compact(str(text)).replace("／", "/")
    match = re.search(r"(\d{1,2})/(\d{1,2})", text)
    if match:
        used, capacity = map(int, match.groups())
        # The adjacent counter/timer can be glued to the final digit. On the
        # saved Oct 15 screen, visible 1/5 became OCR 1/51. Keep scanning
        # smaller crops instead of accepting that impossible capacity.
        if 0 <= used <= capacity <= 10 and capacity:
            return used, capacity
    # On the compact blue header, Windows OCR occasionally recognizes the
    # final 4 as the Chinese character 在 (observed in live logs: 4/在).
    match = re.search(r"(\d{1,2})/([在A])", text)
    if match:
        return int(match.group(1)), 4
    return None


class GatheringTasks:
    def _gather_result_available(self, frame, level):
        b = self.bot
        # The result card also contains coordinates, storage and alliance
        # text. OCRing the entire card made the clearly free Oct 15 level-8
        # farm read as gibberish and made endswith('无') impossible.
        level_hit = b.vision.match(frame, "gather_level_8") if level == 8 else None
        free_hit = b.vision.match(frame, "gather_collector_none")
        if level_hit and free_hit:
            b.log(f"[GATHER] 搜索结果等级8 score={level_hit.score:.3f} "
                  f"采集者无 score={free_hit.score:.3f}")
            return True
        level_text = self._gather_text(frame, (265, 320, 390, 380), scale=3)
        collector_text = self._gather_text(frame, (140, 440, 590, 500), scale=3)
        match = re.search(r"等级(\d+)", level_text)
        b.log(f"[GATHER] 搜索结果重读 等级={level_text!r} "
              f"采集者={collector_text!r} 无模板={free_hit.score if free_hit else None}")
        return bool(match and int(match.group(1)) == level and
                    (free_hit or "采集者无" in collector_text))

    def _gather_text(self, frame, roi, scale=3):
        if frame is None:
            raise ValueError("采集画面缺失")
        return compact(read_text(frame, roi, scale=scale)["text"])

    def _march_state(self, fr=None):
        b = self.bot
        fr = b.frame() if fr is None else fr
        if fr is None:
            raise ValueError("行军画面缺失")
        normalized = cv2.resize(fr, (726, 1600))
        # Accounts and seasonal UI can place the left march panel at different
        # heights. Search the current screen and derive the row anchor from
        # OCR's actual word position rather than a fixed account layout.
        header = ""
        parsed_count = None
        first_row_top = 425
        for box in (
            (200, 385, 270, 425),
            (12, 380, 268, 425),
            (12, 430, 270, 520),
            (12, 365, 275, 445),
            (12, 475, 275, 575),
            (12, 325, 275, 395),
        ):
            for scale in (3, 4, 2):
                data = read_text(fr, box, scale=scale, context=True)
                candidate = compact(data.get("text", ""))
                parsed_count = parse_march_count(candidate)
                if parsed_count is not None:
                    header = candidate
                    for line in data.get("lines", []):
                        if parse_march_count(line.get("text", "")) and line.get("words"):
                            word_y = min(word["y"] for word in line["words"])
                            first_row_top = round(word_y * 1600 / fr.shape[0]) + 25
                            break
                    b.log(f"[VISION] 行军数量位于 y≈{first_row_top-25}："
                          f"{candidate} -> {parsed_count[0]}/{parsed_count[1]}")
                    break
                header = candidate or header
            if parsed_count is not None:
                break
        count_text = ""
        if parsed_count is None:
            # Capacity varies by account. Re-read only the count with OCR's
            # context padding, which helps the Windows OCR engine retain both
            # short digits around the slash (for example 6/6 or 5/6).
            try:
                count_text = compact(read_text(
                    fr, (185, 340, 275, 575), scale=6, context=True
                )["text"])
            except (RuntimeError, OSError, subprocess.TimeoutExpired, ValueError):
                count_text = ""
            parsed_count = parse_march_count(count_text)
            if parsed_count is not None:
                b.log(f"[VISION] 行军数量二次OCR确认：{count_text} -> {parsed_count[0]}/{parsed_count[1]}")
        if parsed_count is None:
            candidates = []
            # Exact visual fallbacks cover known UI variants only. OCR above
            # remains the primary path so accounts with other capacities work.
            count_templates = (
                ("gather_count_4of4.png", 4, 4),
                ("gather_count_3of5.png", 3, 5),
                ("gather_count_4of5.png", 4, 5),
                ("gather_count_5of5.png", 5, 5),
            )
            for filename, used_count, capacity_count in count_templates:
                template = cv2.imread(str(Path(__file__).parent / "templates" / filename))
                if template is None:
                    continue
                score = cv2.minMaxLoc(cv2.matchTemplate(normalized[380:425,210:268], template, cv2.TM_CCOEFF_NORMED))[1]
                candidates.append((score, used_count, capacity_count, filename))
            candidates.sort(reverse=True)
            if (not candidates or candidates[0][0] < .92
                    or (len(candidates) > 1
                        and candidates[0][0]-candidates[1][0] < .04)):
                raise ValueError("无法识别左侧行军数量：" + header)
            _, used, capacity, count_template = candidates[0]
            b.log(
                f"[VISION] 行军数量OCR未完整识别({header})；"
                f"模板确认{used}/{capacity} score={candidates[0][0]:.3f} ({count_template})"
            )
        else:
            used, capacity = parsed_count
        if not 0 <= used <= capacity <= 10 or capacity == 0:
            raise ValueError("行军数量无效：" + header)
        rows = []
        return_times = []
        self._last_march_return_seconds = None
        normalized = cv2.resize(fr, (726, 1600))
        for index in range(used):
            top = first_row_top + index*65
            patch = normalized[top:top+38, 65:212]
            state = "unknown"
            for name, label in (("gather_status", "采集中"), ("gather_returning", "返回中")):
                template = cv2.imread(str(Path(__file__).parent / "templates" / (name+".png")))
                score = cv2.minMaxLoc(cv2.matchTemplate(patch, template, cv2.TM_CCOEFF_NORMED))[1]
                if score >= .85:
                    state = label
                    break
            if state == "unknown":
                template = cv2.imread(str(Path(__file__).parent / "templates/gather_marching_arrow.png"))
                buttons = normalized[top:top+58, 208:264]
                score = cv2.minMaxLoc(cv2.matchTemplate(buttons, template, cv2.TM_CCOEFF_NORMED))[1]
                if score >= .85:
                    state = "行军中"
                else:
                    # Keep the visible queue count even if this row's label
                    # is covered by an icon or animation. Unknown rows are
                    # never selected for recall.
                    state = "未知"
                    b.log(f"[VISION] 第{index+1}条行军状态未识别，保留名额计数并跳过召回")
            if state == "返回中":
                try:
                    timer_text = self._gather_text(
                        fr, (65, top, 212, top+65), scale=4
                    )
                    # OCR sometimes turns the colon into punctuation or
                    # drops the first zero (e.g. "№ 0 . 10 : 40"). Read the
                    # three numeric groups from the whole row instead.
                    parts = [int(part) for part in re.findall(r"\d+", timer_text)]
                    if len(parts) == 3:
                        hours, minutes, seconds = parts
                        if hours <= 99 and minutes < 60 and seconds < 60:
                            return_times.append(hours*3600 + minutes*60 + seconds)
                except (RuntimeError, OSError, subprocess.TimeoutExpired, ValueError):
                    pass
            rows.append((state, (top+9)*fr.shape[0]/1600))
        if return_times:
            self._last_march_return_seconds = min(return_times)
            remaining = self._last_march_return_seconds
            b.log(
                "[VISION] 最早返队倒计时="
                f"{remaining//3600:02d}:{(remaining%3600)//60:02d}:{remaining%60:02d}"
            )
        return fr, used, capacity, rows

    def _gather_purchase_close(self):
        fr = self.bot.frame()
        text = self._gather_text(fr, (15, 635, 705, 930))
        if "出征队列" not in text or "前往购买" not in text:
            return False
        self.bot.actions.tap_frame(round(fr.shape[1]*.925), round(fr.shape[0]*.422), fr.shape, "关闭队列购买弹窗")
        self.bot.log("[STOP] 出征队列购买弹窗已出现，停止派遣")
        return True

    def gather_recall(self, max_recall=None, only_if_full=False):
        """Recall gathering marches; optionally stop once a march slot opens."""
        b = self.bot
        if not self._ensure_world():
            return False
        recalled = 0
        try:
            for _ in range(10):
                fr, used, capacity, rows = self._march_state()
                if only_if_full and used < capacity:
                    b.log(f"[BEACON] 行军已变为{used}/{capacity}，有空位，取消本次召回")
                    return True
                if len(rows) != used:
                    raise ValueError("行军列表未完整识别，停止召回")
                gathering = [row for row in rows if "采集中" in row[0]]
                if not gathering:
                    b.log(f"[OK] 采集回收完成，已召回 {recalled} 队；返回中的队列仍占用行军名额")
                    return True
                _, y = gathering[0]
                selected_index = rows.index(gathering[0])
                if not b.actions.tap_frame(round(fr.shape[1]*.325), round(y+fr.shape[0]*.015), fr.shape, "召回采集中队列"):
                    return False
                time.sleep(.4)
                modal = b.frame()
                if modal is None:
                    raise ValueError("召回确认画面缺失")
                template = cv2.imread(str(Path(__file__).parent / "templates/gather_recall_prompt.png"))
                score = cv2.minMaxLoc(cv2.matchTemplate(cv2.resize(modal, (726,1600))[680:840,150:580], template, cv2.TM_CCOEFF_NORMED))[1]
                if score < .9:
                    raise ValueError("召回确认弹窗未识别；停止，避免重复点击")
                if not b.actions.tap_frame(round(modal.shape[1]*.73), round(modal.shape[0]*.617), modal.shape, "确认召回"):
                    return False
                confirmed = False
                for attempt in range(3):
                    time.sleep(.4)
                    _, after, _, updated = self._march_state()
                    returning = selected_index < len(updated) and updated[selected_index][0] == "返回中"
                    selected_unknown = (selected_index < len(updated)
                                        and updated[selected_index][0] == "未知")
                    collecting_decreased = sum(
                        "采集中" in row[0] for row in updated
                    ) < len(gathering)
                    if (len(updated) == after and collecting_decreased
                            and (returning or selected_unknown or after < used)):
                        confirmed = True
                        break
                if not confirmed:
                    raise ValueError("召回后队列状态未确认，停止重复操作")
                recalled += 1
                if max_recall is not None and recalled >= max(1, int(max_recall)):
                    b.log(f"[OK] 已召回{recalled}支采集队伍，等待行军名额释放")
                    return True
            raise ValueError("召回次数达到上限")
        except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            b.log(f"[STOP] {exc}")
            b.fail_snapshot("gather_recall_unconfirmed")
            return False
