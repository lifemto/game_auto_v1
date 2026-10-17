"""Account identity from profile ID; task history and state stay account-local."""
import datetime as dt
import json
import os
import re
import time
import uuid
from collections import Counter
from pathlib import Path

from local_ocr import read_text


def profile_identity(frame):
    readings = []
    account_id = None
    candidates = Counter()
    for scale in (2, 3, 4):
        text = "".join(read_text(frame, (20,1425,500,1480), scale=scale,
                                 context=True).get("text", "").split())
        readings.append(text)
        found = re.search(r"ID[.:：。·]*([0-9]{8,20})(?!\d)", text, re.I)
        if found:
            candidates[found.group(1)] += 1
            if candidates[found.group(1)] >= 2:
                account_id = found.group(1)
                break
    if not account_id:
        return {"id": None, "id_ocr": readings}
    name = "".join(read_text(frame, (240,985,460,1040), scale=4,
                             context=True).get("text", "").split())
    server_text = "".join(read_text(frame, (530,50,710,115), scale=3,
                                    context=True).get("text", "").split())
    server = re.search(r"#(\d{1,6})", server_text)
    return {"id": account_id, "name_ocr": name,
            "server": server.group(1) if server else None, "id_ocr": readings}


def _write_json(path, data):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    os.replace(temporary, path)


class AccountRecords:
    def __init__(self, root, identity):
        self.run_id = dt.datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
        self.identity = identity
        key = identity.get("id") or "unknown_" + self.run_id
        # WeChat keeps its existing directory for historical continuity.
        # Douyin may log into the same game ID but has independent daily work.
        if identity.get("platform") == "douyin":
            key += "_douyin"
        self.directory = Path(root) / "accounts" / key
        self.directory.mkdir(parents=True, exist_ok=True)
        self.day = dt.date.today().isoformat()
        self.latest_path = self.directory / (self.day + "_tasks.json")
        try:
            self.latest = json.loads(self.latest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.latest = {"account": identity, "date": self.day, "tasks": {}}
        _write_json(self.directory / "account.json", {**identity,
                    "last_seen": dt.datetime.now().astimezone().isoformat()})

    def record(self, task, status, evidence):
        base, _, attempt = task.partition("#")
        event = {"time": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                 "run_id": self.run_id, "task": base, "attempt": attempt or None,
                 "status": status, "evidence": str(evidence)}
        with (self.directory / "task_events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        previous = self.latest["tasks"].get(base, {})
        # Preserve the last confirmed completion even while a new check runs.
        last_complete = event["time"] if status == "complete" else previous.get("last_complete")
        self.latest["tasks"][base] = {**event, "last_complete": last_complete}
        self.latest["account"] = self.identity
        _write_json(self.latest_path, self.latest)
        labels = {"complete": "已完成", "waiting": "等待中", "skipped": "未安排",
                  "scheduled": "待执行",
                  "unknown": "未确认/失败", "start": "开始", "acting": "执行中",
                  "observing": "识别中", "verified": "已验证"}
        lines = [f"账号 ID：{self.identity.get('id') or '未识别（本次隔离记录）'}",
                 f"昵称OCR：{self.identity.get('name_ocr', '')}", f"日期：{self.day}", ""]
        for name, item in self.latest["tasks"].items():
            lines.append(f"{name}：{labels.get(item['status'], item['status'])}；"
                         f"{item['time']}；{item['evidence']}")
        (self.directory / (self.day + "_tasks.txt")).write_text(
            "\n".join(lines) + "\n", encoding="utf-8")


def identify_current_account(tasks):
    b = tasks.bot
    identity = None
    opened = tasks._open_profile()
    if not opened and b.ensure_main():
        b.log("[ACCOUNT] 首次主页截图未确认，恢复主界面后重新进入一次")
        opened = tasks._open_profile()
    if opened:
        for _ in range(3):
            frame = b.frame()
            if frame is None:
                continue
            data = profile_identity(frame)
            b.log(f"[ACCOUNT] 主页识别 ID={data.get('id')} 昵称OCR={data.get('name_ocr', '')!r}")
            # profile_identity already requires two OCR scales to agree on
            # the same ID in this fresh screenshot. Only recapture on an
            # unreadable frame; do not require a redundant second screenshot.
            if data.get("id"):
                identity = data
                break
            time.sleep(.25)
        if not tasks._tap_game_back("账号记录-主页返回") or not b.wait_main(5):
            b.fail_snapshot("account_profile_return_unknown")
            return False
    else:
        if not b.ensure_main():
            return False
    if identity is None:
        b.fail_snapshot("account_identity_unknown")
        identity = {"id": None, "name_ocr": "未识别"}
        b.log("[ACCOUNT] 未确认账号ID；隔离记录本次运行，不复用其他账号进度")
    identity["platform"] = getattr(b, "startup_platform", "auto")
    records = AccountRecords(b.output_dir, identity)
    b.account_records = records
    b.daily_state_path = records.directory / "daily_state.json"
    b.daily_state = b._load_daily_state()
    b.training_schedule_path = records.directory / "training_schedule.json"
    b.log(f"[ACCOUNT] 本次账号目录：{records.directory}")
    return True
