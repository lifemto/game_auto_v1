# -*- coding: utf-8 -*-
"""
三国：冰河时代 - 田忌赛马 Android ADB V3.3 出牌CD感知版
======================================

本程序按用户实际录像制作，不使用 OCR：
- 自动找“小游戏”窗口
- 大厅自动点“开始匹配”
- 三轮按：下等马 -> 中等马 -> 上等马
- 自动识别底部手牌数字 1~10
- 金色/道具牌仍按牌面数字参与组合
- 等手牌接近/达到上限后，自动找高收益牌型
- 自动选择牌并点击“出牌”
- 三轮共享同一实际牌堆/手牌状态：程序每次从当前画面重新读取，不会把第二、三轮当成新牌堆
- 比赛结束自动继续并尝试领取奖励

紧急停止：
停止：Ctrl+C。

依赖：
pip install numpy opencv-python dxcam
"""

import base64
import io
import json
import math
import os
import random
import time
import threading
import queue
import subprocess
import struct
import statistics
import traceback
import atexit
import zipfile
import sys
import ctypes
import shutil
from pathlib import Path
from collections import Counter, defaultdict, deque
from itertools import combinations
from functools import lru_cache

import cv2

try:
    import av
    AV_AVAILABLE = True
except Exception:
    av = None
    AV_AVAILABLE = False
import numpy as np
import re

try:
    import dxcam
    DXCAM_AVAILABLE = True
except Exception:
    dxcam = None
    DXCAM_AVAILABLE = False


SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "horse_race_config_v3.json"
LEARNING_PATH = SCRIPT_DIR / "horse_race_learning.json"
USER_AVATAR_OVERRIDE_PATH = SCRIPT_DIR / "horse_race_user_avatar.png"
DEBUG_DIR = SCRIPT_DIR / "horse_race_debug"
DEBUG_DIR.mkdir(exist_ok=True)

# ============================================================
# 运行诊断：控制台、结构化事件及关键画面保存在本次会话目录。
# ============================================================

SESSION_ID = None
LOG_ROOT = None
SESSION_DIR = None
SESSION_SCREENSHOT_DIR = None
SESSION_LOG_PATH = None
SESSION_EVENTS_PATH = None
SESSION_BUNDLE_PATH = None

_LOG_LOCK = threading.RLock()
_SESSION_BUNDLE_DONE = True
_DEBUG_SAVED_AT = {}
RACE_REPORT = None


def _session_paths():
    """Create one diagnostic folder per run on first use."""
    global SESSION_ID, SESSION_DIR, SESSION_LOG_PATH, SESSION_EVENTS_PATH
    if SESSION_DIR is None:
        SESSION_ID = time.strftime("%Y%m%d_%H%M%S") + f"_{os.getpid()}"
        SESSION_DIR = DEBUG_DIR / SESSION_ID
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        SESSION_LOG_PATH = SESSION_DIR / "console.log"
        SESSION_EVENTS_PATH = SESSION_DIR / "events.jsonl"
    return SESSION_DIR


def _write_race_report():
    if RACE_REPORT is None:
        return
    with _LOG_LOCK:
        folder = _session_paths()
        path = folder / f"match_{RACE_REPORT['match_number']:03d}.json"
        path.write_text(json.dumps(RACE_REPORT, ensure_ascii=False, indent=2,
                                   default=str), encoding="utf-8")


def start_race_report(match_number):
    global RACE_REPORT
    RACE_REPORT = {
        "match_number": int(match_number),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "status": "matching",
        "rounds": {str(i): {"horse": None, "plays": [], "rank_changes": [],
                            "result_rank": None} for i in range(1, 4)},
        "final_rank": None,
    }
    _write_race_report()


def race_report_horse(round_index, horse, hand, metrics, availability):
    if RACE_REPORT is None:
        return
    row = RACE_REPORT["rounds"][str(round_index)]
    row["horse"] = horse
    row["initial_hand"] = list(hand)
    row["horse_availability"] = {
        name: bool(availability.get(name, {}).get("available", False))
        for name in HORSE_CAPACITY
    }
    row["available_horses"] = [name for name, data in availability.items()
                               if isinstance(data, dict) and data.get("available")]
    row["horse_estimates"] = {name: {
        "expected": data.get("expected"),
        "utility": data.get("utility"),
        "final_utility": data.get("final_utility"),
    } for name, data in metrics.items()}
    RACE_REPORT["status"] = "playing"
    _write_race_report()


def race_report_rank(round_index, rank, score, elapsed):
    if RACE_REPORT is None or round_index not in (1, 2, 3):
        return
    row = RACE_REPORT["rounds"][str(round_index)]
    row["rank_changes"].append({"elapsed_s": round(elapsed, 2),
                                "rank": int(rank), "score": round(float(score), 3)})
    if row["plays"]:
        last = row["plays"][-1]
        if elapsed - last["elapsed_s"] <= 8.0:
            last["rank_after_nearby"] = int(rank)
    _write_race_report()


def race_report_play(round_index, play, hand, rank, elapsed):
    if RACE_REPORT is None or round_index not in (1, 2, 3):
        return
    RACE_REPORT["rounds"][str(round_index)]["plays"].append({
        "elapsed_s": round(elapsed, 2), "name": play.get("name"),
        "values": list(play.get("values", [])),
        "multiplier": float(play.get("multiplier", 1.0)),
        "hand_before": list(hand), "rank_before": rank,
        "strategy_score": play.get("ai_score"),
    })
    _write_race_report()


def race_report_round_result(round_index, rank, rank_score, live_rank):
    if RACE_REPORT is None or round_index not in (1, 2, 3):
        return
    row = RACE_REPORT["rounds"][str(round_index)]
    row["result_rank"] = rank
    row["result_rank_score"] = round(float(rank_score), 3)
    row["last_live_rank"] = live_rank
    if round_index == 3:
        RACE_REPORT["final_rank"] = rank
    _write_race_report()


def finish_race_report():
    if RACE_REPORT is None:
        return
    RACE_REPORT["status"] = "reward_confirmed"
    RACE_REPORT["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    _write_race_report()
    lines = [f"场次 {RACE_REPORT['match_number']}，最终名次："
             f"{RACE_REPORT['final_rank'] or '未识别'}"]
    for number, row in RACE_REPORT["rounds"].items():
        plays = row["plays"]
        names = ", ".join(f"{p['name']}({p['multiplier']:.1f}x)" for p in plays)
        lines.append(f"第{number}轮：马={row['horse'] or '未识别'}，"
                     f"出牌={names or '无已确认出牌'}，"
                     f"轮末名次={row['result_rank'] or row.get('last_live_rank') or '未识别'}")
        estimates = row.get("horse_estimates", {})
        if estimates and row.get("horse") in estimates:
            chosen = estimates[row["horse"]].get("final_utility")
            options = [(name, data.get("final_utility")) for name, data in estimates.items()
                       if data.get("final_utility") is not None]
            if chosen is not None and options:
                best_name, best_score = max(options, key=lambda item: item[1])
                lines.append(f"  选马评分：本轮 {float(chosen):.2f}，"
                             f"候选最高 {best_name} {float(best_score):.2f}。")
        if plays:
            average = sum(p["multiplier"] for p in plays) / len(plays)
            high = sum(p["multiplier"] >= 3.0 for p in plays)
            lines.append(f"  出牌统计：{len(plays)}次，平均倍率 {average:.2f}x，"
                         f"其中至少3倍 {high}次。")
        changes = row.get("rank_changes", [])
        if changes:
            lines.append("  名次轨迹：" + " → ".join(
                f"{entry['elapsed_s']:.1f}s第{entry['rank']}名" for entry in changes))
    lines.append("名次变化与附近出牌仅作时间关联，不能单凭一场证明因果；"
                 "跨场比较同类手牌和马匹选择后再调整策略权重。")
    (_session_paths() / f"match_{RACE_REPORT['match_number']:03d}_analysis.txt").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")


REFERENCE_W = 496.0

REFERENCE_H = 964.0

# ============================================================
# Android ADB
# ============================================================

ADB_PATH = os.environ.get(
    "ADB_PATH",
    "adb",
).strip() or "adb"

ADB_SERIAL = os.environ.get(
    "ADB_SERIAL",
    "",
).strip()

ADB_SCREENSHOT_MODE = os.environ.get(
    "ADB_SCREENSHOT_MODE",
    "auto",
).strip().lower()

ADB_SCREENSHOT_TIMEOUT = 6.0
ADB_BENCHMARK_ROUNDS = 2

# RAW在部分Android 16设备上测速很快，但连续screencap可能偶发返回空/短数据。
# 如果PNG只比RAW慢不超过60%，优先选PNG稳定模式。
ADB_PNG_STABILITY_RATIO = 1.60

# 当前截图模式运行中失败时，立即尝试另一种模式。
ADB_RUNTIME_MODE_FAILOVER = True

# ------------------------------------------------------------
# V1.5 页面状态机
# ------------------------------------------------------------

# 选完马以后至少看到3张牌，才确认进入比赛。
RACE_ENTER_MIN_CARDS = 3

# 点击开始匹配后，如果长期没有进入选马页，才允许重新检查大厅。
WAIT_HORSE_RECOVERY_SECONDS = 14.0

# 点击马以后，如果长期没有识别到比赛手牌，打印诊断并允许恢复。
WAIT_RACE_DIAGNOSTIC_SECONDS = 10.0

# ------------------------------------------------------------
# V1.6 ADB截图稳定性
# ------------------------------------------------------------

# Android screencap 不是桌面截图，连续无间隔调用会把部分 Android 16
# 设备的 screencap/ADB 管道打到返回空数据。
# V1.6 只允许后台截图线程访问 screencap。
ADB_CAPTURE_COOLDOWN = 0.28
ADB_CAPTURE_FAILURE_BACKOFF = 0.80
ADB_CAPTURE_MAX_STALE_AGE = 12.0
ADB_CAPTURE_WAIT_TIMEOUT = 5.0

# ------------------------------------------------------------
# V1.6 Android比赛手牌自适应
# ------------------------------------------------------------

# 原Windows录像模板的数字Y≈69.4%。
# Android宿主裁剪略有偏移，因此在附近多个Y位置自动寻找最匹配数字行。
ANDROID_HAND_DIGIT_Y_RATIOS = (
    0.728,
)

# 旧卡牌边缘检测失败时，使用更宽的纵向区域和更宽松阈值重找。
ANDROID_HAND_EDGE_Y0 = 0.48
ANDROID_HAND_EDGE_Y1 = 0.97

# wait_race一直0张时，每8秒保存一张调试截图，方便继续校准。
WAIT_RACE_DEBUG_SAVE_INTERVAL = 8.0

# ------------------------------------------------------------
# V1.9 scrcpy + DXGI 实时视觉
# ------------------------------------------------------------

# V1.7/V1.8 的 screenrecord 裸 H264 路径在当前 RMX3850 / Android 16
# 上不可用。V1.9 改成：
#
#   scrcpy-server -> scrcpy Windows 窗口 -> DXGI(Desktop Duplication)
#   -> 最新画面 -> OpenCV -> ADB持久shell点击
#
# scrcpy 负责手机端低延迟视频；dxcam只抓 scrcpy 的客户端区域。
# 不再调用 screenrecord，也不在比赛中调用 adb screencap。

SCRCPY_REALTIME_ENABLED = (
    os.environ.get(
        "HORSE_SCRCPY_REALTIME",
        "1",
    ).strip().lower()
    not in ("0", "false", "no")
)

SCRCPY_PATH = os.environ.get(
    "SCRCPY_PATH",
    r"C:\scrcpy\scrcpy.exe",
).strip()

SCRCPY_WINDOW_TITLE_BASE = os.environ.get(
    "HORSE_SCRCPY_TITLE",
    "HorseRaceScrcpy",
).strip() or "HorseRaceScrcpy"

# V2.2：每次运行使用唯一标题，绝不误锁上一次残留的scrcpy窗口。
SCRCPY_WINDOW_TITLE = (
    f"{SCRCPY_WINDOW_TITLE_BASE}-"
    f"{os.getpid()}-"
    f"{int(time.time() * 1000) % 1000000}"
)

SCRCPY_MAX_SIZE = int(
    os.environ.get(
        "HORSE_SCRCPY_MAX_SIZE",
        "1080",
    )
)

SCRCPY_MAX_FPS = int(
    os.environ.get(
        "HORSE_SCRCPY_MAX_FPS",
        "60",
    )
)

SCRCPY_VIDEO_BITRATE = os.environ.get(
    "HORSE_SCRCPY_BITRATE",
    "8M",
).strip() or "8M"

# DXGI采集轮询目标。scrcpy本身最多60fps；视觉线程只保留最新帧。
DXGI_CAPTURE_FPS = float(
    os.environ.get(
        "HORSE_DXGI_FPS",
        "60",
    )
)

# scrcpy窗口启动与首帧等待。
SCRCPY_START_TIMEOUT = 12.0
SCRCPY_FRAME_TIMEOUT = 2.0
SCRCPY_FRAME_MAX_AGE = 0.40
SCRCPY_GEOMETRY_REFRESH = 0.75
SCRCPY_MIN_CLIENT_WIDTH = 420

# scrcpy窗口必须保持可见。脚本自动启动的窗口会使用 always-on-top。
SCRCPY_AUTO_LAUNCH = (
    os.environ.get(
        "HORSE_SCRCPY_AUTO_LAUNCH",
        "1",
    ).strip().lower()
    not in ("0", "false", "no")
)

# 旧变量保留给后面的通用快速模式参数，但不再表示screenrecord。
ADB_FAST_STREAM_ENABLED = False
ADB_FAST_STREAM_REQUIRED = False
ADB_STREAM_BITRATE = 0
ADB_STREAM_SIZE = "disabled"
ADB_STREAM_TIME_LIMIT = 0
FAST_STREAM_FRAME_WAIT = 0.80
FAST_STREAM_FPS_LOG_INTERVAL = 5.0
DIAGNOSTIC_HEARTBEAT_SECONDS = 3.0
HAND_RAW_EVENT_MIN_INTERVAL = 0.12
PLAY_CONFIRM_TIMEOUT = 0.52
PLAY_CONFIRM_RETRY_TIMEOUT = 0.38



def load_config():
    """
    V1.9 默认配置。
    原V1.8配置加载函数在重构实时视觉模块时被覆盖，
    这里恢复为内置字典，避免启动阶段依赖外部json。
    """
    return {
        # 视觉
        "capture_fps": 30.0,
        "hand_worker_fps": 30.0,
        "rank_worker_fps": 10.0,
        "stable_hand_max_age": 0.32,
        "hand_consensus_window": 3,
        "hand_consensus_required": 2,

        # 出牌速度
        "min_seconds_between_plays": 0.22,
        "urgent_play_interval": 0.06,
        "card_click_wait": 0.010,
        "clear_card_click_wait": 0.004,
        "valid_button_poll_seconds": 0.60,
        "valid_button_poll_interval": 0.035,
        "out_button_poll_interval": 0.035,
        "out_button_cooldown_wait_seconds": 4.60,
        "out_button_ready_stable_frames": 2,
        "after_play_wait": 0.025,
        "post_play_hand_change_timeout": 0.52,
        "post_play_hand_change_poll": 0.010,
        "vision_suppress_after_action": 0.12,
        "urgent_loop_sleep": 0.004,
        "normal_loop_sleep": 0.010,

        # V2.5：历史状态机仍直接读取这些键；V1.9重构时漏进默认配置。
        "scan_interval": 0.010,
        "aggressive_after_seconds": 8.0,
        "emergency_after_seconds": 14.0,

        # 学习
        "learning_enabled": 1,
        "learning_history_limit": 300,
        "learning_confidence_samples": 14,
        "learning_horse_weight": 0.72,
        "learning_play_weight": 460.0,
        "learning_exploration_rate": 0.0,

        # 赛马
        "horse_selection_v2_enabled": 1,
        "horse_simulations": 800,
        "horse_selection_mode": "estimate",
        "horse_fallback_order": [
            "lower",
            "middle",
            "upper",
        ],

        # 牌库
        "shared_deck_enabled": 1,
        "deck_state_log": 0,
        "refill_checkpoint_count": 3,
        "refill_max_cards": 3,

        # 自动化
        "auto_start_match": 1,
        "auto_receive_reward": 1,
        "matches_to_play": 1,

        # 调试
        "save_debug": 0,
        "async_vision_enabled": 1,
        "realtime_rank_enabled": 1,
        "probability_enabled": 1,
        "probability_log": 0,

        # 识别阈值
        "card_avg_confidence": 0.50,
        "card_min_confidence": 0.38,
        "selection_card_min_confidence": 0.47,

        # 保留位
        "learning_result_avatar_threshold": 0.7,
        "hand_cache_enabled": 1,
        "hand_cache_diff_threshold": 2.0,
        "deck_state_enabled": 1,
    }

CFG = load_config()


# ============================================================
# V1.9 FIX2 missing embedded template compatibility
# ============================================================
# 原V1.9合并时缺失的内置模板变量保护。
# 如果后续有旧版本模板，可直接替换这里的数据。

if "DIGIT_SAMPLES_B64" not in globals():
    DIGIT_SAMPLES_B64 = {
        1: [
            "iVBORw0KGgoAAAANSUhEUgAAACYAAAAfCAIAAAAEKiicAAAI/klEQVRIDXXBa4xc910G4Pf9/c+ZmZ09tjf27iROvBtbwTWtKARMFwkhj2f2G6hFVDSuuAQIfA4XUVqIEIhQSpOUClSJqF8bJ1tatWhL06SEVOmytktU0V3Teu0myCTG8a7nzF7mcmbO5f8yO3RDaNrn4WNPvhSGofeZ5A1g5vM87xadvMhv3rp+89Z/AZLl3IcxM8MPoTGMScLb8M8+8ezBgwd9dtjnVfNd61+7ceP1k0e/PVnBZPWN6sTNIHBghj0e32PYY/gBCBAifjg+8vGlqakp5NPDQWnQ2ezc+rdSOfy5d2/MHJmYOrhZnbiJEaYYofC/ROwx/ACGPYSIt/HeA+DHPvUiDComu92s075m3a+ePn36Z9/5nWo5q3C9zCsSDGUvDzmAQAHm2OOxR6AISBgj9hAgvp8kAOJjT740HA7lq7u7g/7Oq3NTa2fOnLn34PP5MJ7glQOlVwASFUmQAwgUYIYReuwpJAGQ8BaU8P0Ignse//S/9Pt9ynV2u+nu+jtnVur1+vGJxTTZcH63UkosdBhMpUkSVO+ywpR3ZT2rVFDOIKHIkefKC0gYEQSQBhCAJJJ4EwmAj3/6hSRJ6NnZ7aS7V94181K9Xj9RfTrtb7gwKU/mhKF9x87W9u3NcNBHGBbRIX/gkFUmRcoII0hCewADIAHCiABiHwFwz8eefGEwHDixs72T7lz5sSMr9bNnj5cX82TDrBwE1X4vf+3K5I3Xb/zntW6/V1QjTN8VTE9P/fjPFOaMQBgGYVjy3kuCCJAChRER/x8B8KN//89pmjqhs7Uz2P7Ou49caDQa95YWs/6tQd8liWvHvdVL/O53+72dvMhUqmBqmocPl37h3EQpLNEQhqUR770kyACYaCIAT7yVJAD8i089VxSFiTtb20n72z9x5MLCwsJc+HRn67VXr+SvrKN1e9i+hU5H8pCHgLCCmWl+4MF3VcplVxqEJYUlJ6QQzAcEAVAYoQz/R2Pgn//ds957B9tqbyXt/7j/yKWFhYXZ4Px2fP3l5cHL/8q45VEgDCGPES/QUKvxV35rvlqtulInKGVBaLABBFeEBLHPecM+SQAk8U//9jkIRm7F7X58+f6ZSwvN5mx4frt1/RsvDr75NWxvKx+iXAYJEl6gQ62GX31ofnJyMih3XZi6gHSpJPMhQBNMGDFveJMgaISPfPJ5ko6MW3E/vnx/7eLCwsJs+Jmd+Po3X8zWvm69btbbZRDQDOYowgI/MxM88Os/GUVRUO5aOHSO5lJJVAjQPJwwYt6wTxAESXzkk8+TNLLdivvx5ftrF5vN5lzpqf7uf7/y7/76mvV6w5vX814f8GY0QTBfq0UfePBUFEVBuevC1BzMpZKoEKB5OGHEvOFNgqAR/snfPGdGg7XjuBev/dSd32g0GveWzg/7tzq3DvQ3D3U7g5dX3njttZ7PAsq896BqtTvOPXQiiqKg3HVhag7mUklUCNA8nDBi3rBPEgBJ/ONPfMXMHBm34n778um7Lp09e/be8vk02XDpoTCf2tlOvrp0Y3Ut4dAcnfcArFabPvfbd0dRFJR7Lhw6B7pUEhUCdIJ5QDA5vIUkAPzIE8+amaPFcSuJL5+++2K9Xj9eeSZLNkpwJQVxq/vCP/W/9S34Hp2cvINKtVrt3O8ciaIoKHddmJqDuVQSFQI0D/MCYD7A2/AjT3zZRsi4FSfx6k8fu1g/Uz9eOZ8NNkNZUNjGG93lr5SvXk0GO4R38gStVjt87qF7oigKyj0XDs3BXCoJCilSME8I5h1GSIyRlMQPP/ElGwHjVitpr73n2IV6vX584jNZshF65wrbvDVcXppeX98cdg1ygmC+VovO/ebJKIqCcs+FQ3Mwl0qCDwDSG70BMh8AIIkxkgD44ceXQDoybsXJ1ur8sYv1+pkT1aeywWZQmGXu9sZweWn66tXNQccg5+XpdOed0QO/8SNRFAWVnguG5mAulQQfAqQ3ekqgdwCMBAHwe/7o8X8kaUDcigdba/NzF+r1+n2TT+XD25Y5pnZ7I11eml5f3xx2Td68vAU4evTQLz94PIqioNx1YWoOZqkk+BJAeMJTEn0AgPuMBMkPPfZFko6MW/Fga21+7sLZs2fvi84Xw03LnAa8vZEuL01fvbqZdMznEORCu+eeqff/2lwURUG568LUHMxSSfAlgPCEpyT6AAD3mRlHPvTYF0ka2I7jZGttfnal0WicPPC0z1pMrejj9sZweWl6fX1z0GGRw0ulSjA3d+R9H7w7iqKg3HVhag5mqST4EkB4wlMSfQCA+8yMIP/w418g6WjtOE621uZnVxqNxsmDzyCPNUDR1+2NdHlp+urVzWSXRYZCvlQJT5yYee8Dd0VRFJT7Lhiag1kqCSphxJs85EU5ADQSBGkkCP7BX3/eaI7WjuNka21+dqXRaLzj0DPI2z5R0VdrM1teml5f3xx0mGcqvCoT4X333fm+B45Gk5NBkJjLzMFcqj0GUII8JGGMYwBIAuDv/9XnzMzR2nGcbK3Nz640m813HHoGeewT5j21NtPlL01du9YedJmn8tLERHjiRO2XPnisWp10LqGl5mQulSAZAAnykIQx7sEYAfD3PvoPZuZo7ThO2qvzcxcajcapqUXkbd8P866LW71LL+rVV32RMs8RmB2KSocP3/Hzv3isWq2SAzAzB9oQgCQA2ocxjgHQGH/3Lz9LMjDXjuN+e3V+dqXZbJ6aWkTe9v2AaSlL7eYrM61WnA0q8i5wk9VSrVqtHp1rT0xMgAMgMydaCkASAO3DGMc0BoAPP7pIs4DWjuN+e3V+dqXZbJ6aWkTeRpaF5oNSyW//aK/XG/RCs3IYHGBxIM9z57YrlQqUSznpYUOMSQIowXsPgCQAjmGMDz+6SDIw147jfnt1fnal2WyemlpE3rYiC52nC7PWyaIoBv1SGEyE7kCRTnS73XKlV6lUqEwoAA8OQBJ7BMrLe08SAGkk3sSHH100M0drx3HSXn3P7Eqz2Tw19VnksfNZ6DwszFonNZJHUiBf9v6Ame0OXgcQls0FBL1XH8BkeZKAMvqhvPfKjWNmxjEA/wPKPgnDeySvWwAAAABJRU5ErkJggg==",
            "iVBORw0KGgoAAAANSUhEUgAAACYAAAAfCAIAAAAEKiicAAAIbUlEQVRIDXXBa3NU9R0H8O/39z9nswkoCCR7zWazCTpssqkl4NgyRbz1HfRJpw+kDxCLtKKttTO+hspMp1PbEe2MLwJjh+nUFqojCbXWtUYh4Zbs5rLZJdnbOed/ft1sVcIAnw9/cvK3xgig1gaB74VhqGo7AJKCb4hANwGgdhDfUkBxXyHRpYACCvo88fqZ5gPqqR8p/XvX8vSu3Tsy2fLOh3Y62gMVQkGLLlUFfdKCIKmq6FAoFF1EiA7xlJZQqAKgBACMqqiSIDyeeP3MMtdLlaVRd/WZdJhI9CcHrzsdiAoMoKBFF0nVdghPQ0WXiJDEN4gQgEobtIBSFR30ARhVUSWU9PnCb96pblxer1e+P7j+o/3egzv6nOg/gsC6zoaIDxChQRdJSBv0FaoAAYIgSHxN0WFCGhUQCsEmBaAkQBAWlsdefWulUnxwR++Te5vP5kpuhE70H0EQiFSN8Y04UAddqgppUzySoJLoUMVtSgCONaIEoBQAqgpASQVBWli+8Mqf1qtzifieHzwyd2jwA9cVJ/KZKgTWUKk96u1oNBqu60qHWxenyQ5EVBX3ImEIKDpIdKgCChCAQkNRvvirt25VriTj/UfyVw5lz4mo434uAgERaqsua2VTqYQko1H0PRj2bgsjEUYjDqC4FwlDqoIA0RGGii4FFFADnvj1mcrKXDI58HTh5uHhv9vQA2ZUQ786XFsxCzfWr33lV6v1er3d1+cOpJjKbEskdvf3C+6m6FCxipBQQNHBEFAqCAVAWJ547e2V5SupVPyZiYXDufM28ILgQ2t1YTZ+/bK9emX92ldhux2EYShG4ik8nN+5b196II67UdFhJVCGgBKKDgYARJWqJASWP3vtnZXly+l04tmJ0uHsP33fv3llbnZ24eZspHTD1iqhX98OENIgEU9qfnzPeCE7kFrDfSmhuC1EFwESpM/jr55ZXZ3LZJLPFsqHMhe8tvev83MffLCwtqCNmgaeG8FDIMRZJxFPhPnxWKEwPDBYwn1QQdwbCdLn8Zf/vFb7bzabenps6VD6Q98PLp3/dGpqbW3R+k0wjETwgO/7bo9PIpHA2Fh8fGI4li7hPgjFXZTYRAAej596t1r7dDiXeiq//L3Eh2EYFqc/O3duZeGK9RoQdY3tC8PQOD7JRAL5fLwwkYulS7gfhriLAkoACvp8/qV3q7c+yY2knh5bfCz2t1azWbp6Y/aLavladGnBVld9f8ON9rhtzyOQiJuxseT4xHAsXcJ9KaC4k1KVABQIeOwX71bXL+VyyR9OLD6e/OvGxkZQr9dq9Wppz5efr39RLFdLjESM1wbJRNzJ70sWJnKxwRLuSwHFnZSqVGwKeOylt2u1L7LDqacK1w8O/sX3vAfCVqPRaFYzn36yOvPR5fJVqEJDIZGIu2P51PhEbmCwDIBK3AtBBUD8H0klQACqCHjs1B+ra9ezQ+knHp0/mH0/DPxdftVq2KxlL02XPzr/VXmOnqciIJGIO/l96cJErn9wGYCEgruQAhCkEh0iQhIkOggVy2On3qxWbgxl0k9OXnt877nAa+32rlFMrZK5NL368flri5el2bSOAxLxuDOWT41PjAykywBEiTsIAFIIglR2CUUEICigWlg+//KblZWbmcH0kcm5x0bft34rERZDxa3qyKWZxsfnywtfmmbDd12QiMWd/FiqUMjFBksARHEHddBBEh1UCkmIigjFoTgk/dDy+Ctvri7dzGTSRybnD45OWb+VsEUAtbXczEzz4wvlxVmn2fDcCEjE486+fKowMRwbLAGggthCHWwiAQUVRAchHU7EuBHjuHAdHv/lH1bKN7NDmSOT85Oj71m/lQyKAKqV4ZmZ5sULy4uzbqPRivSAgljM5PPJwndyA+kSAFEQW6iD2wg1qsoucaNOT6/bG0XvNh575ferSwu54eyRyfn9ubPWbyWDIoC1SnZmunXxwtLibKTRaEWiEEEsZsbGkuMTuYF0CYAoiC3UwW2EGlVll3F7TE+vE42ibzt/eup3leWFkVzuycn5ydxZ67eSQRFAZXVoZrp18cLy4qzTaHg9vRBBPGbyY8nxQm5gsARAFMQW6uA2Qo2qsstxI8Z1xXEDdzuf+/nptZXS6OjIk5Pz+0fOWq+VCooAVlcy0xdbFy+sLM6aZtOP9kEEsZgZyyfHJ3IDg2UAoiC2UIPbCDWqyi7jRozjQJw2o3zuxTdqq+WRkdwTB68+uves9VuZdhHA8tLgzHTr4oWV0qxpNIPebRBBPGbyY8nxwshAehmAKIitBN9SAKKq7BLHEeOEkBaifO7FN2qVpZHc8OGD89/d+571WhmvCGCplLo00575aHXxS9NqB319UEUsbsbyicLEyJ7kCgCjuJPgW0qA6CIojkMjVqUVujx68nStsjScHTp8YH7/I1PWb2XaRVWUbsY+/8wv/qdWvmZEwt4eqde5Z7eO5fvHC9ltOysAJASxhQo2ESBAKNFFEkIYqjJQh0dPnq5VlrLZoScOzE8+MhV4rYxXBLBwffdSOdyoiV9P1Ot1Vx4yxtm+rdW/J9If20F3gaRYpWILwSZCiQ4VdJEMBUpSBCbKoydP1ypL2aGhIwfmD+6dsn4rZYuq2GhEmi21/nbHjvi+j2C3ETdibrnSINXAAxCxYKjoIgl10KWqAKEGX6M6VCMQ4/f08ejJ07XK0nB26MiB+QOjU9ZvpWxRFTC7gsDxmlHbHASg/m6ABlWjG6pWtAXADVQU4CZ0qIMOVQVUgVA6VBWAFcAx4kZsdBt//NLp9spSbmjoyOT8gdEp67cSYRGAZaTtq9/abvyHjTFhe7C+0VRbi0bCnh6pt64bYyIqQqKLJNQREQCqGlpVFXYBsAIYETdie7f/D6dlNGLpcaBDAAAAAElFTkSuQmCC",
            "iVBORw0KGgoAAAANSUhEUgAAACYAAAAfCAIAAAAEKiicAAAJEUlEQVRIDW3B3XsT55kH4N/ved+RZBtZcvwpIynLcvkj2CZ1amOcdGkpdAN2bPoVaE/6P7YH3ZM9X7LQ7AZSjA0lKSSYADa2NNaMNDPPs7Iac2179b559hfrgqZZaggUOevKUgDOEoESRhhOOFBAitI1SIocChXfo3TK6LI2kNHg1FS1kAiAfKZBpqngoA+c/91vC3gRHx0Eb/ZGO63Jysh71UfTU1OSGzAICXHGvwFJo5BUuBBdTADDCZ8JgCCjUxJwBsAAAkjEUgIERHn25i8RPYnCN6c9V06Pf7A4P19/XBos5gbKzucAoygAkjhmJCgKCdHFBDCc8JkAyGUUJQFneKsjSAVZlh429jl+/Xp/9thLsljn2geDs7NnTw/cIVl0aU7UjJoJjpkBMAUMx4guE4B4i4oupgYDjDCcMBxLDC9j4/j162N9L94p5S8tFNd/OFAa7B9I/7Ovr6+Qht4S0sE8gCzL7JgCBsCM6DIHEG9RASgT0AAzU/wDF2hxlPWrvx4tPJwYG7iynH68vBd4GcJTIQfalsuALIdOEXYMJOQIEgGEOTgBiGMGA1RBNSBzaoQBRsPfy+gjX+KZq7eGc19Wxgd+drFz7eJ3Qgxmu05wKvFBbPGhHbxyUQTScjkWBrJCvwYBcoEgCNBlhi4zmBkyAJkzJQxQ4h8o3ZEMcubKzXf8TmXs1LUfpZ989FKztD98qaa+MRHvc/dp4/HD9uvXLQoKeQ6N2thkbmKiOFkrBLm8mZKEQc1gZlAABsJg+CeMruVO8dzVW2V5VBk7tX5JNy+9SpNOsL+bpunhk8HX3+izv7a272evXmV9/XCC8ij+5WwwPXOq/q+FoaEhVSUJwE6gywhDl5nhBEkAShe7U1y48puSezQxeurjVf145WXSTvO7A59//vDFI2m+0mYzbbxBu2MusCxFeQQz7w1/sFSvnY37+vrMjCQI61KDmcGopBFmNPwd0gRRkPD8ld8MyqPKWHHtR279w4OjZqu9VfqPP/7Pd4+TdghVM2WXSdbpoDyMufnK0oXp2lTDe29m7LEe2DFRQgED1fAWQdKcxrkjvn/lVkn+Uhktbv40//MfNw/2D/fuJr///ZP9p0kWgyDNkVBL2x2UhzG3MLF8Ybo61fDemypFSFoPDGYmGalmajDDCYIiNEGci/mDKzdL/GpirLh5Odi8dHCwf9j43/QPf/h671maxaAJzJuZatJuozyCuYWJpQtTtamG915VRYSk9cBoZi4jMjNVGt4iQQoco6DD9y/fKvnHlbHi5mXe+Mle87AZP9Tbt79t7LrGvjbeaNzMm1maaNxOyyPZ3PmJpQtTtalD772aCoWkwUwNRjNzGaAwNSr+H4oQTiJHvn/5tyW/XRkrfvKTdOPSbiuMcruFr79+kTbGdp9Gf9nZf/msYGZJG3HcHhrNzr0/vrQyVZs68N6rqoiQtB4ozcxlgMLUqMRbhFAgriUBz1+9VZKtyfHitY/a1y5+E0fxeDQSNkNr1Z/s7H3+31999dADSNq5drs9NJqeOz+xvDJdnd733quqiJAEoKowwCApoTA1KtFDEoCIKCVknvM/+3VJHpweL679W3r94vOw0axnY2aWhqcfbb28c/vJzj0mSaLJqXa7MzSanjs/sbQyXZve896rqoiQtB6YAZDEIYOp0QiAJAD2KF0DfZy7vjmIB6fHBzcvcf3iy8abg7qVVbXTnHz8cP9Pnz3dvsc47ljS1+kk5dFsbmFyaWWmNvPSe6+qIkLSDGYKJWBMHTLAACMAgiRIIZnRHdgAZzf/fTB7cHqitPmR21h+fdQM6yikaRo3KtsPDu/81zc795CmQJpLkrQ8onMLtaWV2drMc++9qooISeuBOjNDKlACEBMAJEEQ7Mro91Hm7I0rg7pVnShtfOg3l/ePms0acmmnHTWq2w8ad29/u3MP7Q6c5pMkKY/Y3Hxt+eJsdeZb772qighJ64E6M0MqUAKgCQARAcAelSDMj3Pmlz8e1K3aRGnzw9zGBwdHzWZNmMbt6LC+vRXevf185z7iGAHynU5SHubcQn1pZaY2+8x7r6oiQtJ6oM66UoERAE0AiAgAISkCX0iHzvC9X10uZn+uV4Y2Pgw+WXzTCsOqSBpF0eG721vNu7ef79xHHCNAvtNJysOcW6gvrczUZp9571VVREhaD9RZVyowAqAJABEhKSIkERR0uM5zn/60mP65VilvrubWF/dbYVgVSaMoOnx3e6t597PnO/cQx/DIJ52kPMy5hfrSykxt9pn3XlVFhKT1QJ11pQIjDIQAEBF2CYWCIMiGRzl382ox/bJWGdq46NcX91rhUVWYRlF0+O72Vnj3s+c79xDHCJDvdJKhYZ5bqC+vzFRnnnkfmKoISbG/UTEzZGIKgIQAEBEQQiFp3rdLQ5y7ea2Y3K9VShurbn3xdSsMqw5pFEUHZ7a3ju5+9nznHuIYHvmkkwwNc26hvnRhtjb1TRAElqmIkLTv0bqUqgBICgARAcAedS7s7+f8revF5H6tUt5YlbUfvGqFYdUhjaLo8Mz2g6M7t58/+hKdDjxyaZKW38HsudoPl6frU7tBEFimQlLEvkcAplQFQFIAsAcgCXXuMJfn/M31YnqvVilvrHJt8btWGFYFaRS1wzM7W/Gf7rz462N6b0y9CPoHUK2NvHeudmamGQSBppn0GADrIgBVwohjgh6eyEQOcgXO31wvpvdrldLGKtcWv2uFYVUsjWJJZ/dfY/fbw4NXhaPWkdNJ74Nc3oZHipOny8w9CIJAMxWKCAGYmSrRZQ4gjglOsCcTd5jv4/ynN4rZF7VKaWPV1hZ3W2FYdZpGHWpV0/6k47P2YBiGgSsVCn2ZHTrf7ivkj8I33nvLVHqsB+bRZR4QHBMzwwkRURc0ciXOf3qjmH1Rq5Q2Vm1tcbcVhlWnadTptEZgRS8Dmgy12+18MJTrH0iSvTQ5IKFZbGY0OCdCAWCAZQLAzMGEJCBmhhMk4fNhX5nzn94oZl/UKqWNVVtb3G2FYdVpFnXa8QDZ5zmIdKLVaiXxEIwqR8bIoCpt6QrUOekiqarWRleOeQcHA4xmRhIASevyudzk2f8DvbEzvpg4NTkAAAAASUVORK5CYII=",
        ],
        2: [
            "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",
        ],
        3: [
            "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",
        ],
        4: [
            "iVBORw0KGgoAAAANSUhEUgAAACYAAAAfCAIAAAAEKiicAAAJaklEQVRIDWXBSXNc13kG4Pc9596+3Wg00JhBioQE0aTKEigpgxylSorLGllysvDCVdhklWX2qbJ3MvMHrP+RZJlyaCsukgBBQ6RAkEYDxCSQALsJAj2ih3vvOd/nBl20zfLz8J//7ecAjA/g6eIkbp6ISMf41DvnY+diEQEd/koulzs5OYnjmGQYhtZa730cxwCCMLRhYK0V6B+JCF7gaq1OMptmWkftg737J+X/iuN4PNoq5MMosoYCiJgjnHIKhUTqBwBQs4o+AQUQMMUpBUAFQLyMAPjcVpKI976Oyt7hwd6dsPebsbGx18fLmUAID3WgqDnSPjhVgUTq8+jTLPoogIACJHiBABR/TVUBcEd6TlzvWe3xw+1n+0uzw78an5i4MLVnTUIXI+1BHVgXL2kqzom4UFweCjDGKeKUgYYEaaiqhmKMAlD1eIHPAeCWdpxz6VGtvLvXOLxzafya934i/2AoH1gfa69NCIIOFKTVPh/BD5IEezhFgFADzeA5VSW9oQCqEIJ4Gde1DYWrbn1XulOv3Pzo+19778f0aWDUeGUqCjAQ9IkBjLjIdfOqQliA1oKGtGRk4RwkBQTwoAcIOuLPVNHHDe0CSI9LO2tLjcqNzy9/LSKjrmrh6QEPKGCgCkPrUu000aiaXg9xj2GI3ACyWZOJwvxgGGYy0FRVVB3oAQU9XqIA+FBjwqTVlZ21hXp54cqbN0Rk1B8beO+GUz8MVcCrqk8LrWb85HFjd6tdPU47TRtlwoECcgMmP2inz+n3Ll6McnGc9OgZ0ir6FC8QfSTBLXUE4+rdnbUbjfLClTcXRWTEHVn4xI0lbhwQwKtq3CkcP2ttbRzdX2mXy0JHQ2MzMBaZLMfP4NNPXztzPudcQscMM1C8hCAJgDsqAHrVO9ul6/XywpU3b4lIMWkaiLpBdQVVwA0679vVye3Nyv2VR5vr3W4XVPQpTmWymDwX/PSn/zh1NgWc9wJVhVLxJwT7jDHcVmdgutVvtkrXG+XFz9+6JSLF+MSowuXocir08Uiv1yvvDq/9vryxVn564EmoQhVQKJDJ4sxMOD//2dhUi3RevFevIkbxZyQAay23VSzYrX6zWbpeq9z65K1bIlJMmkbFugxdRr1NO5ONen31d7KxVq+U23GHBLyIKlTRl4kwdT6Yn/9sfKpJ49ULxIso8RICxlruqBqgW72zWbpeqyx+NHdbRIpJzahYF5o08I7dxtTBwcHCr5NHu2nSg0XgvU+9g0IVfWGE6fPB/PynY1Mt0lGUXlQVoniZtZbbKgbs1L7ZKl2vlhc/vnxbREaSmoWaeFA62fZJ+nSvUFrfXV321WNEGTM2FhaHC+ulI/yRIpPF1Plgfv6T8ckWjaNXI1BRFcFfIGGM5ZaKATu15c3S9Vpl8ZPLt733I2kjgAZuMqkPPP6ucu82trYaT/Y0sHZqOjp7bqBQyP//tT28EGUxPRPMz388PtEiHT0oCoWKoE+hUIIgrLXcVm/Adu3uw9KNemXx07lb3vuiq1kqOq9UH+Ue3Nu4s+iOjxF3MTs7fumN4ULRlcv1xa8b6FP0ZbI482owP//R+ETLGA8PCKAK7wGoKgCesn3cVU/wpLayUbpZryx+PreYpmlRa5bydHNo/Xd28+HT/R10u5iYiN79m0sX3xhO9fHy8t63N3BK0ZfJ4uxrwfz8j8YnW6SHB8SoCH2KF0hjTGiM4Z4IwWbt/kZpsVFZuHL5RpIkYVuatebaUm/ttjQbcD30zV2efvfd709MDRzXtpdur99bwCkBFNk8Zy6EX3zxT2MTDRuIpbUwqkLtoU8VgDGhNTljDPe9kqhX10ul282nN//lnd92u92nm8GDe8+2v+3U9wAin0OhELz97syFCzPZbPCksn/37nppGX0UQJHN8ZXZ8PLlN4ZGGoOFYLQ4NDSYB4RsAVBVAJbZwBastdz3KclabbO0ttw+vPmTv73ZbDa//W391kL3yYZHE7kcRoc5OJidnB4aGs6TWq83NjaqJ8/QRyGEJtBcAZOTUWE4Pnd+9OLF6bNnRwihaeI5ktbmQjtsrOW+dAxNtbq7tnan82xx/u9vHR4eLv5vbWmxV94StJDNopAzgQ1oxRga0ns5Pna5EH0UCzGq6sQVChwZ1bfmLs29PX7mnAWEYQOAIa21gR20wQiCgI+kJx5HR5ul0t2kuvSvP7j15MnB0q9qqyvu8DukDRgiMugTRZ8xICACevRRLMSIaOJcsWiGR2Ru7tLld8bPnLOgmKDB56y1QTBoglEEAXdVa/WTR3srDzfuZLor//7DpVajcX+hvbTYKe957RKA8VRo0qO1DAKoIEmcxDgllmpENHF+dDQcGdPZ18++/c7Eq6/nAGFQ53PGmMAOBsEIgpDbqW7vlu+tfr29vTIV7fzHlWURaT0avvvNQeVxrL0MFOqNiKiPVCkeva7bP6j2GuijGqgJQgwVMT09PTaRnp85O/NqfmQMgMA20KdK0tqBjC0yDHnjUXz9+upq6f86yZMfXOz8/ItrtePjYjJTP6q3G4FPBgGKDwCIG0gT7Xbiw8PWwsJu/TH+JJfnzIXwvff+bny6mc9nstkwCAypyi4AEVFVwzAI8tZY/ud/7y4vLzdaq9lc570LrZ99/D/dbreo5xB73418OqAgJALg4ny3405a3YOD+rVrv68/Jk4pgHzBXpob/PGPP88PPXO+BxVjSKqyC8B7r6qGoWGOAGevrgIIW6Vh03j/9adffvBL7/2IJNalLvGJE3GEHwLgumd6HTbqceWg++BBpbIdoc94GB/lcP6C+eCDtwqFNAhoTWgYAgoTAxBVAISlZlSVs1dXAWROSkOm+f5s5csPfglgOG4b51wqqRPxhAwTRHpOXNTrar3qDvb3yzuvoM+2wTiT9RNnk7m5OWOapEAtYUlVxiRFFABhiQwAvvaLeySD9nohaPzDa5WrH34FoNhuGeckoaQUAfwQAE0nIRGQcS7T6XSsyQJQeFBoNMxoYWio1awHQQBV7z1JVQWgqgDMc+ybvboKIOysD9rm+7OVqx9+JSIjJy3jvaSUlOKhvgAgbhepWTISiUTEOwNA6WnUGsKk2WzU63XDMCShogBUFYCqAjDGWGuNMXz1yxWSYWejEDTfn61c/fArERltKr1qanxiRFR9AIBacCm8iNJIn3YAKFXEq8KabBRF3hMKw4AISKpYAKoKwBgTBIEx5g/yHM72+GzXzAAAAABJRU5ErkJggg==",
            "iVBORw0KGgoAAAANSUhEUgAAACYAAAAfCAIAAAAEKiicAAAJjUlEQVRIDXXB6Y+V53kH4N/vft7lnPecWcADjG0Wz8TCYRtmY5lhDAMmSiQnX9pINSL/ghWnTaTGciuZpm3UqomcVqoUtZ+qNrGtygo4RU284ZrYyBiwDRjMZmBgGM9+Zs72nve57x5PY0pV+7r4d/98jGIASvOl6elZfI6GJWZQAGK4lxJNRtyLBpJmChigBg/AzJuZLjE1qPJv/ukNusbc3MzU1KSq7+rqevgra9euXZfkYxEBwSaREFWSFpQsKJNilgOgDAykKk0BYbqCBIgmAjSowsxU1cy4BI788c9fq1Rnp6Yng8B1dT20YcOG1Q+ujOPACQCCoIBECE/CwpK5RTMCOTNkcAAFcGYAkbaD+D0DDWYwgxmazOAcWorgj37+m8Xy1NT0ZHf36j2jQytXroxDukAEaqZe1auCiKwugAoVUGMjiwzm6Yx0RECAcEoANDX1pgYzLCFIoVAoDMOAz/7j0XJlulxZ2LR5/ejeoXw+7+gBiChpBI10cBFqBBROQQVSH6haJgQZCAJAAAG8wnxmpgCoBsA5R9KWYAn//B9ertZnzfyWnq8OD/e7ICBBAjQQS6zJQwzMiAxQRcPoHCKBEIEi8MgyEHAOpAcMUNKTxBIDYE0wGP/s749UqjPisKXnq0PD/UEQACBhNEOTAgYgtSB2qHhMLtQmpxZmFyRJ8stbc4VE8kQeCBycg3MgM8BAJT2+CJ957uVydToMpad3w86hXuccABIGAwwwGJpmA6cen9yZO3fp+uXLNycmpVAorLivtZjkVrSE6zuLWzZ2BnUVJ+ZSo0cTDfcyNJHCp396uFKbieOwt2/jjqEeEcESgwEGA81ITjiZmCyd/PDSqQ8+unHzzsxsLoqiQhJGYbAsRu9XVnz7D76xKjIR+qBu9PgSpOMPf/JSuTqbz8d9/Zu27+wRIWD4jBkMUGimqjdqycn3Pnz9g5vnb89XKo3yQui9jxzjOLy/kO3a0Padg99c7TSOJEXDQ/ElKOQPf/pSuTKby0V9/Zt2DG0VIWj4jJqZama+Vq+lJ66mx98+8daV0o1yKAx9I/E+o/owcGvbsn09rQcPfH0NzAXMmHkovhAhAj793K8WFyfjXNTfv3nncC9Jo2GJmXnva2mjVCr9+++unT7zwaUZP2sJGfm0VVXFGYkHW9K9Pe0HD+xfZw0RMUGTmeH/ISlCPvOzw/OliTgX9g9sGd7VT0Lxe6rWyLRUro2Njf3bmxc+uXFzKsvXo1ZVtzAXq9cgJyDuL9R3b249eGB/F+rCJjHDFyOcOD7zs5fnSrfjKBzYtmV414AIDAogBT1YqmaXbi2eOnXq2LnbaSNj3Jpv7XBB7v0Pb3v1QS4nzq1Kao9uLB48sL8bdREBaIYv45zj088dmZu/HUXBtm09I7sHRQB4gAvKuXL6yfjc2x+Nnzlz5up0unLVqjUPrGtvW15aqL761gfee5cvigs6k9rIxuLBA491IxURgGb4QgTEOf7pT14qlSaiKBjcsfXRPYPOwQMCjM9mF6/eOnn+6juXx6dnZuphW19/79YN65zp6TPXXvvdde+9S3LiXGdSG9lYPHhgfzfqJEEx3Iu4BwPhD/72xcWFqVwcDe7sGRkddIIa0KinZy9OnTpz4eSFKx9O10Wks2vD/q/1PbKufW5i6o03P3797RlVdUkgATvz9ZFNxYMHHuuyuogoCRD/S3AXYQH5/R+/UC5P5XPRwNDWkdFBAWbS9MqVq8ff+/T8x+M3p6bveIvjeO/e0d0jq9uS3LXLd9586/zxdyveqxQDOllVbAxtaP32H+55iGkYSOBANJHEEqLJANAIDcAf/PWLi+WJJIkHh3qHRwe91ws3xl599dW3ztmn8yFjkWVRsVh84vHd3evyaeovXhx758T59943VUUxgON9SbapK9k90r9GSu3tLZ3LC0kcgCCIu4wAjcgc+P2/erFcmUiSeNtQ7/CegUqj8drxdw8f/tX7050Vt6rQ3tK2qq21ra1//f1xjEq5dmts/OrlsZvjiaoiieGk4BY7W9KudWvWyPjmzZv71j/Q0ZbH/0EYACpRF/BP/vLFSnUiSeJtQ1uHdveXarXDR988cuTI+aivlF8jwkDgRBJnquoN9Vo9LVfF3aeqlitCgpzNtnCmpZh0x1Pf+ua3tq9f3lF0uMsEn/NAxRn/+EcvVKqfJknUt7Nn++6+tN546eXXfvvKK+fdxvlwJbzCZzBzQm8KCY1OPRxbvFeLChCXw3wLZ/JxtFbG/+iJJ7Y/3L68IPicmeBzSiyK8Xt/8XylOpEkce+OLTt298P4n2+cOHbs2NlKx7wVjWJ0APLLOtK0YRbXalpaLEtU8FkGKYAuYaktLCW5qDuaevzxx3esX97REgCEEYASd3mgLOBTh35ZqU7k89H2Xf3Do4OVWv367emzZ8+dm4tnGzEoJiEJFtpVtVLF+J2ZGzdv+aDYqKfmCjC2RtW1K+Th7oc2tJQ2bdq0vjNujQHQQABG3NVQTKbgd5/9RaU6kcuFO4b7du/d2fCGQG7dGs8YQJzCNSQEWHU5VYxPVU6evnD8ndML1lGv173Lq6Gj6PvWL983OrK1I02SXDFiSBihRJMZSPyP+Yoe+6jGpw79crE8nsuFgzt6Hx3d6ZxjhCxDw6CE0RoCM9RhkXBiTt94+/zhX/9XSVemaaouUsPqjnh464N7R/u3rIAaYsABBig+4wkCmSLN/Pjk4r/+5hqfOvTCwuJYPh8N7RoY3bdTFQzgPTKawQzmqQZrpI04jGcX5989feqV10+Ml1YDEKaA3n9f67YtD+/bu31lC+YryDmEgiYxNE07BMT4XOPSremPL08cOyP87rPPLyyOJUk8vGtg9LGdADKDKjzVYArz1CYzE5FyvXb91tiFizdnqmsBkCmgywpx94PLHnnkgbYYlToiZwEBA0EAdxSVml668em5j69duT55c76LTx56vjR/q1DI7RoZ2LdvOwWpQhUKBaG0jGpmALIs83ANNd/wxbAAQAkDqOpUwzBwAZoyST0zg1PEAM7c8Jev3rl4bfrG+GypklZznXzy0PMLpdvFYn54uH907zYKUoUqjGowpXkxACTr9boycEEUCbWBJiUMCIgQUAMIEpmkRq9wDQ1V9V/+49LZcxdKNWHUxjA3lRX55KFfzC9MtLQUhof6RvcMkMjMm5kSRijhBU0kVRVQkgK4zJuZCkAaA4+g3mhIHFXrGUJxIqVFf+bK3NGjR6+WOxqZMmoP4jYRp2r/DXU++E0DadpAAAAAAElFTkSuQmCC",
            "iVBORw0KGgoAAAANSUhEUgAAACYAAAAfCAIAAAAEKiicAAAJyUlEQVRIDWXB23OU5R0H8O/ved733ewhm81hs8eEJCAhkmQTqYyI6ICgDEM141R733+gF73yxpvetBe97EwvOq0gFky1g47VGWvrAUVDDMgp5JyQsDkflj2/+77P8+sGLCL9fOhXv/691tq27UIhXywVmNmQLKX00LJEgcDEWill2zYzCyZDCCElCymEYC0AAiRYMrMGA2BymBQTMylUUQWAhstwGVprmybWHafsjI+Pr6ysGIbh9/tjsVhDQ6O3xpSCAMY9Qq4CLFWtUD4NOBIPCCoIkUMVGwAYkkFMzOQCYBLYxgAzG6zqKV3i+dml2dnbQoh4PO71+kKhOq9XGJIIPxJGBmDp+oTyMKEi8IAQZSEKAIElAIbBIBBruACYBLYxwKylXQ7QbCZ74cJXtm23t3d0dnZKKSBBBA0wwMQMBuDRBgANrcEAMxg/YENLSxkgMLY5gjShShNjG2Ebo4ogBWg2k/34449DoVBnZ2csFiMiLTQRaQIDDGZiAB7lIUDB1aSxTeMeZjaU4dEWM5hQ5QhoAgOa8AiGcqhA06Xc2XNnE4lET3dPOBxGFTERMe4jgAAomERkKy67UAqmlwOEKlsBijyaBEFgGxNAqCI8iuE4cp1mnPzpU6dbWlpSqVQ4HMb/MDO2EUAAtLQItLpVWlje3MpkakP+ULA26A9IwzBZeEGGAQP3EEigSuBRTI4jV2nGLZ46daol2ZLqSzWHmxiAZoABAsBELhGAvLK2tpxbEwujY9MrKyuGx+f1ehvD9XWhYEu4qSPaFKiVQRMPEEHgUQxHiw2adoqnT59OJpP9fX3NzWHNGpqrABCRJnIFAVgrWuPjd767PDo5dTuXL+QLFcdxgg3++ob63TuS/Z0729tako0GEcCoEgTC/yGXxSZNVwpn3jqTTCZTqb5Ic0RrzcyaNQhSSAhZYS6q8rdpZ3jk+yu31hfXHa1I2R6tddAo1ZjiZ13x40/vbm2JRoICAEETMQHEjEdpwKbJUu7s2bOJRCKV6os0R7XWfB8gpGBBeeUsr60MXl4cHZ+YXOSNgleQ6aFGkAjpTQPugZ74K0c64/GGsA9VBAUwmMGaiPATTMw0kc8PvvNOPBFP9aai0ahSmpmYoQRYoMJYLZRu3br19teTaxtbq1kj73il8Ag0E1Etbwh2D/YmXn2+KxFviPjADCYF4iowExEeQmChXZrMFQYHB2PxeF+qNxaNukqzFsxwJBQhV65MLK0ODQ19+P1SxVVlXVuBDzAdpxGAT69LuAd7E689v7e1pSFWA6WhhWLiKgBEhIexFq5Nk7nC4OBgLB7r703F41FXc0lvg5QFG7eX7o6Mzl65cmVkPhsM1YcicYfl4tJG1m5mwKfWJZyDvclfHn28bUdjrAaOCyUUQ+MeIsLDWKNSoslcYXBwMB6L9aVSiXjUYb6rKqoKnoV0ZuTa7KWrs4uLi+uOtafr8Z1d7ZnC3YtDNzbKMQA+vS7ZOdiTfO1YV0d7U8yC7UKRy8S4h4jwMK25UqSxQuHv5wYT8Xhvb28sEq0ovaWLynXT6+r769PDI6NTsxlmDrfv3b9/b+uu8Mz83PsffrvmtgLw6g0J50BP/LVjXTvbw1EDJc2AJjABBBARHsJac7lEN0uF984OJmKx7p5UuClScdQmZ5Wrvhie+/a70ZmZpVzeE6oLPfnM0VQqUtdkXb156933v17lXQC82BDsHOiJ/+JY186OcESgyMpgSIAYEiAiPIS11mWbrpXsf5z7WyIW6+7uDTdFXaVmNjN3Fu7861L6+kQ6l8v7/cG2tranDxxob/NqFiPfjX34yYVlKwnAo0uC9VPdyVeO7mnfEQqRy1J4iA0CMQwGQHgYsyo7dLnI5wffTMQi3d2ppoZIvlD89NLs0NDQ+Gbdal4ahtO+M5hKpXo7mn1efXfVuTo89cXw8HwgSCDLNQSL3o7w4X2tzc01zf5KPB6tNckkCIahsY0Bwn2sWdtMw7b7wbnTiVikZ29fsLZxfn7lrfcv3rx5Y93qLFJ9TQ12tAV2794dNCrFQjG3aa+kt+aWl5dr/ILIcIWAiNairYH8/pqneoP793c3ei0TEAxTo4o1E4GIGNBKcwV0UZc+evvtZDTavbcv6G+aml7587tf356bW/Y8nkNIc9Hrs+vq6opba4aUrmsqV7KQOWFKaZCrJAkvZwO8GQh4Xzm+59ChrmavZQGCYSlUMTMJEgRmaMVcIfrCLp3/61/aW5L9ffuCtU3T00tvnv9mMZ1Ooz3LQc0lMvKWaQpmwzDKZeE4UphWAYZpmmw7UggfZ/28Gaz1v/TCrucOdUd8lkUQDEuhSillGEIIYobrcsUmeu9O5swf//BEb/ezhw7V1zfOL6yc+fDi6Oho3upwzQYWrpJFANIXymVzxbLJ5JemVba15fG4hZJhGLWy2GDmGxpCxw+1PLHvsXit5ZUQDEuhSillmkIIUq4u2XqrRPTbT6988NafDj/7zPEXn4+GGze28sMTK2NjY+tuYwUBLVwlbQDe+lg+X7idvru2aSvFuYLy+v12Nm8aRrLR2pPwJROx/j2elpbGiFfWCAjNpmICKbCUxIyKXclkiwurJTr+u3/euHh+4OcvvnziuR3xgKvdPBvzCwswA2R6NJErCEDZrLubzX85NDZybTqTLWXtgD8QKGfzpmGkHoscfrK9tTUaCxUDPm8DiRpAMJtKE1GZtlUqlXw+v7S8/tXwJPX/5uzW3DcDJ188+cLB9rjPhSLUKKAMVBgKUNhWJORL+t8Xbvzny8sLi2tlI+bx+pxC3jCMw091vXq8P1QPH1yfKWsBCxCAqTWBykQayOUL6xsbM7fTH31+lTpfHylPfzZw4sjJo/0dMTB+IBmEH1kCa5u5oUvDQ0NDU+n12ZouAAY7BH56X89Lxw76fSLkhWHAJ2ASCCwIVUVGpliZSi9fGxu/PjE764Ro9+sj5enPBk4cOXm0vyOGBySD8CPH0YVCYWlpOZ1OL2VK8zWPATDYJXB7oqFnV8RjwW/CNOEj9gAEuAyl9UJeLS4tXZ+cvjU1s7SR3fS2UusbI6WpzwdOHBk40tcZBhHuEwDhB1KiihgKqCiUXLjYxgIgGIBFqBISRBCwCcpWuFvG5ubW1UUxNTM3Obe6lasI01uUYWp9Y6Q09fnAiSMvH051hiEE4R4BEH7CcXRFEyRZHjjYRgABEjAAAlxAMRjFiipmcqX0UmFtbe2TK+vrm5mSawmr1vQEttwAJd+4Upr+bODE4YHDfXuamaoYVQIgbGNGFTOYQQABgiAIRNAAMzSgCERwBDSQVVjdKs4ubNyaWZ2ZmZlY9SomeEIwAwLCQzX/BSarMIduIHlVAAAAAElFTkSuQmCC",
        ],
        5: [
            "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",
        ],
        6: [
            "iVBORw0KGgoAAAANSUhEUgAAACYAAAAfCAIAAAAEKiicAAAJ4ElEQVRIDW3BSWycZxkH8P/zLt983zfjGduZOF7iODZuYifxkjRJk9CG0kZiEeIECAmkICRuLOLMBcQRCSROnBA34AhqoYpKStR4srRZWtdxajt2vCX2zHjsmfHMfNv7PrgpFCj8fvSFbw1HMUdRHAQchiJJYhY1rbVSjlJKCiWhiAgsADAzACISQhCREIKIQIAQRARjARClCJLZsZwlIgOBZ5gZDAlNC+V6ylXVanVx6cNHizONRvP46Jl8fl+uvSPblknplAnxCSbLxPiIwMcIAANMjLQNABhYJhhCoLCHifEvBIAVbQRWSHr6dGP24fubxceDg4OHDo5ms9l02tdKEogMPsYMFgDh/2PO2ASAgWFwIhBK7GFY/BsRNK1FFuDZBzOrayu5bPrcC+cjEwsSYAYgAM0WgAAss4GwRGAB1tjDEiAmgCAEJ04DACMBWwIEBACGwX+QDFpuxVLS/Xv3W2FjdPgzR3v6KsyJjdlYZiZmDUuAIAJzQsJCAAJWYw8TACaAICQHqomPJAATQzCesSD8E0MCtNKKlBQ3b95gNhMTJ7LpTAIGwCwYABMBIDIQQiAkiggMWFCpgkwbwghs4DnIupxQAECgQUgEhGIFQDI+QYCyoPUwEQKFQgGwE5Njvu/zxyCYCSCArLUxKwAb9fBJpb69U92uh9s7u60wchy3v69rdPjAQLeXIAAgsUuIBYSGA0BafIIAZUFPg0QQCoUCwZw8OeZ7voEAUcTCEllQzNRs8mo5aDZb0yvl2ZVSZadarMfGmnqrlE57I0O9L0wMTxw7POLHAFJcVxxZUgm5+G/EpFjQejPSShampgjm1OQJ3/cNaSIKmBJQGKNUjYqbWzenFzeLpaVKuNEwUWJ3OWWMSWhbOTTQ1XHm+OC5M6c+30MAPFtXHBlSofDw3wgkraSVZuJoUSjcEDZ5fmLM8/1EaCGpBWzVo0fr9TuPK/Pzcw82uRVErciJbUoCnkmSODGpWmyCvjb/wsjQKy+fPXsIewQZIssAE/4HCRa00rJaU6FQkNY+P37C8/1IOCDeDmn5afmdB8t/m1krFotPzIEoNiZKSZFOO7rThGDO9Sovkzqcy0707D98KH96GHtYAgTAEhn8D8GCFkPrKCpM3RaWz4yf8DyvJUSrZeY3m+/PLd15sHpnDVEc14QbBIHSnN/fPjjQN9LXlu8U2Yxsc52ulJvXChLDbYw9AgQwERM+jUFM9ChhLVCYuiMMzowd93x312Kn2np3oXjj3sz0o631eKDVajVlAqLe/vSRke4jwweH+/22NHm86ymRJZVhAtDNRghBz4AIQgKwjH9jgEELhhWhMHVXGjo9fszzUtsxlh+vv3V/8c7M/EolqqSHgyDQ0mituro7e3r2ZTIekARB0KGSXMbv7swdzGfac85w1mpFPpHGHmIiAMz4FJozrAiFqXvS0JnxY67nlFuYnp5549bs7NJ6KZJbmYE4ibMykUL4XkbrVBglOw1rjEknUWcu292V7Tng9/V2XBjxu/JtXY5wAWb8J2b8E4HmLEugMPWuNHRm/ITrpYot3Lp1+41bHyw+Ke7Y9GZqOI7jLNWlIFf7WjoJRJUzANxmyVNEDguX9+/f9+LR3OTksRP7nXYHxNAAMz6NQHPMklGYuiUtTo+PuZ6/2cK1a9f+evO91VKlIfKb+jiAdlGShHyuqzvflW73K066VDKtx++ZsNGgeFfGjlaDvnnllc+9crSrv8PxFLIK1uLTiGmemdgWpm4IQ8+PT7q+Xwxx9erV1wuza+XdQHVs6SGlVd6p9fV2Tx45eHRgX1tWxdxYWWldvXZ3s7hVZaqplE2SdHPr3Llz3z43eLy3zfdE2sVHWOITZAkhLTCDk6mp62TEqfHnXc8rWXHlypuvXV9eK4exTtfdAwAGOvniS2dfnNg3tB+ujXNRabu6/Zs3N+7OPF6t87bqgpRufae3t/enXx156blOoSlxmIhgHQCEZygRVKUFtszN64W3yahTYy94vl9mefVv116berJaDCPl1px2Y5KRPv/SqyfPjnT055CG7UDQiIJf/3nh5v0PlyvxjtxHWrcF20rrX16+8IXxPlLc4JgAcAoMEPYQLFGTFjgxqFy/cY2Me2rsRc/3t9mZmrrzl8LW0tOwyVzTGoyBffLiSxMXRvODnTIF6yRRLQh/9Ye7Dx892TJuzckncdQWrOZyuZ9988zF0R4pEQtDRLAOQACIIAgwTItsqnHx2ttXNWXOn3zZ8/1QqPvT869PlWbmy5UgrHk+W5uVwejoyLnRvud6MloiqVWePt3441sfVuqhTedDP9+oV7Px8uTk5Hdf6j/el1FKKikIINIAEYEIAoBlehRF0+XFK2+9mffzX774lbTvZ11aqzSvFDZvvDM/t7G56fjWWERB1/79A135ns52P+WF1cbS0tLidi2yzDplHc/RYrRr5+WXL158rq0nJ6UVTqyIQAp7iCAIArAWdH1tpbA4e+3G9aEDg1//0tfaMn67T7UQ7y3Ed99/fOvho3eLNWbYVsvz3DalMlo7yokb0Va5HKQ8JmGkEp6Xz3dcOumePj021iU6UlYa4cSKAGjsIYIkkOFKlel3b719ZeHR3ZkH506c/N43vum5nFahIrXVkHNLpb/fX/7TTLnZbNrQANBGaVZEkq2yFgYMMDSn270jzx3+4gvO0FD3Qc+mpZVGuJEGYFMgAjE0YEI7N9+in//+TzfWirNLS6+ePfn9b38tJRm24QgnhipX+d7jxusPiqsra83dVhAECEkYARDBkUKyBBEyOX9g6MDJiaGjB6LOfU5exS5ZaaQTKyJYRSAQA5ab9eje/U169Sd/qHJ+7Wn51fOHfnD5vOewb7elkCH8BLplsNm0xVL19gcPHy+vNOqxtQL/0p4V/f29x44ePjLYk3VVXG+6rqtVJIQBSEACECZljQ2AUjWemy/fvvuQzv74t0nq4PrG1qULh35w+YLrcIZ3BIkQfgIdgxvgxKIWU3mrlcRKCg0Qnsm1wXOR1vAkGKxiK6UgCggJQLAEwMZOGIRrleThUvHB7IfzSyU6/dPXIpNeX9+4dL7/h5c/62nu4CoRGfYttEVkqA7AIGXBbDWghRASRIACCLDgBAxYbUNmFmwIFpaQkLW81pArq2vvLtWnV2rFrZ0dm6WzP38jjFJr6xuXzvf/8PJnPc2dqAFI2GdoRmxpF4CBw9jjMDQYexhIEiMESQEiS4AHE5tEwBAsLNkQzVbr9vzm3PzC9Hr0pKlJpcJUFx38xb3sbmNzdfXS+UM/+s4FV3EnathjfLAWYAkDgKxiBhhgMMMCzLASDFhiKyEEGYHIILFgRqOJldXwnXfu3VspMmgLmRr5LFRLZ/4BH002Ojg6ui8AAAAASUVORK5CYII=",
            "iVBORw0KGgoAAAANSUhEUgAAACYAAAAfCAIAAAAEKiicAAAKPElEQVRIDVXBe2yV530H8O/veZ73Pee85/huwBfABhvIhSpCWePYA2NX3apIXVncS3yJKm1/Tfun+2dkqzZ1UaeqJO26NdW67r9pUpWRSk1oS9IWpSsJN4OxDYbYxrEdx4Btju8+l/d9n+f324GQsX0+9Jd//0N2q8XC1srGrY3CuGc8V7TMnAxS2hhAQHIfQhEBO4jgU2SIFIEAEiJSECLC/2cEJST4X/SdH59eWrw9MzOtNR7b33rw4MHWfc3JZBKKhPCIKAiCGCkLQEACYD1gMcp42vO0ImhGiZATMJiEDUoEJaJYFOMB+uarJ+/cnisU8nv3NLW3PdO6r6Wh3hQjOAEIjwgBSEdIWtxHAmAtBacBEBFKlEOJEIswhIQ1SgQloliU4AH65iv/OT019tiB/R1/+OzjT7QGgU+EMIwiDVYEEQhKiImZNYsWwaecSUApED5hGCVCFsICAQgAQwEQQPAQfedff35t+MIXvvBHRzrbK6vSIhBxgBSVWAgAEhCREgVAA1oEn4qhBcQCAURgHO5TkYgTEYgAIOOzlEDwEJ34yakrQ+9/5SvHjnS2G49iJ5YsiEIFBxGUGEIJQVBirRTyiGKXTOpyTZ6GEhBDBMIQAVRI5ADQA04UABYRgIgA0Cv//ovBK2e/9rXnj3S2K4PYwSoLICTExI4hrJkFbLLZ1aV7K8vLaxsbW8wShsWm6pqmxp0NO2oq0j4AEYgAVAQ5IlJKARDSzCKA4CE68ZPTlwbffaH3+c6jHWTEWi4aq6AKpBmIXJwtbiwsLn54p3xpaWXm3sLd9ZUwjJh5c2X5iYadTz158PDjzYd2lLFASABoREqxIsInyJcSPELf/fGvLl5+t7e352hXB2mJrMubkECbpItOlteyN6Zujo2NzdzbXQztsitucOScC8Mo2tzYt237Y637njt04I/3N4pASAAYRFoxPQBA4AMQPEL/+Nqpwatn+/qfP3q0gxSs5Q0TC8t8zkzfXp6YWrjxYW5mZmajosKyxDaO4ojDmELL1tZVVLXubflSW/MXn6pihiJLRJpABAVSjBJRCoQSwUP08j+/eWXkvf6Bns7ODiIIYY3s5ubm8HRu6MbM5PTq4kbd6trqarrAnoYAsVUsO4LKIAh21zU01Fce3h8cbjIiMCpWJaQIpASKcZ9GCRNA+AR965/eujp6tn+g58iRDlLwPdxxfPODD3595e7I5O3ldXF+S76Q30gteEGyvrZqd/22XTtqn6xJkfaqk0E6qetSqsEHIMrPa62ZAAhBk/gAFAGE/4v+4funhq79fmCgp+NwhwCe4ev3sufOn/v1cHZ6IRcjrTP74jjOlX3cvLfpqQNNrbvrasuDPSYyJlFpxNOUcpSOARIkt7TWlhxDCB4hBUDjPgIID9HLP/jl0PD7/QPHnu18JlQs4N+PzJ05c2b4jsoWDasAQR0pRX6xrr5uZ21tZRCQi9TqvFK0r3FbTVX59qqgvsqkM0gnYw0SYoEAiuAJRIHwAOEh+va/nL48dGFg4IttXX9QMLYYxW+/N/fOO+/M5KtzKLPKi7yMMSbhPK21x75mLbbo1meZXXNddUVZeufOsiefqGltbWjcDgMYWIABIiiBAAoPER6gb792+vLQ+b6+P2nv/mxB29XN3M/enT579ux8sbagyoV0rBJEMJYB+Mr3tU/EYbRaKBRSCdJGV6Z1S2PN008f6ng6nfZRBudDFMNnAlD0BI+QQNHLr/3q8tC5/v4vtXe15Sm+u7r6H29PDg8P341qQyoHKYEiopqyVCqZrKkuq64KYDC5uDkxPg7EOun7giptDuxv/dPunS1NmXqtUgLPIROjZC2wQoAIQQTE8OhbP/zF0NXzfX3HOrrbtiSaW8z+6J2J8fGJ5bA2RpkieIQgCD7/mca67VXbt6Wqqo0FX5hYO/PbM/fW2SqjyfPhVZZneg7v+lxX8+6UlwKMSMIKgHzCCYFECCKgGD793Q/eHB651Nd3rKO7bYvDqfl7r/737NTU1MZmhZOMp6hMcWNj4zc+t7N5VyaTYd9I0WJwPnfq1KmhueRWSIwES8qAn3sy9fWvt+2tTKYIBFHiADhiEAhCEAZFlKC/efWNa2NXevue7+huy0s8u7Dyo/fvjl2/ns36VpUlfK9CS0tLy198vql5lylLI6EQRri84N56863LM2q9wCzGsaGo+NmG4l9949iB2iAgEESBAVhYIlIEAjuhkJJ0/MTr124OvdDb09H9bGQkux792+9uDw4OLqz5kUp7msph9+zZ89Wju5t2pSrSSGqEEYZmi6fffvuDJb9gNYtYayUOu1rUn//Zc4/XBBklBCERAA4xKTIkEHZCeQror7/7+rWbV17o7enobncJbBT5p+cWz587P7Oi89bXwgmx22q3tR2s3r1rR01ZMu3pYuyGJucvXry4aCvJT4tYGxeSCdP7TP3RroP7yryMEkBIUOIk0kZrIogLLbJxio6fODkyNtjX39Pe3W59RE6GPsyfO3fh0vjy0nosIIZOJBJUoesb6uvLqzPaz+ULtz6aW84u5xKBl0wq5HyvsKdpV3/bnr17d+z2kQEDBBAAx6GvfaVU7OLVXDx616PjJ06OjA0ODPS0d7fHHhzjo3WMjIy/N7o4c3t1K18MWRMoKqMgk05YUkUbxfHa5lYQpHMJPwyLvinWN6QPPfWZrx5qrKlO1JENhAECCICT2GjDIrl8bn5p4+xETC+98sbotUsDL/Yc7mp3GizY0pi6tXh1Mj82eWfm48Vs3gBYymhR5MfaZy3sCpvr6UzGlish2V6jDj5e88yhg90NSQKqOEqKgwBQACLtBCgWw+XsysTs0i9HI/rb7/1sePTiiy/2HO5qdwpOUPCQzYZzWXPj1vyNyZmPs8VCoXBXOQa0M4Y1AeJiz/PKG6uqtlXv31N96LGK1rpUI8CQMhf5YiEEKACxRyK0urY1PTt3dezD39x09NL33xgevTAw8OXOrg4QmBEnEMWOtQrJLue2ZucXxicmxm5lHYuDz+QRKd/3Wlv37mtqrCwvr/bsjkRcU1VFEGZnJCZxAEE0gA2d2Mq7yY82R27OfTD10Z2oio5/7+To9Uv9/V/uPNpOGs4hSkBYLIGJLFAUydloYYVZhOExGSJ4HtJplHswBD+OU9alkknAEWDgFFgEzArA7bw/Pjl/9cbs5OydtVxUSDfTS6/817Wbl3v7ejqPtisFa4GEOOciTaxQIhAADoIHFAQgAAyn4RRgHHwnADwSx2xFCZEjirQi0OmLZmT0xvTd25thSL5fCAI6fuL1azevvNDbc7Sr3RjEMZAQdi7UYAWABAJAQAAIQgCBFLQCLPIC9hx8h5KkUcysdDKyLru1MZe9t7m5+fPfxYuLK3lxKplUicSqIjp+4uT1scHevp6urnbPII7ha3HOhRqsiUBKFAAlKNGAAoigFKIILGBB7NiBg8CwgQDLDgurmPl4a3Ry7vr165tqGwus8ln7pOBU+D/VyHS3xKE+pwAAAABJRU5ErkJggg==",
            "iVBORw0KGgoAAAANSUhEUgAAACYAAAAfCAIAAAAEKiicAAAKLklEQVRIDW3BW4yc51kH8P/zvu93/mZmZ3dnZ3cd2+uzvbbTxG4cJWkL5gJUUcINUlWpN73nrhISArUIgYQoAglQVVRUUYEQvUBJ6xDjU9wc2iA7MbYTOz7Fu+uNd73e3ZndWc98p/d9HiYWF4D4/ejXvj6lSuuqyhalVNYx51roKQBuiNk5/E9EUApEIAIRhohAhP8f4X8R0N31x+gUve7m6uLljUfvB76ZlDkA1u+IyphgFQOoNONzDIAAQ1BKxf640b6igigDIEwQYVsD+6CSdB9DIgIBSNHnrLU039vsL3YX5xaK3u1muFRP453+QwDOX2edMcEqBmA1CwQQQAjQEAJpqQGaUBD6AIQJgNiasE+qIvUEgAiLYEgpBZC1Fd3tdReu/sv64/nRcHnv5FqaRPFgHiJs1kVlIMs0ACCEzwmGSKAYQ1TVIIZUrigTgQAiEKtZlBLS8ADwkDAApRQRWWvpVnft+tt/E5hiutkZD+96WjXdIx4ya0IZyIrKARDhvwkI0AIiUnaEYIhyoifWiuApZ0QUiSI2eIqdE0ApRUTWWrqxtnLrwt/VfZ6euD8Sv2uUGisgwqzXhTIRgmiBiAuERVEi7DvrQpX2+30FFUaxF5agDbEVVA8CCAlAIIAwJMIMEJQCQNYyffR4+e6FH9YDmZ6420wvalLjhWFmMR2oTETDGRbJ+qgq29vA+hp1O8h7uixdaxQ7ZxqTU16UFnAWekME/wczREAEIgxZC7r6aPne+X9oBHimfXMsPeNpU8tCCNh0RGW2TPub7W53486tlW6H+1vS38JWD/0NVBXGW3j++YmjzzYnpzwiwKwAEBiAIIrYB+AcmIUAUjRkraMrS0v3zv+4EdD29o2J+mnP+Ek/AuDMuqh8dUVdu2w2N8u5T4veptgSYuEq2BzMMD4OHPSfe66+d99IkkQwywAYHqAArdgHQPCYRT1FRM45urK0dPPCX9dDnmnfm05/7nmeKVxvK3ODsdVH2d3b63dv8kYX/QGIIA7CIIALEMFaNOp07Pjky1/aOzJSQ/ApAIJgSIxwjCE9EHZESmulyLNVQFeWlq5f+F49crsn7m9L3wt8X8osy4qHt+qfXC/u39vaWEWng6JEGIEAAjyDyFC73QZ7RuuDh1pfPLE9jgOKbouIggME8JgTAKQ3WBxAShGRcWVKHy4vX7nwR/Wo2tde3JH+h2d8l408Wl6+8k518yp3HhvfNba2SkE/iryRUdVqJxOt5nhT1eo1TxM7N9I009uSMAzJbImAJIRogEVVGFIdYcciEAGMLRP6YPnRB+d/vxaXB9oPZ9LLWnuq2nP+/I2Pfll89qmU/TilVp4X9Xpeq6U7dkfbZ+pjY/VaWvlDnnW2JFXEkSRpAigMuQbYhyqhNzGkuiIMgIZgbJnQB8uPLp39bi2sDk4v7qq9z06tLez96U8/nv+47Heg4cemNhhk9RSjo82xVhDGYivLtooib3zM+L6q19VEOxgbGzVBhiEOIAbkQBWGdFeEiUhrTeTZIqbLS8uX/v1PapGd3fZgV+MX2cBePt96++2Hq3PWZvCM8pWuqiqNdJokSktRFv1+aXOEIRp1L01Na8LbsTPdtXtyarsQEaAhhM8pAKI6gBBIKUXKs0VMHz5c+eWbf5lGcnT7vT0jb/V6+akf4+OPit6KKAejoVhpTZGvtVYi1rJ1FlUB5+BpRCHiFKNjZs+e+pdOzviep3UAKIgFlQCENkACKKUMyJRlSlcWH7/75g/SUD2785N9zTc3uoN/+qtiYQH5BjyCZ0gsidD4aDoy0kiSwDOw1nW7vdXVXpVVisT4iGKMj6tf/82ZVqsVR3WljKAg6gEQ6gEiMEp5pPzC1unq/NrFc3+RRPLcjk8P1M9tdLN//vPGgwcbNiNNEHFl4ZTCsS+Ozc4eGGslnsdFUS0tdj++cefxYlkVog08D55PL30lPXLkSHPUN0ZEKkgBgOgJIEwBqYBMWFGLrt/rnH3nO3FaHZtePJy+u9kpXvve7O3bd6xlYTi20DZNzW/9zsSBg3trDYYeVCV66+m5s+89vKcGT9hZZgsWvPQr8SuvvDzW7nv+gJ2IIwBaBoA4lZJOlB+X/ja6drdz5p3vxKk7vm3hcPJ2r1uc/f7shx/eGvQrAMaTIKZa6n3jW0fHx5th4pTO2WnY9muvvb4yH/Y2yiK3thIRvHQyfPHFF1uTue8XwgIWAAYZRFjXyavrILXRDrp6p3P6ne8mNT4+PXc4eutJr/ro9WcvXrzRXS8VkR9AeRIE9NXf3r9zZnucamMcxHRX6WenTm+uRPnACVgbKIXZ5/HCCy+02uz7luBIKgAeKgLENCloqqjO8Qy9f/PRmYt/3BhRJ565Pxuf629x5z9PnD9/5bOFwlXkeWAIgH2HwiNHDzVGIqWFHS3Ob167dnttieF0GOsoId9X+47IsWPHx8bEGAuxJCUL+1QpIpgmwlGK6ohn6PXLty+e/bOpyfSV3XOHk9P5QMLevkuX7l+7VD1ehq3gLLRGGGF6qhUnIbMUebW1VeR5ubFqNZkw5rhejY4lJ77SnJqcrCeeMUSwhExEfJUTAG+UgiZFdYln6Af/9t7lX/ztjh2jv7r/wRdqp4sctWzP/PzC9cvV/buyvibZAEqBFMIQxpAIqgp5JlUFFCoKw1pDRltqZtf08y8naS0NPGgCUJEMAPhUABDTIH9EhbUqmqFvf/8nc7d+sn//tt849OBE8zQ7SbL6oN//5Lp/60Y1P5etr1JZiigQgUhDSJhJWEBeHiZROt5WM/vCw0cOtvdvqCEIQSAVSR+AJ1ZEnE7Jq+kgzaMZ+uaf/rD72RuzB7d/9chnL4+fEZZwkLqqWl2aWJgb3L/XeTCvO508K60IhAlCSsHT0Fo3TKNZH3lmZ7JvtrF7z4yN7gigIBCBVCQDAEasMFtKxCQ6SPJ4hr7wez+qd88cO7zja0cffHn8DQiCHGAuswlbRptdb+5OtLKy8nC5M8jsoC/OURxRI9FhGO7fHk62RkYnwtoIK60rv5KnMCRMbAEYzkSkokh0TF6UJ9vo0Lf/fqx37viRnV87svDl8TdI4OcAc5m1qyKsyrTs73LMpPzV1cdFXimtA89PgiSKwprpeZqVychsgaQ0FoCAIIAw2AIwkolwRZHomLwoT7bR1B/+6zNrPztxePurs/MnR09B4JUMa4uiXdlIw/gqybIsimoQAARSJCBLIKIiR1lCBtB9COD5GHJ1SMBwFiUAZQYibClgHZIX5ckURb/7o0PFWy89O/Pq7PzJ0VMEMiWjqsqi7TiBaOV8AGXBeVEY7RnPA8NYsEhkLYlA5aQHUArGgwi4AQkYzqIEYLycha0KWIdkwjyZouYf/HzP+j++eHjq1dn5k6OnAHgFo6rKctJx4tgrOQXgLDEzRCnlGW00VJ7nidewlQP3Qj8LogjZGoYkhhhmsk6LiB8UAKzyWQfKj/Nk238B53uKnYBqGVUAAAAASUVORK5CYII=",
        ],
        7: [
            "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",
        ],
        8: [
            "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",
        ],
        9: [
            "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",
        ],
        10: [
            "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",
        ],
    }

if "SCREEN_TEMPLATES_B64" not in globals():
    SCREEN_TEMPLATES_B64 = {}

if "USER_RESULT_AVATAR_B64" not in globals():
    # 1x1透明PNG，避免启动阶段解码失败
    USER_RESULT_AVATAR_B64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
        "YAAAAAYAAjCB0C8AAAAASUVORK5CYII="
    )

if "REALTIME_RANK_TEMPLATES_B64" not in globals():
    REALTIME_RANK_TEMPLATES_B64 = {}

if "REALTIME_RANK_WIDGET_B64" not in globals():
    REALTIME_RANK_WIDGET_B64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
        "YAAAAAYAAjCB0C8AAAAASUVORK5CYII="
    )

if "SELECTION_DIGIT_SAMPLES_B64" not in globals():
    SELECTION_DIGIT_SAMPLES_B64 = {
        1: [
            "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",
        ],
        2: [
            "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",
        ],
        3: [
            "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",
        ],
        4: [
            "iVBORw0KGgoAAAANSUhEUgAAABwAAAAgCAIAAACO148VAAAHpUlEQVRIDVXBy2+cVxkH4N97zplvbvbMeByP7TixSxq7IU3j3gBVhaotTQotIWXHAqlSWZZF1ZYNCKGqAsSCbvofICp2FQuQClU7NsQoaWJP68w4tR3HduI0juMZj8dz+S7nvC8Ti6DwPFQtv2FzL6reJ/OZ/E2oW3vtr7brf/zL39bW1gL2vvnyz5aWljZr9e1WZ29v70BfeuJo4bXXnvkGIckIGc45AEpcnJsA2GjrGdosva4GzsT6vpVJZUp74YX58vT09OKt7UKhMPLAkckXfvjxx/NXr2/drNWJUOjPjB8Z+MmPH38M6MFdLQt2DmwT0gLgjHaeoc3S61Q4EyVPbm1t/XU5/HzperlcyY9875ETh8eO5cyE/4+/f7yy2rp5OwlgJJcbHy2cOfP4pMEBg94YInbWWnGRkRCAM57zElQtv8H9L+3K0fPnz38wW9tshMIyMfnqyEg61R+t69LCwsLtO6paz/u7jYFM7wMjQ6dOfWfS1IZ6TT6fyRDCKHQu0hwAcMZzXpLqi2+7vpduNQ9+8MGfPrp1tBYmCKTSWWOUxNCMW4FEzD5bG9k0bI9R+f78T595cGI0P3Ag28eBc47ZAQKgTWgQqL74Nudf3uoc/vDDD/98ZfB2KwbAxpRS5JTqJEgpLUbYsFKqBzZJEjOxV789dvzI8Ojo8AF0nGMRxj7WhmMeVRfewsCZJh0rlUofbZpqQABC1QMgYrXdTlxdXvYjJi8dBkF/T1DoMxPj488dzR4ayObzuRGv5RyzwKoYAKVIa03blTepcDZMnNzYuHm9bzAwHoAAGkA7oKUb6pNPLt6ptUOO1berQwfs+NcyZ88+P9mnk0pHTo+l2q5LECkPQKvZrNeqtHX5LT34Cvc8sdfca/b1WqMBhPAANDuutNyam7u8dadRb1obRfkcjY72nfnBs2NJKOs6fnQoEwutCwW+MTu74cryennuMm1+/qYe+hEyTwZB0OqNOa0A+EgACKzcqEXtdtM6y8IGFDpoLzY2ms9EUQxQSuU93Q7Ceie8ttOan79y51a1sb1HG3Nvx4Zf0ZkngjAI0ynWCkCoNAAraDiOkSICKUQQcoCQMzDNtqd0Mhn320G1VrtV3Z3f2C1XKq3AQ/wgrc/+PDF8VueejMIwSqVEaQChRpeDtMDsnANbiNFep+miSOLpeIYjLQxgbr5yff36za3aV2HC76I+3xyma7O/6Bl62cs+7tixx1ACIECGCE6w64SUc0qsYqNM05e2b9vtZqvR6bQ7XV8s13d2avVmZ9emjNY2lgm9flqZ+1Xv4PeT2ceky7RYIudcxw07Z61I6CWYxXedtmtEUbRV36vW97a3t7+602n7Uddtf9A5Zx21bUIpxV7KJbK0XPp1pvBiKveoItohrjbqW7e3dqODvu8H1gWxtO9HraDZ6Oz4gV9tNButtu/7bUtO0NXCISICaSsppRSZGHlxWiy9kxs8nc5Naq2XO+7L1bX5+fm6O9TpdPyI2ypjrQ1tFLjQWhuCLaSLjRYiAL4MERFIsySJoDV5hmix9E5f4XRvdtLEzGeB/Wzhyrlz51ruQWttxDpQQwCYxBED4jQzCQBLEBIA4rK4iyAxAForYxQtlt7pHziVyT5qjClqM7OwUCwWA3sMAEu8I2MAHLEjB4BVxMoRUQQnEACJyKBLoIQAaKO1MbQ0926+cKo3O2mMmdH870q5OFVs2RMAHOJtGQPAYFYMwFHEyilSDlZwVzpiACQwgruMBy9JK7O/yRVe6MlNGmMu6vb5ynyxWNzhxwA4JFoYlS6wEANgxUxMRCAHCBHlwjYAEok7AWBjqTCZo5VLv80OvtCTO2lipkTR+Uq5WJyq4TgAh0QDhwEIWMAEsHKiBCCQIxIAKWsBEGBY0GUSiKdp5dLvMkPfTfedNMaURX1W/vLTT6er5gEAFvG6OiQigABCREJWiNFFDBIADA/3iWudNjG6dvH3vcPPp/oeMcZcEXWx/OWnxX9umSNEsIjv6IMiAhGA0aUEJOgihy4igcF94kqljaG1C39IH3w2mX/YGLPC6lJ5sVj81434USKy5NW8AjNDhCAAaB/2EQAiEcF9EgppBVq78F764DPJ/MPGmBXWs5XFYvHcRmIcgFXejjcIAcAkuIvwPwSASERwnzghrYTWzr+fHnkq2f+QMeaa8y5VlorFcxuJcSKyyqvFCtQFEAT7BCBARPD/RARAnCStQGsX3k+PPJXIT2htVjk+W14qTp27mZwAkVNe3RskUkQCEdxHmAV3iQj2iQiAOEmKhFYvvpcafjqR/7rWet0l5ipLU1Mz19MTSilWXkMPEpEQCzEAEiIhAHIP7hERADGSJDGtzb6XHHo6njtGSm1Icq6yPDU1c6PnIVKKVXxPF9BFIoohIAGEAMg9+C8RQVcMnCCm9fl3vcJzJntCKbVpU6XK8vTUzGr/cSISiodSkC4ISLoAAQkAJ4J9zIx9IgLAgD1ytD7/rld4zmRPENFtl/68cnV6ema1/zgRCXmhDEoXRMAABAwIABYR3E+YBYAWZySi1covzYHTOnOSiGq254vK1enpmeWB40opQlxcQUSY2JEDICIApIsYgIjgHmYGQGAl9j8ODnW5h7hp+gAAAABJRU5ErkJggg==",
            "iVBORw0KGgoAAAANSUhEUgAAABwAAAAgCAIAAACO148VAAAH2klEQVRIDU3BS2xcVxkH8P937rl37jzuPPyY8WumTYv8SBOrJdBIICRAICGK+qCt0CTpjgUrKsQGsUJCSO2uEqCqsAN2gFigqlUnVVaUQpvSpG1iO3HsOHbjGY9nrq9n7vucj/GgSPx+9NeP7hERACLCmKE1TjAjxQkWWuOB8ERwfOymaWoII2NmpJRCgAQLIaSUpmnSNU8DIAIRiCAIgsEamjGiNcAQzACYWWvtjwwHffcwSZJyqdxYaEhpCAESEAIjRKBrHpsmTBPDIYSAINgSRBhhgAEChMYIEYTAYJB2e73bW7fm5+dL5dLERAUAE7TAiBAQAvTmjY6UhpTScYpCwDBgGxACRNAYYwgFEISAlAhD7vZ6G5vr9Xp9slKsTpUAaEARCBCAAdDLv28lSaKUOvXIKcdxik5+8eHJTAamAaHADGI2tRIkSJAgStK02zta29yu1+tOqeCUcwAUkBAEAQQm0Ld/+pswCMIoKhQKtWptZqb69a+cq9WyRccSCUaI2dIaY0Tk+/7Bobu2uVNv1EsTxYnpEjO0gBJghgaYQGdefiOKwiiKM3ZmbnauvjD74lNPlh0jZ5OVYESwttKEwRjTWh8cutfX7ywtLU2WC1Ol7CBGakAZSFIEMYIItPjjN5TSSikhxNzcXH1htvn0k04eWRN2ihHB2kpjZsYDnUP32trmyspKqVhwcpkoYZbEFoYBOl1//8CnhZf/qMeEEDMzM/X5mYvPrTpZ5CVKCUYEq0zqM+MBbnfd/6xtrSyvVMpOuZjVjIQQC9y9F968vXtjY5tO/exvURTFcSylrNVqC3PVS98/49jIS0wmGBGsbOXzGABmbh+6Vz+7s7y8XKkUy+VCksAL1eEw/fDDTza2d9fv7NLKL95yXdfzPKdQmK5W5+eqF59ddfLIm6glGBGsbOUzMwAe2++6H924s7S0NFkpTk04e/vp3sHx1v3ee++95w4jL1B05pfvuK7reZ5t2xMTE7Xpiae/tVip5Ct561ETYAgoW/kA+H803+/2r362ubS0VCmXS47z8bV7d/a6Gzv76+vr0nYyhQla/vnffd8PgkAI4ThOqZg/t+I0Ggvz1ckvzmSZQawznBCBGSmz1nr/oHf1k9tLi4sFp5jLFt5669/bu+3t+23f9+1COVucpNkf/UEppbU2DMO27axtPlSNFxcXH2nMfWNlRmsQc4a0IKGJE81aq/1O7+r1W4uLi/mcY2Vyf/nzlZ377b3OAY2YWTKzNPvD3ymltNYZK5PLZfM5+5E5sTAyU/3mah0gATa1loaAIC14f9/b+bz9wSc3z58/X6mU81n7ypWPt/f2t/buJ2kCwyLDotM/+ZPv+2EYWpY1NTU1PVU5v7owNeVMlvOnZx2lAI0csW0KpXEU8o2bW5t37/3rxqdPffep+fmpmVphd9ftHg3afS+KUsM0DMugp197s++6x55nWubc7NzsbO1rXz4zWZFOTjgSScKsYYIMYOAnazverY1bu5/vrW3efv755+sL1dp0XghDGBAGoAADkKBX/7nheYPhcJDJ2JVKsVIuLjdmcxmYBlIFIaAUjj3t+2G3f3x9Y293d7d3eNht7zcvNBsL09VJ284YtoRtQTA0oAB6d8hxko5kzYyUMCVlLBCDNYYRmDmM1M5e0G7vd/vedvvI7feHA8/rd1+6dHH51PTyvMkKBsEkZiYN1gz6iLVmZmiTaYRBAyZmJCl3D8N+f9Bz/bW7bqfTGUZJwFYYhnEwGB4dXLp0cfWhiSdmEMewiLOSCWCGYtD7qdaseYyIGOQGUKka+vGnN/fa7fZhf7BzEAIMw4JdisIw8oeB271w8YWzD1cen0OSwgTbxCSIAc2gK77mE4iiSCkVxen6PS8IAu94eP3mXjSS8lBnpZRCZkSmFAZh5A/DXr/Z/M6ZR4urDaQKhoapYRjQgCbQ65/1WWultOcdRVHkB9HGTsf3/TCMvUBbliWMTCrLSikW0sgUgiCIh8eBe3Ch+b2zp4pP1KEYOoWO0D08iFjEZNJXX/mQx7TWDAYzU4wTzCJhZs0yTh0xYggjYwVBGIe98OhOs/mDxx8uPzkHBXhD3XfV22+/3U1zPS7Rl371PsAMgDHGTAkBDIZImMEwlCoRSBhC2pkg8OOgFxxtXbjw4mMPlc/OQDHcfnSw77darW6a7aFES6/8A2NEhDHBICIQk4gZIG1QWiQiwzAs0/SDIA56gbfdvPDccr20WINS6PfT9n7Sal3uartHDi2++g+MCSEwZjBARGAyYgDEktKSIBJCWJbl+34U9EJvu9l85nSjdLqGFOi7aHfQeuedA2X3yKFTr72PB2gEZDGNAGxQRCMsZVwEIISQUvq+HwW94Hi72XxmuVFaqkJp9Fy022i1LndTu0cOfeHXHxABIIwRYGoiEIgNimiEpYyLAIhISmM4HIZBz/e2m81nVxqlx2pQQM/F/TZarXcPVbYHh5Z/+wEIBMIYAVIZOMGCFI2wkGkOABHIROCPuEfuvWbzmdVG6VwVitE5wt0uWq3LPZU7QolOv36VABBhjBiGEnQCghQAYiHTHEaISSIMw+Gwd3iwdenSc6uN8rkqFKNzhLtdtFqXXZ33RIXOvf4xEeH/kJbMTGCDFU4QaZNGoAUlI4Oh2+5sv/TSC2cb5cerUAptD3e7aLXePWLn2Jj8L36Nd9m13BTiAAAAAElFTkSuQmCC",
            "iVBORw0KGgoAAAANSUhEUgAAABwAAAAgCAIAAACO148VAAAH/klEQVRIDUXBWWxcVxkH8P93zrlz7ZmxncRLGjtpmj1sLS0KArUipUWIllaIF4R4gQdUpSkIHnhAQkhFPFYgEBKVQFSI0oBQszRkbbqQqqIllZKmccZLHU9ie+xZPcude+/c5Xwf40iI349O/vsaEQGg/1FK4S5rLTPLXYahBERkoVggIgNpOY2CMAxLH/9DuKe1nhrqOUYbY2gpskQgAhFECHeJgBl9sgEiUKkQCwszNKgPblyJQq/T6VSKJwixMWbnptRxtOM4NGvFWraW1QZSihwFBkQg+D8tAAuzZMOWllQplU8XS8u3rl279sDe+VyWXNcd4lm2ibWW5kW6fhgEvSDwldJaK0Uq47oZx3HcjFaKiABoEYgw81DUdsRqrbPJwlJx7sqVK1+8fyk7CGNMJrzWC/0wDOn8am91ba1c2aC1NsYMDw+PjY9vGhkZHR3dlKUBh0BQDDCslYxqERKB5DoLd27Nvf/++1895A1krIg0i5fqtV6tFtPfZxuLxWLxzp1SaSWOY2YeHx/fuXPn1rt2TAwPZTMEKAEEfY5pa2W10kPdxcX5m++8887TDycZkyZJsvTRybXVpFQS+vGxd0vl5lq1Gcexl1BoSSv1mf379t03eejQ1LaszhklwACzghDRILEmKEVZf3VhrnD58r+e+PJyxsSptcvTb83OhjOFiL7+y5c7AXuB7fMl02PDzA8e3Hdw99Qjj2yfcJDTAGFQ2ABE5LJokFKUDcofz958++23nvjKbcf0kiRZKbw7UwimpyM6/LOXBocnMtnNc3NzgZgeFLM8eGDfwT2Th7+0Y6tCXkFr5Fg0QCAFISJFGAhr8zPTb7z55jcfu+3oXhzHpRtXZme7hYJP3/jVa1M794zeM/Xqq2c8i8ACRA8e2Htw99Thw9vvIQwpGIMcwwggAIEIpOCG9dnC9KVLr3/rsSWjwr5y4fr8XKdQ6NJPTi2OjG8eHMq99KeTrTjtWusY55O7tu/asfWB+/d+aowmssi4yDE0QwREIAIR3LA+c/Ojixcvfudra64TpUm68J/3SiveSqlLP79Qo4ybkj5+/Ew7SX1rjTG7t41NTWzZv3//F3YN7thislnkBZrFWqEN0JrcsF6Yvn7hwoXvPlUbcOIkTSuFQrvltdtd+v6fbwax+L30xvR0QBIRrLWTI4MTI/k9u/c8+umJ3ffk8vlMjkSLWMtWKVJkDA30vMKNG2fPnT36dCmjwr54pZzGSZqm9Njz5zw/8oKYmXuaIiW+72/Nmi3ZgcnJySc/v2vf9i35oWxOWUPCzNZxoEhrDPR609evnz59+kdP3Vbc7usV18QyROjRX5zo+okfxpaZjGFFrWZzfGRwy3Buamrq8c/t2TW5xXUzeSc1SgDErmFFROiW69PXr58/f/75pxYNt9qt9tUzcza1ENBPL81Xa91qzVtaWrJJatPUWnvfzm2T28b377/vE9s3jQ4PKEW5DBuFvq5DgU06gbc2u1KYLly+fPmP3151pdNqtc794UYcsQjoTF0Wb1dv36kWZmb8IIyjZGRk5MCBvffeu23v3q2jWQw4IAIRBICgFkq94y2vLVdvVYu3Fq9evXbimVuDaPV9eHY+CjlJQR/0pFz1yrXOnTslhoLS+Xx2bGJ0ZGRo82Z30IHREEFoYQXMWCx55ep6sVjs1L1KuVosFs8deS+vW57nhQvrNpE0BRUSGyboJdLp9EhnSDtEIAfQ0ApGoy9K0fYRRrbb7U0vVGqNdqPREN9rN5urq6tnn31jk9uJoijvxRAIhD72k5QkJbHWpuxYMQBSA1bQCokgSqXZ4ZVy0GwF9fr6/HIU9FhE3LDitdfL5fLZ546P5rrMPBz5EAFAN4PUEixB+kgJVGoRWIlZhO3qetrspI1GsFJueJ7vB0EnHrSiQOQG5U5rfW1t7cIPT43lAmYejjzIBvogEgswIAKlIZBmKynVe+udyPO8pUrc9OIwDDq+TRIWEesY0toYbbyK12pUKpULPzgzngtYeDhsQkSY6W+L7RSSEkSEmayVesNbKbcazW6r1epEmShVImJhBAoiTEpp7bqu9la99nq1Wjv33MmxfCDM+aAJZhahJ1/8oEccEbNltkasiqIILMIsIpZcKEdrzWRB0ocwNUq7rquCVb/TqtVqp44eG811RSTvt8CWWejhFy5HGokSgMQaYcUsxCICQES5UEZrDS2AALB+pEm5rmuCcrfTrNVqJ5/961iuy8xDflOYRZgeeOG9WFOqFSkSNsIKfUwEAkBKkaINGkLoEz9SQCaTMf5at92s1WqnjvxlLNcVkWG/CmYRoUO/vRorihX6WLSIAkBMBOoDAUQASBNIWITCRAkcx9Hd1W67Wa1WXzvy8li+KyLDfpWERUCf/fWHqdGpUQAYzCQAlBAJ9QkR7hKwQITZSZQS0lobb7XbXq9UKv88+spYvsvMm4I6hAHQQ7/5KNEq0QSASRhMREoUCREBpEDoY7GA9DmxUiCllOmUvHajWqmePvrKWK7LzJvDOoQFQg/9bjpVZImUUinEQpRSJARQnwAg9ImwCIvIYAID0n3NZa/VqFQqp44cG812rbVjcQ3CfXTo9zMpUUogIguxhD4SAqhPABD6RBjCLJJnZaA2NJe8ZqNSKZ945tho1rPWjsW1NImTJKFDLy4IINigBUogGxgQIhKCYAORgNBHJHSXbs92mrVKpXL8e69vHvCttRO8FvXCXq/3X3n1Xm1bORIjAAAAAElFTkSuQmCC",
        ],
        5: [
            "iVBORw0KGgoAAAANSUhEUgAAABwAAAAgCAIAAACO148VAAAHT0lEQVRIDW3By3Nb5R0G4Pf9vnN0jo/lOJIcy3aw5GscEhLfFmXKbbron9NLyKLtTDftgu7aLU0XTKfQdtopIdBOZwrDJWCDmRDjFBhsxZISIjvxXbZ1OUff+X6VPVNqpjwPX527ncsNns6kd0zzpZde+rxU+2o/o7W2QROe0VqnEgO+G7iuO9bjX7o0Mj7am0uj2jRKscPVIlAAcUQreEAHwFfnbudyg6mezAHja9eufVqolrZPeZ7n9yhJtCqVSto7i1hFUTR0Wk9MTIyPDV6cCDKZTOAlPIcAKCCOaMIjOgD+6YO54aGhnt7elnbeefe9QsXcq6a6upLid4TWPqhUdr9q1A+ajUYj0djs6enJD/Z+/3ujM9O93Z0JbdFGCARtdOgSPsBXbt4cGRk5k+0LoTc3N2vQTb/rlK8aZM3IxrZ95drCw0rVxEZV14IgyPZ2P/vdx5566kI2HXR5OCJCgKRoOIQH8Pdv/2t8bDw70BcprK+vxYnAzfTZGEqJgdmJ9n77m1sP7lWNMXHoe57Xk+l4+pnTzz31+EBXZ8rgawK0XCgFh+Dv/vnGxMREuidzb3NtZWWlSV+d6jexNfowknqtWfvHXzd2t41WmjaVTCaz2eTTz3Q/+eTjg8mg1+JrIghdEYJtL15/+cKFi6cyZ+a/vH9naalSNY9MJwQRlaESKw/v+61IKyrthtlsdjDf8+Szpy7k+/NOIh8JgFhRCKVQd2AAseCvX35xamqqp3/wk3s7b7755uqjWiXsABCKG1klIo7NEz6A7rTk87mR0b7vPNeZd92+GNkQJGMFSwgQJXDYbO3tN/mrP/x5dnZ6MJ+vNuOFj28XKrurW2Gj0Yh1p9W+1jroyLiOp7W+NNWfTvuZVCLXox2gw9oua0mKKGtpLcpb5tH2/ubmNl94+frMzNTwyJAldnYOdxqtzdDEsYkdpwVdr6t0WvuucsnuVKA0tIavEAOe2KQYAGKdVmh3dvYXPt9Y39zb293lz195d3b23Phwf5eB1oiJSCGKRFxYhXrTuj5IgYjErggEUIIj2sBpAdg7MFvbh4XC6q3Pq9WDlrWWP/3jwuzs0MRQb08EY4yljZV1XQ8ORMNYWzOhFQEQR50k2pSgzSaM8aOtra3yg937la27d++W1vxYPN/3eeUvc7Mzo+eHs30xEFsr1lCMuMoFFWKRMDKxAIJO1222EBkxkW00mhu1xv2D2tra2ldbB5t7h7V6/bDVTeW5rssf/O392emxc/lsKjLJhENFC2zvIeHDcSCCVgvWirWQVn1rd39ru1p5sFGr1R7W5d6hCsNwP2ZDlNa6pbtABwB/+OoHs9Oj47neoBkGXiKMuF+TUvmRqCBGIoyivVqtGbVM68jhYe3wsLazXQ3DsCFOnb7jOg3Hi7RD0qhAqEDyR9fnZqdGJ/K9Z+K4pbi+0SiU9peWlmom3TRJE5tqqxmZVhzHOEZQqYSIWCpDRaqao5tKi0hLe6ACwB+/Nj87NXI+n+0H6jYulLZu3b5/5993dtTQoT5DMlYEAdIYQ4BUjuNSkUIKBKgpGyqICNpIRfL51z+avTx8Lt+bAVrWLhfWP1xYXVlZWQsu7Xh5ANZaACTjOAZAUmsCcKwkrABsOFGkYhFx40hRlFK8+saHM5dHzuV6TwPG2uXCw/mPi4XCynpwftc7C0CsxRGKCAASigSgRRxBW6hbRsUicKWhKVTk1b/PzVwaHctlTwHWyvLdR/O3yisrK4+Coap3BoCIkMQJStBGkDgSayPKAnBwoBCz7eobH8xcHh3L9SUh1sry6ub8J/cKhcK2nzt0MgLECiBxRAACUBAcIXHEKgtlSTrSIAQEn7/x3vTk+Fi+PxCxNl4ubc/dfrC6ulp3zoY6JSINFzFBUqwF20AISQFAHBMSSmltBSIA+PyNd6YnJ0bzfR0iYmW5tP3hYmV1dbXmnQ2dtLUSarEESREBQIIQkmgjjhCEKKVoBSJihVduvDU9eX4kN+DJkUJp96PFtWKxWA0G6omUiOD/EBYnKIIQkgDkGK/ceGtq8vxIbsCzFkChvLuwuF4sFqvBQD2RwrchLACBQNCmKARIAhARALxy463pyfMjuYGEtQAK5d2FxfVisVgNBhpeGieICI4RFicQQghO4NXX3568PDGSH/CtBVAo7360+LBYLFaD/noiBYAkjokIjhEWJxBCSBv+iz+58f7k5Nhwvi8QC2C5XJ1f3CgWi9UgW0+cxrchLE4gBGLbeAwAf/b63OXLo8O53gAC4MtydX5xs1wu7/lnaoluAEoRIP5HCAEgxwAQQkgbv/aL658+8cRjQ7l00o0BfFGu31zaK5VK1SBVTyQBkMQ3USxJACICgBBCALANBMFfvrZ08eLZoVy6K2EBfFGu31zaK5VK1SBVTyQBkMQ3CEVIigiOEUIIjwFg2wvXv7h4sT8/2N2VMAA+u994585BuVyuJdOhnwRgrRURACKCYxRLEoCIACCFEJI4RvI/Du36mRW+NjcAAAAASUVORK5CYII=",
            "iVBORw0KGgoAAAANSUhEUgAAABwAAAAgCAIAAACO148VAAAHe0lEQVRIDU3B228cVx0H8O/vnDmzN19m413jOLZzgzoNbSpUtVBU2ooXJCNeeOWPQVWFQFVfzAOgSryUB97yBCoPqKWi26QisXNpHdt1o8S3nfVld3a9O7Mzc87vx9olop8P3dz+YrI66/mV/YP9v3zweavVA5BhDGeUUjMzM3Oz01cunp+aCgoyKGo7MTExXiLfwBiUilCAAggQgAAC6Ob2F5PVWe1Xmgf7f/zDP/f2OgAyjAFwzIN+f3ykUpgoe/MLC9evTF+cPVetVs9N+OWSKhQw5oFwSgAGNOADdHNnNaheMKbSbO1/8ulxt5sDiGIlIlmWtVqtKIqyYd9l/Wq1+sarL9x4/vLU1NT09Fi5ogsFBBoECMCAxSkC6ObOarU665lKs9W0MgPxAQyFAPT72f374f7+fvsojI6baZq+8er3bzx/pVarzc4GlTGvWMSUAuGUBVLAgRlMN3fuV6vnC8XxqBcp8UkUAOESgG63+8knjd3d3U4n6vVO0jS9dv3G5avPzc3NXZitV4Pi5KRX0/iGBXLAgR0c3dx5UK2eV7rw+Mnjncc7wyTFCJcBZFnWbDbDMDzpD+IkZcevvvbW9Rd/MD1SK1UqulikMYIAAgiQwwFCAP1182EwPZta+dvfP7y/1evHFsBQjQHQSpcr5ePjY85TD04Ev/jZWz98+YXJCS8oka9JKWjAAQxhMIMdkIvQB1/eO3d+rhdn77//54N0KmcPwIAqAIgIwMnJiRZXMhj51S/ffPNH132DEoEAFjDYMTt2pDjLs36ctAd9+tOtr76zUPeKxXv3dr7YOxxkOYABVfBMuVwu+apSwPj42I3vBZdmKhWCApxILpKlJCzOIU8lDI+3jloPD3fo9x9vXbhaHwsm9vb40MWZOACx8vCM7xujyFeoVMzMGM6VUAAYsM5lztnUz3OXxHmrefLkyfbawe7K4Ta999Hu1StBtVpuNl0wrT2fADiFbzDDOrBzzLnvm1JR+YY8IIW1bHO2cTTWbnePDrtbmwdRFD3uhA+PntK7H7UWvzteqxabTVcuO6UEgCmwOqO1p5QixUKslXKKnCJAcmSZy9I8fbIhe3v7rfC41cyEeSdur5006Tf/jl+67M8Gqhnay1WUPIw4TxQRk4qVIk2OYAkEHMaum9hBHMfDfpzEgzjefRp1u93+STzok1bqkO2TPKV3bssrlzA/KWHoXqqj4okwR7lyzqWMIyoKkAOpYORpuxNGvSiKur0ojuMkSXq9nrXWWbG5N9LTpqV9evs/8tpFXJ6UZmgvlCzZNEmS9f0oGbEYlGZYxInKYZjdUT/pDuLBYNAbDNPM2jxPs5RZAFHK8zydFLyopOnXK/KTeVydkPDAjiGNT3rtdvtfq4+iKIpzQf05EXEwFkVmTq3JrbLWJrm2jh2zcw4AKRgfWuu0hMGY0Nv35PU5XJ2Q8MB5yh0dtbe39//x2eowTTPWeXBJKcXkWypBYKXAMCLIpcACZgEEIFJifFZKWd+m5SG9fU9en8PVcQkPHRf0Xtje3Nj58LNVY4yYUs+vA3DwLUoARJWFCgAcSiANgIgwQqx0SkTsxWy69M49+fEcLk1IeMBcVrv7R2tr2x/f3TLGiCl2zbRzzorK4RGRaB/KAATtQ2lFBCKcYkECwMrQygn9dlVemcfCBMJD2Aqe7h08ePD47nqklGJdaHs1EXEklkBErAFNGPGIFCmlRASn2HEqInmWp8Mh/W5VXp7H3ATCI3AFX2/vrqw82tozAHIyx3pKRJjYKiaQeBANAoknpEgphTMinNtUIDaz+TCjd1fkpXnMTiJsw5Xx1eOt27c/P8mvZ1k2dNT2aszsYK3KALBmUUxE7DEU6H8gAuesCCQXzpjeuysvLmBmEmEbXHKbX2/evn17iJezLEscdbwaMzvKnE5ExJGIgghYs0AEp4hAIBEBAY5gFS3fkcWLqAcIO4Afb3z16NatWyj+dDgcJpY6piYirPvW64gIw2fRIuIgjlnO0LdAPEKBlu/I4gLqVYQdiD9Y31hrNBrFYClJkjiXjqkDYD1wpiMiDJ9FC4sFswifERH8n0dSoOU7sriAehVhB+LH6xtfNhqNYrCUJEmcS8fUAbAeONMBwPAFnog4gIVFhJnlDDOLCMQjFGj5jixeRD1A2IGYeH1zrdH4tBgsJUkS59IxdQCsB850AAgKAk8gokgAyBlARNg5FgFrEZ+W77prC1QLKOxATLK+udZoNIrBUpIkcS4dUwfAemBNBwCJIXgAWBEI3+ac4xHRjg0t37XXFlQtoLADMcP1zbVG49Ni8PMkieMcHVMHwLpvTQRAwRA8AKKIFI3gjIg455iFRTsxtLySLM6beqDDLkTZ9Y1HjUajWF1KkiTOpWPqRCRqAC+iERCBACil6BmMiDg+lYkakqHllWRx3tQDHXYhyq5vPGo0GsXqUpIkcS4dUyciUQN4EY2ACKRGSJGiEQAEEggzC3MKPYRHyyuDa/N+LfDCLkTx+sajRqNRDJaSJIlz6Zg6EUEPxIuISAuUQD1DRACICIBzjplT0jF5/wUFzdnY255gQQAAAABJRU5ErkJggg==",
            "iVBORw0KGgoAAAANSUhEUgAAABwAAAAgCAIAAACO148VAAAIHUlEQVRIDSXB22+kZR0H8O/39zzP+77zdqaHaTvTaacn2Iq7qIAgRDxhohfExJioiRdeGC4wZEnXliVuICQmGhP/AhNvvZELjBGE5SAgqAi7gt1dWEDcEyuw3Xam7XRm3nkPz8/Z9fPhk7/9Nq6ztLiuMDmuUSMZhggvBEAooSRVrWrsvS/AVKUoimR/pLszmiRJaXS2ND7Ps6+tAyApKgAU6k2Ba9SaHEOkGgIgYQiSgFONvfcFmMMqdHsTH13Qubm58enl8foBXnnjAEASzitJEEPee/XeoAjDkNQs7RAk1UJFhBTQkACpIiBOf1B55a3qfffdZ8tNHza48cfAiJGhLAOgqrjOiExNjI6Mjlojaa8FgKqiBYZIBYdAKgHw9LmxVzZqhw8fRtRIzTR//l2SuEYBVRBCUBCFwYGl+cXFxfFxa8P3ARAqWgAgSE+KgACvOXkhfO7MyNGHjxZututrfPBuDKlCPf4vSRBFGK24W25uLi8vT1RdXHmfQ1DRgkOgqHBIqGQQBG+cd0+fCh577LHMNjr5NO87BGuNtWasCmudscaSiiIKedOno6XFpbGxEUoHAFUtPEnQKJ0IQQEtiBPnefwMjx07VpSWu7LA9burI3FcLo8c+PREHMdhGGRJT0RcoJNTe3EcO2eEmaoSatWTVBgvjiQoCgPgxAU8c1qPHTvm4wN9t8TV24MgsGFoazNRGEYuCKzYqFSKY1NvpnEch4ENghyEQA0UBGgUAQlQFJbEG+f16dPFo48+6kvLfbPAHx2ANTCWIxU6R+ckjEIhXYC5Ja3X6+Pj5ampSEQIFXiSoAEDAhADWgAnLvinT/tHHnnEl5Z70uQj91hQCZRi20/yQVL0u6Kq1urMPGZnJ2v1sZs/UyMphKAgBTQKx2sMxALY+JgvfSCrR1YHprnn6zz+szuzLMuvSVut3Xarc/liN8sAYrSK6elwrln5ytdWOAQVeJKAUTqKkEJxAE5f4cvnuLq6imgpNfO8+LvvD9I0HQzaOzubm5tbV1vnznb7/SIvfFxBrVaanR/96jdXoAC8IOcQhHRCoVhKQPKt/+rzH+j6+rqpHCiiJZ7+zdeT6/b3e5ub262tzuX/5EnivWp1Co3G5Oz8+Be+1vTeA16QcQhiaIVCsZRQRE586I+/Wzz00EN9O7ejdf76B7X8uiL3STIYDLJuB3kOCuqzmG3MzMxOfO6OhlcFCiIFQZA0IkKxMCGJExf98beLow8/vJ1PX+6O86FbAIUC8CABwgucYxRJc3FkZqY+NT1248qE915RKAckAShgjKGxagOSJy/442eKn/702CdJ9dxOhb/4BlShCl+AgAjLo1EYSRy75mJ1qFyJxyaseu9RqKYKEABpjKFxagOSb17k82/z6NGHt4r6pV6Vzzw67RWq0MKJiLWmNj0mIsZIPOaiKLLWiPGq3qv3Ph8CQNJaC+NgQxHZuGRfPBusrf1kUFrcNU1efuIWr14VqiWhiDFxPGLEKDRJ9621JDXPoVCo9z5NU1UYQ2MsjKOLhDx1ma+8y9XVIxi9ISktcfvPn/NDqmkeKABVY4y1lmS327XWCoQpjDGEqEqapgCstcYYiIWLjDFnPsz+ejY5fPhwVrmpG63w6gu3q/deNckCr34o95kvvEIjFyighRa9tFQqWesMbZ7nII111lmaADa0zr59afDqO90f3//jYvRT3ehGfvzsl1TVqyZ56L3Pi3yz/UmaZgTqk1NFUeRplu33KpVKKYriqKSqNEbCKAxD2gCuZK09c77zysb2/fffX1Ru6IaLvPDUXdk1ucek9z5JBv/cOCtigyBo1icG6cDnObO02WzGpRI0E0oQR6NTE845sQHCyFq78e/dl05uPvDAA3l5aT9Y4OnHb221Wru7HfiVLM16/XRjo99oNCarY+OVvN1uZ2kyEurBgwdF/Llzp8rl8uRM9cabF61zcEYDO3Tqvb2XX9968MEHs5GF/aDJN3/72Va73W7tXL4UqyLPcenDcK7ZrE6MVuL0kytXtMiWF2rOuX6/c+Hi20tLi82l2UO332SdpTUa2qF/nd198bUrR44cyeL5jmvwvSe+vr29vbXV+vurOwC8SmtvqjJaKUWBtd1OpyOCheZ0r9fLi4Fy/7bbbptbnFn4VD0IAljxltbaf51tv/DaJ6urq0Xc6JlpXvzTt/b2dtvtveeeuZz0k2RQ7A8aJIEi9zuBC5wz5RErItaxVMahQ4eaC/Ubb24452ColkEQvPnO9nN/++/q6qqPZ/uuxktPfa/f63f2u088/l6rtbe3lwy0MT4+HkZhO2mrgqImgIgYZ6Kyu/XWW29Ynv7MoaqqukDi2EWl0utnPnrqL/9eW1vTkbkkqPOD339nb2+33d578g/ndnd7+/t5L6+WR8rGmXa/Za0No3CyNtZqt7M8tSHvvOuuQwcXvvzFZWOtMWqsTwfpiXeuPPuPi+tra1qeS4I6n/zVnVevXt3c3D61sW+MobgcDeccqK3uVqVSCaPQOGxvbyeDXuH7d9xxx00rC3d+fqVWq5ciE4W+3++fOHv12dcvr6+vo9wYhDX+8odxq5XttLNOh6OVclga2elWirxI83SntzVRrUZR6JGlWZrnWZbvz88vzM9NffZgc2VlZWK8PFktJUly8t2t5098tL62hsrsIKzxgXsgAmNkYmKlUqlYF71zPlFV77NO8nG1Wi1XgtJ411qbDvLtzd2dnd3Iucbk5L333tucq83UK0mSvPV++6U3r6yvr2t5dhDW/weBmdustcwy7gAAAABJRU5ErkJggg==",
            "iVBORw0KGgoAAAANSUhEUgAAABwAAAAgCAIAAACO148VAAAHbklEQVRIDSXBy49kVR0H8O/vnHPvrXc/pl/TzDAMA5EBTNTFJCYkbtxoYmLCf+HGlX+BgQ0rY1wRiXEBMSRiXJigEZFHQBCBHume6ulndfW7p+t5q+ree87va/X4+ci77/5Fk7LGZZLqhmonImLDjLAMgEKAIkISAEloRg2qGkJBaggBfgIQQJ7n4/F4NBpJt/toDDOBAYTRiG4CIMKcYQVTBgABGGMAqKphAShJVQ9wCn4CEECe58PhsN/vy+72+zJ3Wxo3AdAooQRDmCi9tVbHE0vM1GbTSerVCxA5bw2MMUBBKKf8EFAAk0nWbrf39/fl/td/Tt3yKFoEoBIgSlI5URYiYn0ox/Hq6mopLjtjBWIwhihBIFdqYEAYAgQwGo0ODg729vbkzXfebJ7bnUuHK4FQAKopUDjn6rGdazReeP7520/cmKlVy2VYEKQqjSlESADsgwogHY1ardbe3q789q3f/Xs3W2tlvBIAxZURkXvvl2fq15cW79279/QTN+dnarOzsRMIQEKQA0qC7AMKYDgc7u/v7+zsyGtv/PGLVrh/pGaqakzJRFG0UC0q1g8Gg5W50vL8zN27z919anG2XjIWCXOhkgRJJakIFhQA/b7f39vb3t6RV1//05eH+OZE5ubmFm4tzK7MXrtWmY99xYQiz6/VTKMSLS42VqsucZIjJMyM+qAqFKoGVfoExFQ6wO7O3tb2Q3nl9be/bPO/J6jX65XlWuVapdGozVhfcaxUKjevNxbmyitLlZqGCISRmngD9SEg5NTHfAwIgCJLtra2ms2mvPL6Hz7bK75qe+ecLytLiOOkgnGk+dSN1cbK4syzz97+9s2Fa41qpVKZtTBQr4F+TFWSGhIQU9Tqw82H6+vr8uu331k7sRtnJqhmUSicOudGZyeTXi9N09l6NNuo3r791Esv3rq1unjzxup8IlboA7XogYEiCAkgABgqzebm+vo38te1tb1xrT2uiDCLmFuQsvXVxkmr3ev24EfOsNFo/Oj7L37r6Rt37tyeS+CAoGDoAAFTWiIEgM/iZnNzfX1dPtprX9pGz9ZrNRQWOZCO8cWHX+829x89euTHQ/UFyZd/eO/us08+c+dW3cECJChDQAEElAEBMOz7h5sPNzY25I2/f9iP5tNkdmlpxhvk1POL/oOvNg/3ji8vL8vOOhFV/ckPvvvcnRt37tysCgyumGgCIYAcJUIAXJ6nm5ubGxsb8rPXfnOORkdq1VpNxQYiHY2G3WyU5r1ev5GYauxq9fpPX/rOM08urazUywInEIOQ5DAUSM4oUDzx9ecb7YOptrz8i18e+eTERwCImHQiQlQpCYDFqputlZaXl3/8vTtPLtVrNZQNnIExyEtBBBZykZtRpqNJ+Phv73U6nW63Kz//1e8PJuFwEgBYxkIrIklUjaJStVpdnavON6qLi0svrFbnqy6OERsYgQjSGEEA4n476/TGnc5ge+3z0SidjMfy1qfbpyE/C4UACU0EY61Uk6QURc5Fs0lcSeLZRqUeSWxgLQwguHKgxVh95sO/7g+PTx+dnJxMLg6oSqWsdfJ+ZAbOWkEMOMBYJAqnIGGhTpDEoBpMCbxCFSS+uex3hmmv113b8hePehcXF1HeMyLGiOxP0r6NBi5ygAMcrlQ8E2KKogSVWjjnaXKPy1GRFSHP/X92T88vuxfn5/vnpbzQEEJSDEQwJa1Jr+uSgU0sEAMOsEAZiIipAvBBx5NiCDf27KfF/lm/n47TNN3YPUjTcZZnvXxZaQhUw8AAEJGD/LRjqz1TtUACRIABIsAQSgxyHeXa74+bneJRmg0Gg63Tfm84Gg6HfjiyYuI4ntibPoj3fi70jTx2kF8c5/Fp7rLJBGpAMSIICtXg2R+HUVYMB4MHJ/3OcJJl2WlaTIpQFHnZxrGLklIp5XJeaJZlSzIwAiMirWz4ZWuw1hocHh52iyT1kRjxeRG8V9Xcawj03mswqiDpoxjmCikCMcZkWPCB3vtlObICY4wc5OkH66efNs/bh+2eL6UhjpwTAMRUUCihStJQRVW9czDGWksYAxFjMl4LCg1hMbSsEWuNtIrR+xudj5uddrvd9aWUSRzHCAAxFQASSqVyCoS3BtZYYwgrEIjkYU4Vqno937EG1lppFaNPtkef7aat1v6lT9IQl0qlPCtCEQgqRAGQqkoSgDcGRowxoIUIILmfVYWqruQ7kRVrrRz48T8fdD952Ds7O7vU0kBjVSUFxFQhogI+hsdUjPyfWhAki2KGKqp6Pd9xVpxzcuDHHzR7nzzsHx8fdVFNkagqICBUWRgoQBLEFQHFADJl6ACoMs/qSlC5nG1H1jhn5djn72/2PtrqHx8ddVFJUSLpEAx0KjMSBKpqCMFjxgJyBY4ESR8aSiE513/gjDjn5FT9P5q9j7a6x0fHPVRTKZF0CAZBlZlBECFpCMEVihEYMSJwJEgWoUFCVRfSLaHK1JnyvWb3w63u0dFRX2qplKy1kU4sfQjBqhEKyUxEBSKiRigQkWBAgGShjhCSC71D6JX/Ad3sFxyXFPG5AAAAAElFTkSuQmCC",
        ],
        6: [
            "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",
        ],
        7: [
            "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",
        ],
        8: [
            "iVBORw0KGgoAAAANSUhEUgAAABwAAAAgCAIAAACO148VAAAHkElEQVRIDWXBfWzcdR0H8Pfn+7vfPbTX53Yr3UNhSJG4SSBUyERlEjIkyEgQF+IfJoBG1D9UECRsEEKYSgCJLpphNhJiApEYzEIkwTBIcCPIZO1aYA/suq5bb23vrr3e3e9+36fPx/s1mGzx9aKCNuPj4/ML5ZFNo2EYTp0595/xT/fv32/zQ5Qf6OnpiYN8vqtnYGAgjuOTJ08ulRc2rO7cunXr6MjAdcMhgBSgkFD4HBW0GRsbny+Vr9w0Gobh1JnZj45++vrrr1dVt8/2DA4OmrAzSGczmUx/f3+hUKhWShtWd95yyy2jIwPXDYcAAiBAggBCgk44Mz42Pl8qXbHx2jAMT0+XPhqffvWVV7uHRtp7h0ipOHSpTLqtrc1l150vnm/Uq4M96a9u3vyVK/s2fyEEEAIBEjlAIUEnnBkbH5tfKI9svDYMw+kzlbGjM/teemn4qtGBNZcDaIZWFIiojsFKpWziaHV3ONoy0vO1kQyAEAiQyAEKCfqgXn3//ffn5hdu3HJrGKZmpuvjY6V9+/bec+9dN265HsCi1CyciPzlb3NRFFkb50LT3dP9jauH7tlyKYAMkEIiBygk6M2Z04cOHSqVK7ffuT0MwzPT9aNj5X379m3b/p3RzTcAKMspK5GI7P+ns9ayN2HQzOVyW65Z8/2tIwAyQAqJHOAcN42n3797+OChg/VG9NOfP5hKqbNnlz+eWNi9e/eXvrxp/fphAFH6nCctkHNLmyThSOqk6Obrhu+7YxOAEEgh4TWaka43GrTjtbcOHjyorXly164gwNmZ2scfL7zwwgt9fX35fB4Ady4i5ZRSzeDrRBQopNMawDdHh+/fdrUDUgxiMAPae+ed97T9qb/Ozs52dOYe2fFDRYi0W2rYyvxMpFFv6Nli8Xwpch5BEKjsqmwmk8uGXe3I5zu+uLbz+iu6BRBAAAiykIZFWQvd9OMX4zi+ZKj34ce+RwRHsIT2EFnHYvxMFB0/xQuleqVS6V+9qr0915HPDHShvT3sy6qhjALACkJoqRks1U1pKaJr7tvjvV+/rv/JnXcDEAVWCAlpIABiYLmOubI5ez7uXZXLZoJsWnVkEIbIEToELUJwzMvL8eS56lylurhYpY33/pmZL13f9/TOu9BCkAAK8AADAjgLrdGIEbZBEQJChkCEjKCN0SIEbV2xWH13cursXKlSqdBV9+9xzl22ru/5x7cDIEAJCDCBOAUCaQ8GBHACYQiDHECIa3G91KhWq8txtBxFc3PlEwtRNdItdOUP/uScu2xt3+7H7wFADOXhPUeh1sqJSMOJZ2Fm3exmFvbiNVoWS+XZ6XPFYnGhVl2KanEc11NdotIqULThR3utsRvW9uzZ+V20OCHD1rpCdX4uqgpLNYax7JzTUZ5bvIgOBBJHjVp1qVqt1mwcOwMgynYjCFUQ0PoHXjZGX76mZ+/jd4lAjEfTGWMOHT9+fHaWmRebgTbeWqtjYWZhCnyWiNQKEXEKXgGE5VQbqwAArXngNa31hrWdLz5xmwDeiGuKtfbAkZMThfPMflnnjBNnnfNGBBCElCGAVCIVBBIoUUSEKBRPaKHBB97QWl+2Nv/HJ28SiLOBjgPn3NuHixOnKszccF3WKe8dVASAACVoERFmVkqBCCtcpiHKA6BLfvIPbfSla/J/eOImAZwjbQJn3dsfliY+W2bmhu21XjnnEc6jRaCEsEJEqAWfc2FdyAOgtT97S2s9PNT+3I4tAlgPY2Gtf/vf9YmTTWZumAHjA+8Y2eNoESIJsEJEqAUgJHxQA3kANPzQm1rr9UP5Z351swDOQ1tY6w4cXpg4VfXeN2yf9co5lrCMBBEHuABBCAkO6iAGQJc/+vfYmHWDHbseulUA52EcrLUHjpyYmDrHzHXXYZ1y3nnpRYIUB7iIBwlaKAYxABrZ+UYcx2tW53c9uFUA58U4WGsPHDkxMTXLnuuuw3hy3rN0IUHEKVyIGBAAQgYkAGjjU2/VarVL+nO//eW3BHCejWVr7YEjn00Uisxc853WkfPOUxYtoiBpXERAAsATBAm65jfvLC4uru7NPPPwbQI4z8aytfbAkc8mCkVmrvkO65TzzlMWCQJncCESQAB4giBBo8++tzC/0N+devbRbwNwno1lY8w7Y6cmCkXvfZ07rVPOO09ZJBQ4jQuRAALAEwQJuuF3h2Zni32dwXM7tgWA82wcN6Pmu+NTk1NzzrmGdDpW3ntWORFACJLGhUgAAeAIgACgG3cfPnNmpjtPz+/Yliaw98a65drye0fnPpla0kY3g6wHMbOXNiQIEuIiAggATywQALRlz8TUVKEjy88+dmcugLA3zi1WKv+aLH9yuhrHsQ4zTEpaKC8JQEJcRAAB4BVjBW17efrYsWOZIH7mkTu6cxBY4+JisfjBJI6d9vV63XdoCZiIPLqwQoRwASLCCqecQADQ3a8WJycnU4h+/dAd/XkAVvt4Zmbmw0/U8WlZXq5Kl6UQSimPLhFBQuF/iAgrlFJGNEOo5e5X5iYnJ1MSPf2L21d1QSljfbNQKBw+1nFiOrW0tES9NZUWpRRUn4jg/xARVlhokBDRfwHITauPo4GyfAAAAABJRU5ErkJggg==",
            "iVBORw0KGgoAAAANSUhEUgAAABwAAAAgCAIAAACO148VAAAHoUlEQVRIDXXB2W9c5RkH4N97vrPNjGe8QCAGZbcTOwvIie2CLBSIA8WqRHtFJX/+R3pVqVKltioplZqUXrR3vahEpQp604PUEnoDGOI0iY3tibfJeBtnZo7nLHPOmW+piVQJGvI8tKHEwsKC7/sjIyONFDu1h5VK5cbHGwAkjEjZAOgRAAXLMKFbrdbk6Oj5of7h4acu5BAAmUQhRj4PUysjlbSp9d35e/6+Pzo62kxpc2d3dXX19zcrACSMWDsA6BEAOUYmVLvdfm3k4rmhw8PDT13IIQAyiUKMfB4ugwtQJY0WFxdbrdbFiyP1VG7t7K6trV37Z5MIiozUNAHQIwBsrZjW0PqlwaGzA4eHhp99+RBiIANyGoxgATZAq5FfLpfDMLw0OroXpZs7O+vr67/8x55hGGCsY9sA6BEAllam1gDGTw6eHegfHj488SwioAMU8DULcAFaqNfm5+dbrdbExEQ9SZZXVm7duvW38mHDMIp9zsiVZ48eORoEQaVS2dzcDLcLQc3Yq+1Njg6eOXX0xIljY4N2l4uChSKQAyR0Ak3zD3c//fTTWq12+fLl1DSr29uLi4t/udtHRG4RJ0dZf39/s9m8f//+xsZG+0Exrdkijqe+Pz505tTx48ffGLEKBlyCC7QBAakgaHZ374MPPlhfW3/rrbf6B0/ESbK1tfXe+0xJKShJ8tsGGVknS9pJq9WS+3kVOVD6zdcOnx06empg4EdnbQNQQADsxWkmM0UZvT879+GHf69WH0xPT78wNgKTNVutd//UiaJoP6qv799J9vdt1yn09CqlWNrNOgUAb7zSfWaw/9ixY1dPGQxQgA9U661EJNrI6O1f/HG5XFZScs4nxk+bttOM5OamarXa9XqwtLxZ3ajk8/n+558/9Mwz3UWzkDdNRqXiw1LJ7u7pPvl0LwO0QpDCb4mOVJoUvfqT31WrVcuyOJ9+Zey4abl+BKaQZipLZLuV7e7GjLFiMdfd7bo52A4MgwzzITMVY8xlJS2lkiQ6DoA003Fb0+Wf/vnBxpppEOf81bF+y7L9EJb7ldaAdFhyNMugCJrBtMhwamTtG4bB8JxBNhE6mZmlMsvQaVtxOwmCpOm36fLP/lpZWzFJc85fGztkW7Yfwip8AoBkwYgvAJAEYYBA2l0lqwaCTs9ZZjHnWKZElGk/EGsrwcr9VX8/jOMOvfzzm5sr87bOOOevj/c4Fvlhx7TuACA4No6CIMhOWQ6ANFuKJQAafncns5U0wqgTxqkftDcrzVptL0kzKTW99KvbW8tzlow552+OdzmW8qMU4it8jRGVAGSUj6kb0ClsARNAZTfxW2kUpdv15JE0jlIhhNYAiMbfKW8tzlqdYIbzqfGcaws/au9tfQlAKUpTRykZKrcpC0KItnIzZWmtay1ESSdNk4eBllIqpaVU0CAiENGlaxs7X31mdwLO+Q/GLdfu+FF079bHAKREGDMlZSM1qjHLsixRTqZMrVWoe6RmWutIuPgfIgJABy5d29he+MwRAef8zXHLcTp+HNV3HqyuVmoP9w33qbgd1xPabltCiExZHWUCukNdAAOR0DkiwjfQgZFfr+0uzjoi4Jy/Pm7bbua3o55in+fdXFha7z1yut6oNzPWUF1SSiGZ1ExrDW0BBEApG99GB154p7y7OOuKgM/wq2M52xF+O3m6q/SR98m9pUrXsXONRtPvsLrqElJISUpBA4ZkAOGAYvgmIgLo3LWF2tKsK0PO+ZVLXbYDP+k8l3c97993l6vW8VH/QMdsoJhlmZJSK6W1NgUBBMBU+BYCCHT2N/O1pVlXBHxm5srFou3AT8SRnON5n9xZ3jROjLdarX1hNXRXmmUQQiuptWaCoHHAxP8hAHT23Tu7S7OuDDnnVy52W47hJ+qkY3rezf8sb6pTE2EY7guriaKQgglBUmqtDQFoaA2T8Dga+u1cbekLVwTTM3xypM+ymZ+q0zbzvH/dLm+KU5ejKGpJu4kiAEellhY4oGwo0tCkNR5Dg9c/ry3OuiKc5vzqi4ds2/YTXLDged5cuRoPTMVx3FJ2EyXDoJJo5FUEIEOfgoknoMHrn9cWZ10RTnN+9cVnbNvyE1yw4HneXLkaD0zFcRxox6cSY6wkGjkZAkjRq2HhCWjw+ue1xS9cGXA+M/nCIcex/ATnTXieN1euxgNTcRwH2vGpxBgriUZOhgAy9CmYeAI6c+OLvaUvXRXNcD55vs9xTD/DEMHzvLlyNRqYasdxoB2fSoyxomjkZQggRZ+GCWh8Fzp3/e7e8pc5FXHOXz/f5Tis2cFJgud5c+VqNDAVx3GgHZ9KjLFSp5GXIYAMvUqbWmt8F7pwY75+fy6vY8751WHXdg1f4IjCR543V66GA1Ptdhwop0klxlh3p5GTIYBU9WiYeAIau1FprNwu6JjzH08Owc7BB3o68DxvrlyNBqfiOA6U41OJiEqikZchAKX6tDIBje9C33uv2li5XdAR529PnoaThw8UM3ieN1euRoNTcRwHyvGpREQl0cjLEICSvdCmBrTWeAxN/KHeuD9X0CHnP7x6GnYePuB04HneXLkaD07FcRwox6cSEZVEIydDAFr0Qpv6ETzmv4dFfJqyfL0jAAAAAElFTkSuQmCC",
            "iVBORw0KGgoAAAANSUhEUgAAABwAAAAgCAIAAACO148VAAAHn0lEQVRIDWXBW3NT5xUG4Hd939besizJkq2TbdmyZSRzKAGCp0ln2vyI/JCSNE1nmum0t53e9zppbtJCDqVTJpAZmEJoyKFJ2YNdEgJYNjZUsoxsWYd9+taq7Wmu/Dz0/rVL9Xo9W5heGzhXrly5+3Sw3E/1+33qPrOjfi6XcxyHiEQkmUyOjIxkM2MvHK/W69OJuKMYSqHgIGkhANp7Jq5pIqHo3Y/+tLi4OFGa2fATV69evft0sNxP9ft93WvScEdEmFkOLSwslMvlmfL06cX5TCY14lhxC0qh4CBpIQDaeyauMZHQ9MH1T8rlcjw9fn9Hbn96e7k9XPFGTRTFg74VeSYyvu8xM4B8Pl8sFmemp3585ng2Y4+NqswIiDBGiAM+0B30Ha3TTpxufPOvbDZrtHOn0bn818uP+milKrVaLWtJAsYP/L3unud5waFEIpEbz770wmJ9cTqfsTMj0MAoEAN8oD/sO0qnnTi9c+VqpVKJjWZv3m9//PHHq8Nwayz36quv/rTsVLOxJ5Hc+Xz9yXpzX6/Xy2Qyk8X8y+dOzpRTMymrMgIfEAgLBxwEYWgpHbcdevvKtdnZWTuZ/Xrdv3bt2vd7gyfx0VdeeeVnp4rV4thGE19+6W5sPmtvbRljxsfHp0rFl8+eKJdH55P6eBxdwAcHHHmhp5QykfEDn/7498+OHZvL5nKbPbl58/O7zzbv7m3P7ZsoZEeSnee9x4/Xdjq7vX4vnU7nc/nyVOknZ09MTiamRtVsHH3ASCTCGhqEvX5vu7NNf7j0j1On6tOzk34MX321/vXmxp3mum3bo+LYRvu+v93e8Tw/DMPp6el8Pl+eKr50er6YUxmb0goDQHGoIbayh8Gws9tptVr05rs3z5ypHa9OziXxcCv4tr19d+vpYDCMul7U84aeF4UGBEVqZnY2m8nkJrL1ucJ4AlpBgN0+lIgS0Up1dobt7U6r1aKfv33z9Onayerk2SyaIZ4M/Uf9ge/7YdcLe57v+0oprbVlWYVCIZGIJROxQtIeJRiCB2zvIvIl9Nn3vXa7s7Ozu7O7Q7++6C4sTFdnJk7nseNhF7KtJcM8iNg3DGZNpIgUkWXbSoMUKQIZxBQchWd76Dznzrbf2mptt9ue54VhSL//ZL1Uyk7lkyeK6A/Q1+g6qBBCIAIIsADCgSEQAj5jz0Pgh2M2lUat1T2sN3qbG7vr6+vdbldEtNb01qX7lUppdiqzWIAwuoRtQgowGkaBAIX/84FIEAmGPqLIdFq9p6s73QC7O0G3G+x1u77vg8jSmi688+9abaY6O1HPIWahw/IsRL/ft1OOGokZge974YFot9sNoygMo8GQgyBot2RjDcwcRcpEYOYoigDQvjff+65aLVamx+pZ7Gt5UaMXtFotSSTZjofMg34YBGEYBp1OJwjCIIiGHnw/iCKbTZJFjNFslGGOoggiAOg3H65V5sZnisn5JKIQG3uDB9vdzY3NZ0N0AoiIiWLCJCLGGBFhIWNs2QfFsPhAzLBi5iiKhEUg9Lu/bVYqmXIxMePAG6LxfPfe02aj0VjzdTtU2GcSgMIPCEqTQwcUyBJhYkuYmDmKImYWEfrt5c35+bGZ4mjZQa+Hh83nd9c3Hz582EoUdmMpYYlChw0xcxAEzAwRGxYArXTMsogoJloJMXMURcwsIvTWh4+OHctVptJlB/0+vv9v++vVJw8efNdMjO/YSRHhKA5RAHzfZ2YSinFcq33aUhYIMba0KD5gmIWF6Y2/3Dt5cqo6M1524Hl48HTri4er396/34jF2zpGRIoSiiylFDMDUGI5Jh2LxQgaTCJisaVZAZBDzEKvv/fNj07P1Cq5aQdhgG83t+5811hZWdlwUs9jcRCUjAIagNYKIAXt8IjSWkGRaDasjVas6JAcotfe+/Ls2bnaXL5kgRn3n2zdXlm7d+9eK5nbjadEBJyCaIAUAUQKsOmAglKi2TAZrVjvU0oBEBG68OfPXzw3X58vFBT2/Wdt69Zyw3Xd56mpvZEMAJIEoHCAAChCDIpABKWgmJkiRaysQwBEhC5cvLV0vrZYLU0INGG50brhPnZdt5c+5iUmcQQJabGICIeYGQYkKnbAJoKI0IWLt86frx2vliYEmrDcaN5wV13X7aVrXqKEI0hIi0VEOMTMMERCsX22TYCI0IWLt5bO1xarpQmBJiw3mjfcVdd1e+malyjhCAJptogIh5gZhkgots+OEUhE6LVLny69eGyxWhoXaMJKo3ndXXVdtz9W8xKTOIJAmi0i7BMBM8OAoOzYAYAAodffv7304kJ9vjQu0ISVRvO6u+q67iBT90encASBFBN+wMwSCQls27Ysi4gA0C8++OfSuYX6fDEr0ITlRvOGu+q67jC76I9O4QgCKSYcEBEwMwyTkOM4RIoOgN748LOlc9X6XDEj0ITlRvOGu+q6rj9xIkyVcZRAsQAQYWbDzDBMgOM4AIhIKUW//OjO0tlqfa4wJtCElUbzurvquq6fOxmlyjhKoFgAiLAIG2PEsBLYji0ipEgrTb+6/MXSmblapZAWaMJKo3Xdfey6bpg/FaVncJQIGQNAwCJsjAEbBdi2HUUREWmt/wcpsGT3uQw7WAAAAABJRU5ErkJggg==",
        ],
        9: [
            "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",
        ],
        10: [
            "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",
        ],
    }


# ============================================================
# V1.9 FIX3 missing logging compatibility
# ============================================================

_LOG_LOCK = globals().get("_LOG_LOCK", threading.RLock())

_CONSOLE_SUPPRESS_PREFIXES = (
    "[诊断]",
    "[诊断日志]",
    "[V2.5配置]",
    "[V2.4识别资源]",
    "[V2.4保护]",
    "[V2.5线程减负]",
    "[V2.5 DXGI直裁]",
    "[V2.6匹配页诊断]",
    "[V2.7匹配页诊断]",
    "[实时视觉] DXGI≈",
    "[性能]",
    "选马预估 ",
    "炸弹机会：",
)


def log(message):
    """Keep the console readable and retain the same evidence for review."""
    try:
        line = str(message)
        with _LOG_LOCK:
            _session_paths()
            with SESSION_LOG_PATH.open("a", encoding="utf-8") as stream:
                stream.write(f"[{time.strftime('%H:%M:%S')}] {line}\n")
            if not line.startswith(_CONSOLE_SUPPRESS_PREFIXES):
                print(line, flush=True)
    except Exception:
        print(str(message), flush=True)


def event_log(event, **kwargs):
    """Write structured decisions and capture faults for post-run diagnosis."""
    try:
        with _LOG_LOCK:
            _session_paths()
            payload = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"),
                       "event": str(event), **kwargs}
            with SESSION_EVENTS_PATH.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


# ============================================================
# V1.9 FIX4 missing ADB discovery compatibility
# ============================================================

def discover_adb_device():
    """
    兼容缺失的旧版ADB设备发现函数。
    返回第一个已连接的Android设备。
    """
    try:
        result = subprocess.run(
            [ADB_PATH, "devices"],
            capture_output=True,
            text=True,
            timeout=5,
        )

        devices = []

        for line in result.stdout.splitlines():
            line = line.strip()
            if not line or line.startswith("List of devices"):
                continue

            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                devices.append(parts[0])

        if not devices:
            raise RuntimeError("没有发现ADB设备，请检查USB调试")

        return devices[0]

    except Exception as exc:
        raise RuntimeError(
            f"ADB设备发现失败: {exc}"
        )


# ============================================================
# V1.9 FIX6 input_size compatibility
# ============================================================
# AndroidADB 增加设备尺寸兼容字段。

# ============================================================
# V1.9 FIX5 missing AndroidADB compatibility
# ============================================================

def _parse_effective_wm_size(raw):
    """
    Return (width, height, source).

    Critical rule:
    Prefer "Override size" when Android reports one. ADB input coordinates
    use the effective display coordinate space. Falling back to the first
    WxH match (the historical behavior) can overshoot taps toward the
    lower-right corner.
    """
    s = str(
        raw
        or ""
    )

    override = re.search(
        r"Override\s+size\s*:\s*(\d+)\s*x\s*(\d+)",
        s,
        flags=re.IGNORECASE,
    )

    if override:
        return (
            int(
                override.group(
                    1
                )
            ),
            int(
                override.group(
                    2
                )
            ),
            "override",
        )

    physical = re.search(
        r"Physical\s+size\s*:\s*(\d+)\s*x\s*(\d+)",
        s,
        flags=re.IGNORECASE,
    )

    if physical:
        return (
            int(
                physical.group(
                    1
                )
            ),
            int(
                physical.group(
                    2
                )
            ),
            "physical",
        )

    generic = re.search(
        r"(\d+)\s*x\s*(\d+)",
        s,
    )

    if generic:
        return (
            int(
                generic.group(
                    1
                )
            ),
            int(
                generic.group(
                    2
                )
            ),
            "generic",
        )

    return (
        None,
        None,
        "unknown",
    )



class AndroidADB:
    """
    兼容缺失的旧版AndroidADB基础类。
    提供设备绑定、基础tap接口。
    """

    def __init__(self, serial):
        self.serial = serial

        # 兼容旧版代码依赖的设备尺寸字段
        self._input_size = (1080, 2400)
        self.screen_size = self._input_size

        self.last_viewport = (
            0,
            0,
            self._input_size[0],
            self._input_size[1],
        )


    def input_size(self):
        """兼容旧版代码：返回Android输入尺寸。"""
        return self._input_size

    def shell(self, *args, timeout=10):
        cmd = [
            ADB_PATH,
            "-s",
            self.serial,
            "shell",
            *[str(x) for x in args],
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.stdout

    def tap(self, x, y):
        try:
            self.shell(
                "input",
                "tap",
                int(x),
                int(y),
                timeout=5,
            )
            return True
        except Exception as exc:
            log(f"[ADB tap失败] {exc}")
            return False

    def viewport_to_device(self, x, y):
        return float(x), float(y)


# ============================================================
# V1.9 FIX7 full compatibility layer
# Restore common V1.8 AndroidADB dependencies
# ============================================================

# AndroidADB扩展兼容方法
if "AndroidADB" in globals():

    def _adb_get_screen_size(self):
        try:
            out = self.shell(
                "wm",
                "size",
            )

            w, h, source = (
                _parse_effective_wm_size(
                    out
                )
            )

            if (
                w is not None
                and h is not None
            ):
                self._input_size = (
                    int(
                        w
                    ),
                    int(
                        h
                    ),
                )
                self.screen_size = (
                    self._input_size
                )
                self._wm_size_source = (
                    source
                )
        except Exception:
            pass

        return self._input_size

    def _adb_crop_viewport(self, image):
        h, w = image.shape[:2]
        self.last_viewport = (0, 0, w, h)
        return image

    def _adb_initialize(self):
        self._adb_get_screen_size()
        return True

    AndroidADB.get_screen_size = _adb_get_screen_size
    AndroidADB._crop_viewport = _adb_crop_viewport
    AndroidADB.initialize = _adb_initialize


# 常见全局默认兼容
if "ADB_DEVICE" not in globals():
    ADB_DEVICE = None


# ============================================================
# V1.9 FIX9 callable input_size compatibility
# ============================================================

if "AndroidADB" in globals():

    # 保留尺寸数据，同时提供旧代码可能调用的方法
    def _get_input_size(self):
        return self._input_size

    def _get_screen_size_safe(self):
        return self.screen_size

    AndroidADB.get_input_size = _get_input_size
    AndroidADB.get_screen_size = _get_screen_size_safe


# ============================================================
# V1.9 FIX10 AndroidADB full interface compatibility layer
# Auto-added by interface scan
# ============================================================

if "AndroidADB" in globals():

    def _compat_noop(self, *args, **kwargs):
        return True

    def _compat_false(self, *args, **kwargs):
        return False

    def _compat_info(self, *args, **kwargs):
        return {}

    def _compat_size(self):
        return getattr(self, "_input_size", (1080, 2400))

    def _compat_viewport_to_device(self, x, y):
        return float(x), float(y)

    def _compat_device_to_viewport(self, x, y):
        return float(x), float(y)

    def _compat_crop_viewport(self, image):
        h, w = image.shape[:2]
        self.last_viewport = (0, 0, w, h)
        return image

    def _compat_capture(self):
        return None

    def _compat_shell_output(self, *args, **kwargs):
        return self.shell(*args, **kwargs)

    # 核心接口
    AndroidADB.get_input_size = _compat_size
    AndroidADB.get_screen_size = _compat_size
    AndroidADB.viewport_to_device = _compat_viewport_to_device
    AndroidADB.device_to_viewport = _compat_device_to_viewport
    AndroidADB._crop_viewport = _compat_crop_viewport
    AndroidADB.capture = _compat_capture
    AndroidADB.initialize = _compat_noop
    AndroidADB.connect = _compat_noop
    AndroidADB.disconnect = _compat_noop
    AndroidADB.refresh = _compat_noop


# ============================================================
# V1.9 FIX11 AndroidADB getprop compatibility
# ============================================================

if "AndroidADB" in globals():

    def _compat_getprop(self, key):
        """
        兼容旧版:
        ADB_DEVICE.getprop("ro.product.model")
        """
        try:
            result = self.shell("getprop", str(key))
            return str(result).strip()
        except Exception:
            return ""

    def _compat_wm_size(self):
        """
        返回Android有效输入坐标尺寸。
        有Override时优先Override。
        """
        try:
            result = self.shell(
                "wm",
                "size",
            )

            w, h, source = (
                _parse_effective_wm_size(
                    result
                )
            )

            if (
                w is not None
                and h is not None
            ):
                size = (
                    int(
                        w
                    ),
                    int(
                        h
                    ),
                )
                self._input_size = size
                self._wm_size_source = (
                    source
                )
                return size
        except Exception:
            pass

        return getattr(
            self,
            "_input_size",
            (
                1080,
                2400,
            ),
        )

    AndroidADB.getprop = _compat_getprop
    AndroidADB.wm_size = _compat_wm_size


# ============================================================
# V1.9 FIX12 tuple/string compatibility
# ============================================================

def _compat_size_string(value):
    """
    兼容旧代码把设备尺寸拼接到字符串中的情况。
    tuple -> '1080x2400'
    """
    if isinstance(value, tuple) and len(value) >= 2:
        return f"{value[0]}x{value[1]}"
    return str(value)


if "AndroidADB" in globals():

    def _compat_device_input_size(self):
        return _compat_size_string(
            getattr(self, "_input_size", (1080, 2400))
        )

    AndroidADB.device_input_size = _compat_device_input_size

    # 保留wm_size原始查询，但返回字符串格式
    def _compat_wm_size_string(self):
        try:
            result = self.shell(
                "wm",
                "size",
            )

            w, h, source = (
                _parse_effective_wm_size(
                    result
                )
            )

            if (
                w is not None
                and h is not None
            ):
                self._input_size = (
                    int(
                        w
                    ),
                    int(
                        h
                    ),
                )
                self._wm_size_source = (
                    source
                )

                return (
                    f"{w}x{h}"
                )
        except Exception:
            pass

        size = getattr(
            self,
            "_input_size",
            (
                1080,
                2400,
            ),
        )

        return _compat_size_string(
            size
        )

    AndroidADB.wm_size = _compat_wm_size_string


# ============================================================
# V1.9 FIX13 missing template + pyautogui compatibility
# ============================================================

# 防止缺少模板导致大厅检测直接崩溃
if "SCREEN_TEMPLATES" in globals():
    for _name in (
        "lobby_title",
        "start_match",
        "match_button",
        "play_button",
    ):
        if _name not in SCREEN_TEMPLATES:
            SCREEN_TEMPLATES[_name] = np.zeros(
                (10, 10, 3),
                dtype=np.uint8
            )


# 防止异常处理阶段 pyautogui 未导入导致二次崩溃
try:
    import pyautogui
except Exception:
    class _DummyPyAutoGUI:
        class FailSafeException(Exception):
            pass
    pyautogui = _DummyPyAutoGUI()


# ============================================================
# V1.9 FIX14 SCREEN_TEMPLATES missing-key safe access
# ============================================================

if "SCREEN_TEMPLATES" in globals():
    class _SafeTemplateDict(dict):
        def __missing__(self, key):
            # 缺失模板时返回空模板，避免状态机崩溃
            return np.zeros((10, 10, 3), dtype=np.uint8)

    if not isinstance(SCREEN_TEMPLATES, _SafeTemplateDict):
        SCREEN_TEMPLATES = _SafeTemplateDict(SCREEN_TEMPLATES)


# ============================================================
# V1.9 FIX15 late SCREEN_TEMPLATES protection
# ============================================================

def _ensure_screen_templates_safe():
    if "SCREEN_TEMPLATES" in globals():
        if not isinstance(SCREEN_TEMPLATES, _SafeTemplateDict if "_SafeTemplateDict" in globals() else dict):
            class _LateSafeTemplateDict(dict):
                def __missing__(self, key):
                    return np.zeros((10, 10, 3), dtype=np.uint8)
            globals()["SCREEN_TEMPLATES"] = _LateSafeTemplateDict(SCREEN_TEMPLATES)
        return SCREEN_TEMPLATES
    return None


# ============================================================
# V1.9 FIX16 global SCREEN_TEMPLATES missing key protection
# ============================================================

# 在任何地方访问 SCREEN_TEMPLATES 前均保证存在关键模板。
# 修复状态机循环阶段重复 KeyError。

def _patch_screen_templates_global():
    global SCREEN_TEMPLATES

    if "SCREEN_TEMPLATES" not in globals():
        SCREEN_TEMPLATES = {}

    required = [
        "lobby_title",
        "start_match",
        "match_button",
        "play_button",
        "select_horse",
        "settlement",
    ]

    for key in required:
        if key not in SCREEN_TEMPLATES:
            SCREEN_TEMPLATES[key] = np.zeros(
                (10, 10, 3),
                dtype=np.uint8
            )

    return SCREEN_TEMPLATES


_patch_screen_templates_global()


# ============================================================
# V1.9 FIX17 runtime lobby guard
# ============================================================

def _safe_template_available(name):
    try:
        t = SCREEN_TEMPLATES.get(name)
        return t is not None and getattr(t, "size", 0) > 0
    except Exception:
        return False


# ============================================================
# V1.9 FIX18 final lobby template runtime guard
# ============================================================

def _runtime_fix_screen_templates():
    global SCREEN_TEMPLATES
    if "SCREEN_TEMPLATES" not in globals():
        SCREEN_TEMPLATES = {}
    for _k in [
        "lobby_title",
        "start_match",
        "match_button",
        "play_button",
        "select_horse",
        "settlement",
    ]:
        SCREEN_TEMPLATES.setdefault(
            _k,
            np.zeros((10,10,3), dtype=np.uint8)
        )

_runtime_fix_screen_templates()


# ============================================================
# V2.9 关闭详细诊断文件
# ============================================================

_DIAG_PATH = None

def init_diag_logger():
    return

def diag_log(tag, **data):
    return


# ============================================================
# V1.9 FIX20 template + DXGI window monitor
# ============================================================

import time

_TEMPLATE_LAST_KEYS = None

def monitor_runtime_state():
    """
    监控运行时关键对象是否被覆盖。
    """
    global _TEMPLATE_LAST_KEYS

    try:
        keys = list(globals().get("SCREEN_TEMPLATES", {}).keys())

        if _TEMPLATE_LAST_KEYS != keys:
            diag_log(
                "screen_templates_changed",
                keys=keys,
                count=len(keys)
            )
            _TEMPLATE_LAST_KEYS = keys

        diag_log(
            "runtime_state",
            screen_template_count=len(keys),
            digit_template_count=len(
                globals().get("DIGIT_SAMPLES", {})
            )
        )

    except Exception as e:
        diag_log(
            "monitor_error",
            error=str(e)
        )


# 修复scrcpy窗口变化导致DXGI区域非法
def safe_dxgi_region_check():
    try:
        if "scrcpy_window" in globals():
            w = getattr(scrcpy_window, "client_width", 0)
            h = getattr(scrcpy_window, "client_height", 0)

            if w < 300 or h < 300:
                diag_log(
                    "invalid_scrcpy_window",
                    width=w,
                    height=h
                )
                return False

        return True

    except Exception:
        return False


# ============================================================
# V1.9 FIX21 monitor hook
# ============================================================

def runtime_monitor_hook():
    try:
        monitor_runtime_state()
        safe_dxgi_region_check()
    except Exception:
        pass


# ============================================================
# V1.9 FIX22 game capture calibration + hand safe mode
# ============================================================

import cv2
import time

_CAPTURE_DIR = None
_CAPTURE_INDEX = 0

def init_game_capture_dir():
    global _CAPTURE_DIR
    try:
        base = globals().get("SESSION_DIR", "horse_race_capture")
        _CAPTURE_DIR = Path(base) / "screenshots"
        _CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        _CAPTURE_DIR = Path("horse_race_capture")
        _CAPTURE_DIR.mkdir(parents=True, exist_ok=True)


def save_game_frame(frame, name="frame"):
    """
    保存DXGI实际游戏窗口截图，用于模板恢复。
    """
    global _CAPTURE_INDEX

    try:
        if _CAPTURE_DIR is None:
            init_game_capture_dir()

        _CAPTURE_INDEX += 1

        path = _CAPTURE_DIR / f"{name}_{_CAPTURE_INDEX:05d}.png"

        if frame is not None:
            cv2.imwrite(str(path), frame)

        return str(path)

    except Exception:
        return None


def safe_hand_recognition():
    """
    手牌模板缺失时不崩溃，保存现场截图。
    """
    try:
        if len(globals().get("DIGIT_SAMPLES", {})) == 0:
            frame = globals().get("latest_frame", None)

            if frame is not None:
                save_game_frame(frame, "missing_digit_template")

            return []

    except Exception:
        pass


init_game_capture_dir()


# ============================================================
# V1.9 FIX23 stable race capture + exception guard
# ============================================================

import traceback

def safe_template_get(name):
    try:
        templates = globals().get(
            "SCREEN_TEMPLATES",
            {},
        )

        template = templates.get(
            name
        )

        if (
            template is None
            or getattr(
                template,
                "size",
                0,
            )
            <= 0
        ):
            return None

        # 兼容层曾用10x10全零图充当“缺失模板”。
        # 这种东西不能参与matchTemplate。
        if (
            template.shape[
                0
            ]
            <= 12
            and template.shape[
                1
            ]
            <= 12
            and float(
                np.std(
                    template
                )
            )
            < 1.0
        ):
            return None

        return template

    except Exception:
        return None


def safe_max(values, default=None):
    try:
        if not values:
            return default
        return max(values)
    except Exception:
        return default


def save_hand_error_frame():
    try:
        frame = globals().get("latest_frame", None)
        if frame is not None and "save_game_frame" in globals():
            save_game_frame(frame, "hand_error")
    except Exception:
        pass


def safe_hand_scores(scores):
    """
    手牌识别保护：
    无候选时保存截图，不中断比赛状态机。
    """
    if not scores:
        save_hand_error_frame()
        return None
    return safe_max(scores)



# ============================================================
# V1.9 FIX24 clean state machine guards
# ============================================================

def safe_template_get(name):
    try:
        templates = globals().get(
            "SCREEN_TEMPLATES",
            {},
        )

        template = templates.get(
            name
        )

        if (
            template is None
            or getattr(
                template,
                "size",
                0,
            )
            <= 0
        ):
            return None

        # 兼容层曾用10x10全零图充当“缺失模板”。
        # 这种东西不能参与matchTemplate。
        if (
            template.shape[
                0
            ]
            <= 12
            and template.shape[
                1
            ]
            <= 12
            and float(
                np.std(
                    template
                )
            )
            < 1.0
        ):
            return None

        return template

    except Exception:
        return None


def safe_best_score(values, default=None):
    try:
        if values is None or len(values) == 0:
            return default
        return max(values)
    except Exception:
        return default


def hand_recognition_guard():
    try:
        digits = globals().get("DIGIT_SAMPLES", {})
        if not digits:
            frame = globals().get("latest_frame")
            if frame is not None and "save_game_frame" in globals():
                save_game_frame(frame, "digit_template_missing")
            return False
        return True
    except Exception:
        return False



# ============================================================
# V1.9 FIX25 OpenCV empty frame guard
# ============================================================

# 防止DXGI瞬时无帧导致 cvtColor(empty) 崩溃

_original_cvtColor = cv2.cvtColor

def safe_cvtColor(src, code, *args, **kwargs):
    try:
        if src is None:
            return None

        if hasattr(src, "size") and src.size == 0:
            return None

        return _original_cvtColor(src, code, *args, **kwargs)

    except Exception:
        return None

cv2.cvtColor = safe_cvtColor


def safe_frame_valid(frame):
    try:
        return frame is not None and hasattr(frame, "size") and frame.size > 0
    except Exception:
        return False


# ============================================================
# V1.9 FIX27 clean DXGI frame guard
# Do not rewrite existing .shape calls.
# ============================================================

def ensure_valid_frame(frame):
    """
    DXGI线程统一入口保护。
    """
    try:
        if frame is None:
            return False
        if not hasattr(frame, "shape"):
            return False
        if getattr(frame, "size", 0) <= 0:
            return False
        return True
    except Exception:
        return False


def safe_get_shape(frame):
    if not ensure_valid_frame(frame):
        return ()
    return frame.shape


# ============================================================
# V1.9 FIX28 DXGI entry frame guard
# Only allow valid frames into state machine
# ============================================================

def dxgi_frame_ready(frame):
    try:
        if frame is None:
            return False
        if not hasattr(frame, "shape"):
            return False
        if getattr(frame, "size", 0) <= 0:
            return False
        return True
    except Exception:
        return False


def safe_process_frame(frame, callback):
    """
    DXGI空帧直接跳过，不进入大厅/状态机。
    """
    if not dxgi_frame_ready(frame):
        return None

    try:
        return callback(frame)
    except Exception:
        return None



# ============================================================
# V1.9 FIX33 integrated lobby green match
# Replace lobby start button detection with green button detection
# ============================================================

import cv2
import numpy as np

def detect_green_match_button(frame):
    if frame is None:
        return None

    try:
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)

        lower = np.array([35, 50, 40])
        upper = np.array([90, 255, 255])

        mask = cv2.inRange(hsv, lower, upper)

        h, w = frame.shape[:2]

        # 只搜索右下按钮区域
        roi = mask[int(h*0.55):h, int(w*0.55):w]

        contours, _ = cv2.findContours(
            roi,
            cv2.RETR_EXTERNAL,
            cv2.CHAIN_APPROX_SIMPLE
        )

        best = None

        for c in contours:
            x, y, cw, ch = cv2.boundingRect(c)

            area = cw * ch

            if area < 1500:
                continue

            ratio = cw / max(ch, 1)

            if 2.0 < ratio < 8.0:
                gx = x + int(w*0.55)
                gy = y + int(h*0.55)
                candidate = (gx, gy, cw, ch)

                if best is None or area > best[2]*best[3]:
                    best = candidate

        return best

    except Exception:
        return None


def get_green_match_point(frame):
    box = detect_green_match_button(frame)

    if box is None:
        return None

    x, y, w, h = box

    print(
        f"[大厅绿色按钮] box={box} click=({x+w//2},{y+h//2})"
    )

    return (
        x + w//2,
        y + h//2
    )


def decode_png(data):
    raw = base64.b64decode(data)
    arr = np.frombuffer(raw, dtype=np.uint8)
    bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if bgr is None:
        raise RuntimeError("内置模板解码失败")
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)

DIGIT_SAMPLES = {
    int(k): [decode_png(x) for x in values]
    for k, values in DIGIT_SAMPLES_B64.items()
}
SCREEN_TEMPLATES = {
    k: decode_png(v)
    for k, v in SCREEN_TEMPLATES_B64.items()
}

EMBEDDED_USER_AVATAR = decode_png(
    USER_RESULT_AVATAR_B64
)

def load_user_avatar_template():
    """
    默认使用当前录像结果页里截取的账号头像。
    如果以后你换了头像，可以把新的头像小图保存为：
        horse_race_user_avatar.png
    程序会自动优先使用它，无需改代码。
    """
    if USER_AVATAR_OVERRIDE_PATH.exists():
        try:
            bgr = cv2.imread(
                str(USER_AVATAR_OVERRIDE_PATH),
                cv2.IMREAD_COLOR,
            )
            if bgr is not None:
                return cv2.cvtColor(
                    bgr,
                    cv2.COLOR_BGR2RGB,
                )
        except Exception:
            pass

    return EMBEDDED_USER_AVATAR.copy()

# Android ADB截图速度远低于桌面截屏。
# 保留异步视觉，但把“最新帧”寿命放宽；后台截图线程会自动受screencap耗时限制。
CFG["capture_fps"] = min(
    float(
        CFG.get(
            "capture_fps",
            30.0,
        )
    ),
    4.0,
)
CFG["stable_hand_max_age"] = max(
    float(
        CFG.get(
            "stable_hand_max_age",
            0.75,
        )
    ),
    3.5,
)

# ------------------------------------------------------------
# V1.7 极速模式参数
# ------------------------------------------------------------

if SCRCPY_REALTIME_ENABLED:
    # scrcpy/DXGI实时帧：手牌线程高频读取最新帧，两帧一致即可稳定。
    CFG["hand_worker_fps"] = max(
        24.0,
        float(
            CFG.get(
                "hand_worker_fps",
                20.0,
            )
        ),
    )
    CFG["rank_worker_fps"] = max(
        8.0,
        float(
            CFG.get(
                "rank_worker_fps",
                6.0,
            )
        ),
    )

    CFG["stable_hand_max_age"] = 0.32
    CFG["hand_consensus_window"] = 3
    CFG["hand_consensus_required"] = 2

    # 手动视频里有效选牌->出牌通常在0.5秒内完成；
    # 这里把旧版秒级等待全部压缩。
    CFG["min_seconds_between_plays"] = 0.22
    CFG["urgent_play_interval"] = 0.06
    CFG["card_click_wait"] = 0.010
    CFG["clear_card_click_wait"] = 0.004
    CFG["valid_button_poll_seconds"] = 0.60
    CFG["valid_button_poll_interval"] = 0.035
    CFG["out_button_poll_interval"] = 0.035
    CFG["out_button_cooldown_wait_seconds"] = 4.60
    CFG["out_button_ready_stable_frames"] = 2
    CFG["after_play_wait"] = 0.025
    CFG["post_play_hand_change_timeout"] = PLAY_CONFIRM_TIMEOUT
    CFG["post_play_hand_change_poll"] = 0.010
    CFG["vision_suppress_after_action"] = 0.12
    CFG["urgent_loop_sleep"] = 0.004
    CFG["normal_loop_sleep"] = 0.010

USER_AVATAR_TEMPLATE = (
    load_user_avatar_template()
)

REALTIME_RANK_TEMPLATES = {
    int(rank): decode_png(data)
    for rank, data in REALTIME_RANK_TEMPLATES_B64.items()
}

REALTIME_RANK_WIDGET = decode_png(
    REALTIME_RANK_WIDGET_B64
)

SELECTION_DIGIT_SAMPLES = {
    int(k): [
        decode_png(item)
        for item in values
    ]
    for k, values in SELECTION_DIGIT_SAMPLES_B64.items()
    if values
}


_V32_SELECTION_FAST_TEMPLATES = {}

for _value, _samples in SELECTION_DIGIT_SAMPLES.items():
    _bank = []

    for _sample in _samples:
        _gray = cv2.cvtColor(
            _sample,
            cv2.COLOR_RGB2GRAY,
        ).astype(
            np.float32
        )

        _gray = (
            _gray
            - float(
                _gray.mean()
            )
        ) / (
            float(
                _gray.std()
            )
            + 1e-6
        )

        _bank.append(
            _gray
        )

    if _bank:
        _V32_SELECTION_FAST_TEMPLATES[
            int(
                _value
            )
        ] = _bank


def _v32_fast_selection_value(
    patch,
):
    if (
        patch is None
        or getattr(
            patch,
            "size",
            0,
        )
        <= 0
        or not _V32_SELECTION_FAST_TEMPLATES
    ):
        return (
            None,
            0.0,
            0.0,
            {},
        )

    gray = cv2.cvtColor(
        patch,
        cv2.COLOR_RGB2GRAY,
    ).astype(
        np.float32
    )

    gray = (
        gray
        - float(
            gray.mean()
        )
    ) / (
        float(
            gray.std()
        )
        + 1e-6
    )

    scores = {}

    for value, samples in (
        _V32_SELECTION_FAST_TEMPLATES.items()
    ):
        scores[
            int(
                value
            )
        ] = max(
            float(
                np.mean(
                    gray
                    * sample
                )
            )
            for sample in samples
        )

    if not scores:
        return (
            None,
            0.0,
            0.0,
            {},
        )

    ordered = sorted(
        scores.items(),
        key=lambda item: item[
            1
        ],
        reverse=True,
    )

    best_value = int(
        ordered[
            0
        ][
            0
        ]
    )

    best_score = float(
        ordered[
            0
        ][
            1
        ]
    )

    margin = (
        best_score
        - float(
            ordered[
                1
            ][
                1
            ]
        )
        if len(
            ordered
        )
        >= 2
        else 1.0
    )

    return (
        best_value,
        best_score,
        float(
            margin
        ),
        scores,
    )


class _StreamFallbackRequested(RuntimeError):
    pass


class AndroidH264Stream:
    """
    V1.8.2：
    Android screenrecord -> H264 stdout -> 分块读取 -> PyAV CodecContext解码。

    V1.8.1直接：
        av.open(proc.stdout, format="h264")
    在部分Windows + Android组合上会卡在av.open()的探测阶段，
    导致日志只有stream_start，却永远没有第一帧。

    新版不再使用av.open()做流探测：
      1. reader线程直接读取stdout字节
      2. 主流线程有超时，可判断“到底有没有字节”
      3. CodecContext.parse()增量解析H264
      4. stderr单独收集并写入诊断日志
    """

    def __init__(
        self,
        adb_device,
    ):
        self.adb_device = adb_device
        self.proc = None
        self._stop_requested = False
        self.reconnect_count = 0
        self.frames_decoded = 0
        self.bytes_received = 0

        self._chunk_queue = None
        self._stdout_thread = None
        self._stderr_thread = None
        self._stderr_lines = deque(
            maxlen=30
        )

        self._started_at = 0.0
        self._first_byte_at = None
        self._first_frame_at = None

    def _stderr_snapshot(
        self,
    ):
        return " | ".join(
            list(
                self._stderr_lines
            )[
                -8:
            ]
        )

    def _stdout_reader(
        self,
        proc,
        out_queue,
    ):
        try:
            fd = proc.stdout.fileno()

            while (
                not self._stop_requested
                and proc.poll() is None
            ):
                chunk = os.read(
                    fd,
                    STREAM_READ_CHUNK,
                )

                if not chunk:
                    break

                # 永远优先最新数据。
                # 队列满时丢最旧块，避免解码积压成“看过去的画面”。
                while True:
                    try:
                        out_queue.put_nowait(
                            chunk
                        )
                        break
                    except queue.Full:
                        try:
                            out_queue.get_nowait()
                        except queue.Empty:
                            break

        except Exception as exc:
            try:
                out_queue.put_nowait(
                    (
                        "__reader_error__",
                        f"{type(exc).__name__}: {exc}",
                    )
                )
            except Exception:
                pass

        finally:
            try:
                out_queue.put_nowait(
                    None
                )
            except Exception:
                pass

    def _stderr_reader(
        self,
        proc,
    ):
        try:
            while (
                not self._stop_requested
                and proc.poll() is None
            ):
                line = proc.stderr.readline()

                if not line:
                    break

                if isinstance(
                    line,
                    bytes,
                ):
                    line = line.decode(
                        "utf-8",
                        "replace",
                    )

                line = line.strip()

                if line:
                    self._stderr_lines.append(
                        line
                    )

        except Exception:
            pass

    def _start(
        self,
    ):
        self.close()

        self._stderr_lines.clear()
        self._chunk_queue = queue.Queue(
            maxsize=12
        )
        self._started_at = time.monotonic()
        self._first_byte_at = None
        self._first_frame_at = None

        cmd = [
            ADB_PATH,
            "-s",
            self.adb_device.serial,
            "exec-out",
            "screenrecord",
            "--output-format=h264",
            "--bit-rate",
            str(
                int(
                    ADB_STREAM_BITRATE
                )
            ),
        ]

        if (
            ADB_STREAM_SIZE
            and ADB_STREAM_SIZE.lower()
            not in (
                "native",
                "auto",
                "0",
            )
        ):
            cmd += [
                "--size",
                ADB_STREAM_SIZE,
            ]

        cmd += [
            "--time-limit",
            str(
                int(
                    ADB_STREAM_TIME_LIMIT
                )
            ),
            "-",
        ]

        event_log(
            "stream_start",
            size=ADB_STREAM_SIZE,
            bitrate=ADB_STREAM_BITRATE,
            time_limit=ADB_STREAM_TIME_LIMIT,
            decoder="codec_context_incremental",
            command=" ".join(
                cmd
            ),
        )

        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )

        if (
            self.proc.stdout is None
            or self.proc.stderr is None
        ):
            raise RuntimeError(
                "无法读取screenrecord stdout/stderr"
            )

        self._stdout_thread = threading.Thread(
            target=self._stdout_reader,
            args=(
                self.proc,
                self._chunk_queue,
            ),
            name="HorseH264Stdout",
            daemon=True,
        )

        self._stderr_thread = threading.Thread(
            target=self._stderr_reader,
            args=(
                self.proc,
            ),
            name="HorseH264Stderr",
            daemon=True,
        )

        self._stdout_thread.start()
        self._stderr_thread.start()

        log(
            "[实时流] screenrecord进程已启动；"
            "等待H264首字节..."
        )

    def _decode_one_connection(
        self,
        stop_event,
    ):
        self._start()

        codec = av.CodecContext.create(
            "h264",
            "r",
        )

        got_any_byte = False
        got_any_frame = False

        while (
            not stop_event.is_set()
            and not self._stop_requested
        ):
            proc = self.proc

            if proc is None:
                raise RuntimeError(
                    "screenrecord进程不存在"
                )

            try:
                item = self._chunk_queue.get(
                    timeout=STREAM_NO_DATA_TIMEOUT
                )
            except queue.Empty:
                return_code = proc.poll()
                stderr_text = self._stderr_snapshot()

                event_log(
                    "stream_no_data_timeout",
                    return_code=return_code,
                    bytes_received=self.bytes_received,
                    frames_decoded=self.frames_decoded,
                    stderr=stderr_text,
                )

                raise RuntimeError(
                    "H264流等待字节超时"
                    f"({STREAM_NO_DATA_TIMEOUT:.1f}s)；"
                    f"process={return_code}；"
                    f"stderr={stderr_text or '<空>'}"
                )

            if item is None:
                return_code = proc.poll()
                stderr_text = self._stderr_snapshot()

                raise RuntimeError(
                    "H264 stdout结束；"
                    f"process={return_code}；"
                    f"stderr={stderr_text or '<空>'}"
                )

            if (
                isinstance(
                    item,
                    tuple,
                )
                and item
                and item[
                    0
                ] == "__reader_error__"
            ):
                raise RuntimeError(
                    "H264 stdout读取线程异常："
                    + str(
                        item[
                            1
                        ]
                    )
                )

            chunk = item

            if not chunk:
                continue

            if not got_any_byte:
                got_any_byte = True
                self._first_byte_at = (
                    time.monotonic()
                )

                first_byte_ms = (
                    self._first_byte_at
                    - self._started_at
                ) * 1000.0

                log(
                    f"[实时流] 收到首批H264数据："
                    f"{len(chunk)} bytes，"
                    f"首字节延迟={first_byte_ms:.0f}ms"
                )

                event_log(
                    "stream_first_bytes",
                    bytes=len(
                        chunk
                    ),
                    first_byte_ms=first_byte_ms,
                )

            self.bytes_received += len(
                chunk
            )

            try:
                packets = codec.parse(
                    chunk
                )
            except Exception as exc:
                event_log(
                    "stream_parse_error",
                    error_type=type(
                        exc
                    ).__name__,
                    error=str(
                        exc
                    ),
                    chunk_bytes=len(
                        chunk
                    ),
                )
                raise

            for packet in packets:
                try:
                    frames = codec.decode(
                        packet
                    )
                except Exception as exc:
                    event_log(
                        "stream_decode_error",
                        error_type=type(
                            exc
                        ).__name__,
                        error=str(
                            exc
                        ),
                    )
                    continue

                for frame in frames:
                    if (
                        stop_event.is_set()
                        or self._stop_requested
                    ):
                        return

                    rgb = frame.to_ndarray(
                        format="rgb24"
                    )

                    if (
                        rgb is None
                        or rgb.size == 0
                    ):
                        continue

                    self.frames_decoded += 1

                    if not got_any_frame:
                        got_any_frame = True
                        self._first_frame_at = (
                            time.monotonic()
                        )

                        first_frame_ms = (
                            self._first_frame_at
                            - self._started_at
                        ) * 1000.0

                        log(
                            f"[实时流] H264首帧解码成功："
                            f"{rgb.shape[1]}x{rgb.shape[0]}，"
                            f"首帧延迟={first_frame_ms:.0f}ms"
                        )

                        event_log(
                            "stream_first_frame",
                            width=int(
                                rgb.shape[
                                    1
                                ]
                            ),
                            height=int(
                                rgb.shape[
                                    0
                                ]
                            ),
                            first_frame_ms=first_frame_ms,
                            bytes_before_first_frame=self.bytes_received,
                        )

                    yield rgb

            # 已经有数据但迟迟解不出首帧，也不能无限等。
            if (
                got_any_byte
                and not got_any_frame
                and time.monotonic()
                - self._started_at
                > STREAM_FIRST_FRAME_TIMEOUT
            ):
                stderr_text = self._stderr_snapshot()

                event_log(
                    "stream_first_frame_timeout",
                    bytes_received=self.bytes_received,
                    stderr=stderr_text,
                )

                raise RuntimeError(
                    "已收到H264字节，但"
                    f"{STREAM_FIRST_FRAME_TIMEOUT:.1f}s内没有解出首帧；"
                    f"bytes={self.bytes_received}；"
                    f"stderr={stderr_text or '<空>'}"
                )

    def frames(
        self,
        stop_event,
    ):
        while (
            not stop_event.is_set()
            and not self._stop_requested
        ):
            try:
                yielded = False

                for rgb in self._decode_one_connection(
                    stop_event
                ):
                    yielded = True
                    yield rgb

                if (
                    stop_event.is_set()
                    or self._stop_requested
                ):
                    break

                raise RuntimeError(
                    "H264连接结束"
                )

            except Exception as exc:
                self.close()

                if (
                    stop_event.is_set()
                    or self._stop_requested
                ):
                    break

                self.reconnect_count += 1

                stderr_text = self._stderr_snapshot()

                log(
                    "[实时流] H264连接失败："
                    f"{type(exc).__name__}: {exc}；"
                    f"重连={self.reconnect_count}/"
                    f"{STREAM_MAX_RECONNECTS_BEFORE_FALLBACK}"
                )

                event_log(
                    "stream_error",
                    error_type=type(
                        exc
                    ).__name__,
                    error=str(
                        exc
                    ),
                    reconnect_count=self.reconnect_count,
                    bytes_received=self.bytes_received,
                    frames_decoded=self.frames_decoded,
                    stderr=stderr_text,
                )

                if (
                    self.reconnect_count
                    >= STREAM_MAX_RECONNECTS_BEFORE_FALLBACK
                ):
                    raise _StreamFallbackRequested(
                        "H264连续失败达到阈值"
                    )

                time.sleep(
                    STREAM_RECONNECT_DELAY
                )

    def close(
        self,
    ):
        proc = self.proc
        self.proc = None

        if proc is not None:
            try:
                if proc.stdout:
                    proc.stdout.close()
            except Exception:
                pass

            try:
                proc.terminate()
            except Exception:
                pass

            try:
                proc.wait(
                    timeout=0.35
                )
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

        self._stdout_thread = None
        self._stderr_thread = None
        self._chunk_queue = None

    def stop(
        self,
    ):
        self._stop_requested = True
        self.close()


def _set_windows_dpi_awareness():
    """让Win32窗口坐标与DXGI物理像素保持一致。"""
    if os.name != "nt":
        return

    try:
        user32 = ctypes.windll.user32
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
        user32.SetProcessDpiAwarenessContext(
            ctypes.c_void_p(-4)
        )
        return
    except Exception:
        pass

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass


_set_windows_dpi_awareness()


class _POINT(ctypes.Structure):
    _fields_ = [
        ("x", ctypes.c_long),
        ("y", ctypes.c_long),
    ]


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


def _win32_client_rect(hwnd):
    if os.name != "nt":
        raise RuntimeError("scrcpy/DXGI版本仅支持Windows")

    user32 = ctypes.windll.user32

    rect = _RECT()
    if not user32.GetClientRect(
        ctypes.c_void_p(int(hwnd)),
        ctypes.byref(rect),
    ):
        raise RuntimeError("GetClientRect失败")

    point = _POINT(0, 0)
    if not user32.ClientToScreen(
        ctypes.c_void_p(int(hwnd)),
        ctypes.byref(point),
    ):
        raise RuntimeError("ClientToScreen失败")

    width = int(rect.right - rect.left)
    height = int(rect.bottom - rect.top)

    return (
        int(point.x),
        int(point.y),
        width,
        height,
    )


def _win32_window_title(hwnd):
    if os.name != "nt":
        return ""

    user32 = ctypes.windll.user32
    length = int(
        user32.GetWindowTextLengthW(
            ctypes.c_void_p(int(hwnd))
        )
    )

    if length <= 0:
        return ""

    buf = ctypes.create_unicode_buffer(
        length + 1
    )

    user32.GetWindowTextW(
        ctypes.c_void_p(int(hwnd)),
        buf,
        length + 1,
    )

    return buf.value


def _win32_window_class(hwnd):
    if os.name != "nt":
        return ""

    user32 = ctypes.windll.user32
    buf = ctypes.create_unicode_buffer(256)

    length = user32.GetClassNameW(
        ctypes.c_void_p(int(hwnd)),
        buf,
        len(buf),
    )

    if length <= 0:
        return ""

    return buf.value


def _win32_window_pid(hwnd):
    if os.name != "nt":
        return 0

    user32 = ctypes.windll.user32
    pid = ctypes.c_ulong(0)

    user32.GetWindowThreadProcessId(
        ctypes.c_void_p(int(hwnd)),
        ctypes.byref(pid),
    )

    return int(pid.value)


def _select_scrcpy_window_candidate(
    candidates,
    target_title,
    proc_pid=None,
    device_size=None,
):
    """
    V2.7:
    Never choose a terminal/console window merely because its title
    contains the unique scrcpy token and its area is larger.
    """
    if not candidates:
        return None

    target = str(target_title or "").strip().lower()
    ranked = []

    for item in candidates:
        title = str(item.get("title", "")).strip()
        title_lower = title.lower()

        if not title or target not in title_lower:
            continue

        cls = str(item.get("class", "")).strip()
        cls_lower = cls.lower()

        # Exact bug from user's runtime:
        # "scrcpy - HorseRaceScrcpy-..." was a console window.
        if cls_lower == "consolewindowclass":
            continue

        if title_lower.startswith("scrcpy - "):
            continue

        w = int(item.get("w", 0) or 0)
        h = int(item.get("h", 0) or 0)

        if w < 120 or h < 180:
            continue

        exact = title_lower == target

        pid_match = (
            proc_pid is not None
            and int(item.get("pid", 0) or 0) == int(proc_pid)
        )

        score = 0.0

        if exact:
            score += 1000.0

        if pid_match:
            score += 500.0

        # Real phone video is portrait in this game.
        if h > w:
            score += 50.0

        if device_size is not None:
            try:
                dw = float(device_size[0])
                dh = float(device_size[1])

                if dw > 0 and dh > 0 and h > 0:
                    expected = dw / dh
                    actual = float(w) / float(h)
                    score -= abs(actual - expected) * 100.0
            except Exception:
                pass

        ranked.append((score, item))

    if not ranked:
        return None

    ranked.sort(
        key=lambda pair: pair[0],
        reverse=True,
    )

    return ranked[0][1]



def enum_windows():
    """枚举可见Windows顶层窗口。"""
    if os.name != "nt":
        return []

    user32 = ctypes.windll.user32
    results = []

    CALLBACK = ctypes.WINFUNCTYPE(
        ctypes.c_bool,
        ctypes.c_void_p,
        ctypes.c_void_p,
    )

    @CALLBACK
    def _callback(hwnd, lparam):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True

            title = _win32_window_title(
                int(hwnd)
            )

            if not title:
                return True

            x, y, w, h = _win32_client_rect(
                int(hwnd)
            )

            if w <= 0 or h <= 0:
                return True

            results.append(
                {
                    "hwnd": int(hwnd),
                    "title": title,
                    "class": _win32_window_class(int(hwnd)),
                    "pid": _win32_window_pid(int(hwnd)),
                    "x": x,
                    "y": y,
                    "w": w,
                    "h": h,
                }
            )
        except Exception:
            pass

        return True

    user32.EnumWindows(
        _callback,
        0,
    )

    return results


class ScrcpyWindowManager:
    def __init__(self):
        self.proc = None
        self._last_launch_at = 0.0
        self._lock = threading.RLock()

    def _find_exact(self):
        proc_pid = None

        if (
            self.proc is not None
            and self.proc.poll() is None
        ):
            proc_pid = int(self.proc.pid)

        device_size = None

        if globals().get("ADB_DEVICE") is not None:
            device_size = getattr(
                ADB_DEVICE,
                "_input_size",
                None,
            )

        return _select_scrcpy_window_candidate(
            enum_windows(),
            SCRCPY_WINDOW_TITLE,
            proc_pid=proc_pid,
            device_size=device_size,
        )


    def _resolve_scrcpy_path(self):
        candidates = []

        if SCRCPY_PATH:
            candidates.append(
                Path(SCRCPY_PATH)
            )

        found = shutil.which(
            "scrcpy"
        )

        if found:
            candidates.append(
                Path(found)
            )

        for candidate in candidates:
            try:
                if candidate.exists():
                    return str(
                        candidate.resolve()
                    )
            except Exception:
                pass

        return None

    def launch(self):
        if not SCRCPY_AUTO_LAUNCH:
            return None

        path = self._resolve_scrcpy_path()

        if path is None:
            raise RuntimeError(
                "未找到scrcpy.exe；默认路径是 "
                r"C:\scrcpy\scrcpy.exe"
                "。也可以设置 SCRCPY_PATH 环境变量。"
            )

        if ADB_DEVICE is None:
            raise RuntimeError(
                "ADB_DEVICE尚未初始化"
            )

        cmd = [
            path,
            "-s",
            ADB_DEVICE.serial,
            "--no-audio",
            "--no-control",
            f"--max-size={SCRCPY_MAX_SIZE}",
            f"--max-fps={SCRCPY_MAX_FPS}",
            f"--video-bit-rate={SCRCPY_VIDEO_BITRATE}",
            f"--window-title={SCRCPY_WINDOW_TITLE}",
            "--window-x=20",
            "--window-y=20",
            "--window-width=425",
            "--window-height=937",
            "--always-on-top",
        ]

        log(
            "[scrcpy] 启动本次专用镜像窗口："
            f"title={SCRCPY_WINDOW_TITLE}；"
            f"max-size={SCRCPY_MAX_SIZE}，"
            f"max-fps={SCRCPY_MAX_FPS}，"
            f"bit-rate={SCRCPY_VIDEO_BITRATE}；"
            "初始窗口=425x937 @ (20,20)"
        )

        event_log(
            "scrcpy_launch",
            command=cmd,
        )

        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(
                subprocess,
                "CREATE_NEW_PROCESS_GROUP",
                0,
            )

        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )

        self._last_launch_at = (
            time.monotonic()
        )

        deadline = (
            time.monotonic()
            + SCRCPY_START_TIMEOUT
        )

        while time.monotonic() < deadline:
            item = self._find_exact()

            if item is not None:
                return item

            if (
                self.proc is not None
                and self.proc.poll()
                is not None
            ):
                raise RuntimeError(
                    "scrcpy进程启动后立即退出，"
                    f"exit={self.proc.returncode}"
                )

            time.sleep(0.10)

        raise RuntimeError(
            f"{SCRCPY_START_TIMEOUT:.0f}秒内未找到"
            f"scrcpy窗口：{SCRCPY_WINDOW_TITLE}"
        )

    def ensure_window(self):
        with self._lock:
            return self._ensure_window_locked()

    def _ensure_window_locked(self):
        item = self._find_exact()

        if item is not None:
            return item

        # A minimized scrcpy window has a 0x0 client at (-32000,-32000).
        # enum_windows deliberately excludes it; restore the owned process
        # before deciding that a second mirror must be launched.
        if os.name == "nt" and self.proc is not None and self.proc.poll() is None:
            user32 = ctypes.windll.user32
            owned_hwnds = []
            callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

            @callback_type
            def collect(hwnd, _):
                handle = int(hwnd)
                if (_win32_window_title(handle) == SCRCPY_WINDOW_TITLE
                        and _win32_window_class(handle).lower() != "consolewindowclass"):
                    owned_hwnds.append(handle)
                return True

            user32.EnumWindows(collect, 0)
            for handle in owned_hwnds:
                if user32.IsIconic(ctypes.c_void_p(handle)):
                    user32.ShowWindow(ctypes.c_void_p(handle), 9)  # SW_RESTORE
                for _ in range(6):
                    time.sleep(0.15)
                    item = self._find_exact()
                    if item is not None:
                        log("[scrcpy恢复] 已恢复原镜像窗口，无需启动第二个窗口")
                        return item

            # The owned process no longer has a usable window. Replace it
            # once; do not leave an unseen old mirror running alongside it.
            self.stop_owned()

        return self.launch()

    def stop_owned(self):
        proc = self.proc
        self.proc = None

        if proc is None:
            return

        try:
            proc.terminate()
            proc.wait(timeout=2.0)
        except Exception:
            pass


def _fit_client_rect_to_screen(
    x,
    y,
    w,
    h,
    screen_w,
    screen_h,
    margin=8,
):
    """
    Pure geometry helper used by V2.2.
    Returns a client rect guaranteed to fit in the target DXGI output.
    """
    x = int(x)
    y = int(y)
    w = max(1, int(w))
    h = max(1, int(h))
    screen_w = max(1, int(screen_w))
    screen_h = max(1, int(screen_h))
    margin = max(0, int(margin))

    max_w = max(
        100,
        screen_w
        - margin * 2,
    )
    max_h = max(
        100,
        screen_h
        - margin * 2,
    )

    if (
        w > max_w
        or h > max_h
    ):
        scale = min(
            max_w / float(w),
            max_h / float(h),
        )

        w = max(
            100,
            int(
                round(
                    w * scale
                )
            ),
        )
        h = max(
            100,
            int(
                round(
                    h * scale
                )
            ),
        )

    max_x = max(
        margin,
        screen_w
        - margin
        - w,
    )
    max_y = max(
        margin,
        screen_h
        - margin
        - h,
    )

    x = min(
        max(
            x,
            margin,
        ),
        max_x,
    )
    y = min(
        max(
            y,
            margin,
        ),
        max_y,
    )

    return (
        int(x),
        int(y),
        int(w),
        int(h),
    )


def _primary_screen_size():
    if os.name != "nt":
        return (
            1920,
            1080,
        )

    user32 = ctypes.windll.user32

    return (
        int(
            user32.GetSystemMetrics(
                0
            )
        ),
        int(
            user32.GetSystemMetrics(
                1
            )
        ),
    )


def _fit_scrcpy_window_into_primary(
    hwnd,
):
    """
    Move/resize the actual scrcpy window so its CLIENT AREA is fully
    inside dxcam output 0 (the primary monitor).

    This directly fixes:
        client=1200x623 screen=(782,296)
        -> right=1982 > primary width=1920
        -> dxcam Invalid Region
    """
    if os.name != "nt":
        return _win32_client_rect(
            hwnd
        )

    user32 = ctypes.windll.user32

    if not user32.IsWindow(ctypes.c_void_p(int(hwnd))):
        raise RuntimeError("scrcpy窗口句柄已失效")

    if user32.IsIconic(ctypes.c_void_p(int(hwnd))):
        user32.ShowWindow(ctypes.c_void_p(int(hwnd)), 9)  # SW_RESTORE
        time.sleep(0.15)

    x, y, w, h = (
        _win32_client_rect(
            hwnd
        )
    )

    if w < 100 or h < 100:
        raise RuntimeError(f"scrcpy窗口尚未恢复有效客户区：{w}x{h}@({x},{y})")

    screen_w, screen_h = (
        _primary_screen_size()
    )

    desired_x, desired_y, desired_w, desired_h = (
        _fit_client_rect_to_screen(
            x,
            y,
            w,
            h,
            screen_w,
            screen_h,
            margin=8,
        )
    )

    already_valid = (
        x >= 0
        and y >= 0
        and x + w <= screen_w
        and y + h <= screen_h
        and w == desired_w
        and h == desired_h
    )

    if already_valid:
        return (
            x,
            y,
            w,
            h,
        )

    outer = _RECT()

    if not user32.GetWindowRect(
        ctypes.c_void_p(
            int(
                hwnd
            )
        ),
        ctypes.byref(
            outer
        ),
    ):
        raise RuntimeError(
            "GetWindowRect失败，无法修正scrcpy窗口"
        )

    outer_w = int(
        outer.right
        - outer.left
    )
    outer_h = int(
        outer.bottom
        - outer.top
    )

    border_w = max(
        0,
        outer_w
        - w,
    )
    border_h = max(
        0,
        outer_h
        - h,
    )

    new_outer_left = int(
        outer.left
        + (
            desired_x
            - x
        )
    )
    new_outer_top = int(
        outer.top
        + (
            desired_y
            - y
        )
    )

    new_outer_w = int(
        desired_w
        + border_w
    )
    new_outer_h = int(
        desired_h
        + border_h
    )

    SWP_NOZORDER = 0x0004
    SWP_NOACTIVATE = 0x0010

    ok = user32.SetWindowPos(
        ctypes.c_void_p(
            int(
                hwnd
            )
        ),
        None,
        new_outer_left,
        new_outer_top,
        new_outer_w,
        new_outer_h,
        SWP_NOZORDER
        | SWP_NOACTIVATE,
    )

    if not ok:
        raise RuntimeError(
            "SetWindowPos失败，无法修正scrcpy窗口"
        )

    time.sleep(
        0.04
    )

    nx, ny, nw, nh = (
        _win32_client_rect(
            hwnd
        )
    )

    if (
        nw < 100
        or nh < 100
        or nx < 0
        or ny < 0
        or nx + nw > screen_w
        or ny + nh > screen_h
    ):
        raise RuntimeError(
            "scrcpy窗口自动归位后仍超出DXGI输出："
            f"client={nw}x{nh} screen=({nx},{ny})；"
            f"DXGI={screen_w}x{screen_h}"
        )

    log(
        "[V2.2 DXGI修复] scrcpy窗口已自动归位："
        f"{w}x{h}@({x},{y}) -> "
        f"{nw}x{nh}@({nx},{ny})；"
        f"DXGI输出={screen_w}x{screen_h}"
    )

    return (
        nx,
        ny,
        nw,
        nh,
    )




def _v25_phone_roi_in_client(
    x,
    y,
    w,
    h,
    device_w,
    device_h,
):
    """
    Return absolute screen ROI of the real phone image inside scrcpy client.
    """
    x = int(x)
    y = int(y)
    w = max(
        1,
        int(w),
    )
    h = max(
        1,
        int(h),
    )

    device_w = float(
        max(
            1,
            int(device_w),
        )
    )
    device_h = float(
        max(
            1,
            int(device_h),
        )
    )

    device_aspect = (
        device_w
        / device_h
    )

    client_aspect = (
        float(w)
        / float(h)
    )

    if client_aspect > device_aspect * 1.05:
        phone_h = h
        phone_w = max(
            2,
            min(
                w,
                int(
                    round(
                        h
                        * device_aspect
                    )
                ),
            ),
        )

        local_x = int(
            round(
                (
                    w
                    - phone_w
                )
                / 2.0
            )
        )
        local_y = 0

    elif client_aspect < device_aspect / 1.05:
        phone_w = w
        phone_h = max(
            2,
            min(
                h,
                int(
                    round(
                        w
                        / device_aspect
                    )
                ),
            ),
        )

        local_x = 0
        local_y = int(
            round(
                (
                    h
                    - phone_h
                )
                / 2.0
            )
        )

    else:
        local_x = 0
        local_y = 0
        phone_w = w
        phone_h = h

    return (
        int(
            x
            + local_x
        ),
        int(
            y
            + local_y
        ),
        int(
            phone_w
        ),
        int(
            phone_h
        ),
    )




class ScrcpyDXCapture:
    def __init__(self):
        self.camera = None
        self.region = None
        self.hwnd = None
        self._lock = threading.RLock()
        self._last_geometry_check = 0.0
        self._last_frame = None
        self._last_frame_at = 0.0

    def _ensure_camera(self):
        if not DXCAM_AVAILABLE:
            raise RuntimeError(
                "缺少dxcam。请执行："
                "python -m pip install dxcam"
            )

        if self.camera is None:
            self.camera = dxcam.create(
                output_color="RGB",
                max_buffer_len=2,
            )

        return self.camera

    def _refresh_region(
        self,
        hwnd,
        force=False,
    ):
        now = time.monotonic()

        if (
            not force
            and self.hwnd == hwnd
            and self.region is not None
            and now - self._last_geometry_check
            < SCRCPY_GEOMETRY_REFRESH
        ):
            return

        # V2.2：先保证scrcpy客户端完整落在dxcam output 0里。
        x, y, w, h = (
            _fit_scrcpy_window_into_primary(
                hwnd
            )
        )

        if w < 100 or h < 100:
            raise RuntimeError(
                f"scrcpy客户端区域异常：{w}x{h}"
            )

        screen_w, screen_h = (
            _primary_screen_size()
        )

        device_w, device_h = getattr(
            globals().get(
                "ADB_DEVICE"
            ),
            "_input_size",
            (
                1264,
                2780,
            ),
        )

        (
            phone_x,
            phone_y,
            phone_w,
            phone_h,
        ) = _v25_phone_roi_in_client(
            x,
            y,
            w,
            h,
            device_w,
            device_h,
        )

        # V2.5：DXGI直接只抓真实手机视口，不再先复制完整1200x623再裁剪。
        new_region = (
            int(
                phone_x
            ),
            int(
                phone_y
            ),
            int(
                phone_x
                + phone_w
            ),
            int(
                phone_y
                + phone_h
            ),
        )

        if not (
            0
            <= new_region[
                0
            ]
            < new_region[
                2
            ]
            <= screen_w
            and 0
            <= new_region[
                1
            ]
            < new_region[
                3
            ]
            <= screen_h
        ):
            raise RuntimeError(
                "[V2.2] DXGI区域越界："
                f"region={new_region}；"
                f"output={screen_w}x{screen_h}"
            )

        if (
            self.region != new_region
            or self.hwnd != hwnd
        ):
            event_log(
                "scrcpy_geometry",
                hwnd=int(hwnd),
                x=int(x),
                y=int(y),
                width=int(w),
                height=int(h),
                dxgi_width=int(
                    screen_w
                ),
                dxgi_height=int(
                    screen_h
                ),
            )

            log(
                f"[V2.5 DXGI直裁] client={w}x{h}@({x},{y}) "
                f"-> phoneROI={phone_w}x{phone_h}@({phone_x},{phone_y})；"
                f"DXGI={screen_w}x{screen_h}"
            )

        self.region = new_region
        self.hwnd = hwnd
        self._last_geometry_check = now


    def reset_geometry(self):
        with self._lock:
            self.region = None
            self.hwnd = None
            self._last_frame = None
            self._last_frame_at = 0.0
            self._last_geometry_check = 0.0

    def grab(
        self,
        hwnd,
    ):
        with self._lock:
            self._ensure_camera()

            last_exc = None

            for attempt in range(
                2
            ):
                try:
                    self._refresh_region(
                        hwnd,
                        force=(
                            attempt
                            > 0
                        ),
                    )

                    frame = self.camera.grab(
                        region=self.region
                    )

                    if (
                        frame is None
                        or getattr(
                            frame,
                            "size",
                            0,
                        )
                        == 0
                    ):
                        if (
                            self._last_frame
                            is not None
                            and time.monotonic()
                            - self._last_frame_at
                            < 0.12
                        ):
                            return (
                                self._last_frame.copy()
                            )

                        raise RuntimeError(
                            "DXGI暂时没有返回scrcpy新画面"
                        )

                    frame = (
                        np.ascontiguousarray(
                            frame
                        )
                    )

                    self._last_frame = (
                        frame
                    )
                    self._last_frame_at = (
                        time.monotonic()
                    )

                    return frame

                except ValueError as exc:
                    last_exc = exc

                    # dxcam Invalid Region：强制重新读取Win32几何并归位。
                    log(
                        "[V2.2 DXGI修复] "
                        "检测到Invalid Region，"
                        "立即重新归位scrcpy窗口并重试"
                    )

                    self.region = None
                    self.hwnd = None
                    self._last_geometry_check = 0.0

                    time.sleep(
                        0.03
                    )

                except Exception as exc:
                    last_exc = exc

                    if attempt == 0:
                        self.region = None
                        self.hwnd = None
                        self._last_geometry_check = 0.0
                        time.sleep(
                            0.02
                        )
                        continue

                    break

            raise RuntimeError(
                "DXGI抓取失败："
                f"{type(last_exc).__name__}: {last_exc}"
            )


    def close(self):
        with self._lock:
            if self.camera is not None:
                try:
                    self.camera.stop()
                except Exception:
                    pass

            self.camera = None
            self.region = None
            self.hwnd = None
            self._last_frame = None


SCRCPY_MANAGER = ScrcpyWindowManager()
SCRCPY_CAPTURE = ScrcpyDXCapture()

_V23_LAST_VIEWPORT_LOG = None


def _ensure_adb():
    if ADB_DEVICE is None:
        raise RuntimeError(
            "ADB_DEVICE尚未初始化"
        )


def capture_item(
    item,
):
    return capture_direct(
        item["hwnd"]
    )[0]


def capture_direct(
    hwnd,
):
    """
    抓取scrcpy客户端区域，再按旧Android算法裁出小游戏视口。

    视觉帧尺寸可能是约491x1080，但ADB点击仍映射回1080x2376。
    """
    _ensure_adb()

    full_image = SCRCPY_CAPTURE.grab(
        int(hwnd)
    )

    if full_image is None:
        raise RuntimeError("DXGI抓取返回空帧 full_image=None")

    if not hasattr(full_image, "shape") or getattr(full_image, "size", 0) <= 0:
        raise RuntimeError("DXGI抓取返回非法帧")

    image = ADB_DEVICE._crop_viewport(
        full_image
    )

    global _V23_LAST_VIEWPORT_LOG

    try:
        viewport_log_key = (
            int(
                full_image.shape[
                    1
                ]
            ),
            int(
                full_image.shape[
                    0
                ]
            ),
            tuple(
                int(v)
                for v
                in ADB_DEVICE.last_viewport
            ),
        )

        if globals().get(
            "_V23_LAST_VIEWPORT_LOG"
        ) != viewport_log_key:
            _V23_LAST_VIEWPORT_LOG = (
                viewport_log_key
            )

            vx, vy, vw, vh = (
                ADB_DEVICE.last_viewport
            )

            log(
                "[V2.3真实视口] "
                f"scrcpy-client={full_image.shape[1]}x{full_image.shape[0]} "
                f"-> phone={vw}x{vh}；"
                f"crop=({vx},{vy})~({vx+vw},{vy+vh})；"
                f"Android={ADB_DEVICE._input_size[0]}x{ADB_DEVICE._input_size[1]}"
            )
    except Exception:
        pass

    if image is None:
        raise RuntimeError("裁剪游戏窗口返回空帧 image=None")

    if not hasattr(image, "shape") or getattr(image, "size", 0) <= 0:
        raise RuntimeError("裁剪后游戏帧非法")

    box = (
        ADB_DEVICE.last_viewport
        or (
            0,
            0,
            image.shape[1],
            image.shape[0],
        )
    )

    return (
        image,
        tuple(
            int(v)
            for v in box
        ),
    )


def capture(
    hwnd,
):
    engine = globals().get(
        "VISION_ENGINE"
    )

    if (
        engine is not None
        and engine.hwnd == hwnd
        and engine._threads
    ):
        latest = engine.wait_for_frame(
            timeout=SCRCPY_FRAME_TIMEOUT,
            max_age=SCRCPY_FRAME_MAX_AGE,
        )

        if latest is not None:
            return latest

        diag = engine.diagnostic_snapshot()

        raise RuntimeError(
            "等待scrcpy/DXGI实时帧超时；"
            f"backend={getattr(engine, 'capture_backend', 'unknown')}，"
            f"seq={diag.get('frame_seq', 0)}，"
            f"fps={diag.get('fps', 0.0):.1f}"
        )

    return capture_direct(
        hwnd
    )

def activate(
    hwnd,
):
    # 手机前台由用户保持在小游戏，无需Windows激活窗口。
    return


def get_client_geometry(
    hwnd,
):
    _ensure_adb()

    if ADB_DEVICE.last_viewport is None:
        image, _ = capture_direct(
            hwnd
        )

    return tuple(
        int(value)
        for value in ADB_DEVICE.last_viewport
    )

def _invalidate_async_frame():
    engine = globals().get(
        "VISION_ENGINE"
    )

    if engine is None:
        return

    try:
        engine.invalidate_after_action()
    except Exception:
        pass


def click_client(
    hwnd,
    x,
    y,
    wait=0.0,
):
    _ensure_adb()

    device_x, device_y = (
        ADB_DEVICE.viewport_to_device(
            x,
            y,
        )
    )

    started = time.perf_counter()

    ok = ADB_DEVICE.tap(
        device_x,
        device_y,
    )

    tap_ms = (
        time.perf_counter()
        - started
    ) * 1000.0

    _invalidate_async_frame()

    event_log(
        "tap",
        viewport_x=round(float(x), 2),
        viewport_y=round(float(y), 2),
        device_x=round(float(device_x), 2),
        device_y=round(float(device_y), 2),
        ok=bool(ok),
        tap_ms=round(tap_ms, 2),
        wait=float(wait),
    )

    if not ok:
        log(
            "[ADB操作] tap发送失败"
        )
    elif tap_ms >= 80.0:
        log(
            f"[ADB操作] tap耗时偏高：{tap_ms:.0f}ms "
            f"device=({device_x:.0f},{device_y:.0f})"
        )

    if wait:
        time.sleep(
            wait
        )

    return bool(ok)


def click_match_button_safe(
    hwnd,
    image,
    button,
):
    """
    V2.8 safety-critical match tap.

    The match page detector works in the cropped phone image coordinate
    system. For the lobby START button, map its normalized image position
    straight to Android wm-size coordinates instead of depending on
    last_viewport.

    This prevents a correct visual detection from landing on the right-side
    “牌型” button when the Windows/scrcpy viewport mapping is stale.
    """
    _ensure_adb()

    if (
        image is None
        or getattr(image, "size", 0) <= 0
        or button is None
    ):
        return False

    H, W = image.shape[:2]

    layout = str(
        button.get(
            "layout",
            "",
        )
    )

    # Known page layouts: use the true normalized Android target.
    if layout.startswith("quick_card"):
        nx = 0.806
        ny = 0.840

        # Pre-tap safety: the real START button must still be green.
        local_green = _green_dominance_ratio(
            image,
            0.70,
            0.82,
            0.92,
            0.87,
        )

        if local_green < 0.28:
            log(
                "[V2.8安全点击] quick_card目标区绿色不足，"
                f"green={local_green:.3f}；拒绝点击"
            )
            return False

    elif layout.startswith("weekly_center"):
        nx = 0.500
        ny = 0.863

        local_green = _green_dominance_ratio(
            image,
            0.31,
            0.835,
            0.69,
            0.895,
        )

        if local_green < 0.24:
            log(
                "[V2.8安全点击] weekly_center目标区绿色不足，"
                f"green={local_green:.3f}；拒绝点击"
            )
            return False

    else:
        nx = float(
            button.get(
                "cx",
                W * 0.5,
            )
        ) / float(max(W, 1))

        ny = float(
            button.get(
                "cy",
                H * 0.5,
            )
        ) / float(max(H, 1))

        local_green = 1.0

    # Hard geometry guard: lobby START is always in the lower page region.
    if not (
        0.25 <= nx <= 0.95
        and 0.78 <= ny <= 0.91
    ):
        log(
            "[V2.8安全点击] 开始匹配坐标越界，拒绝点击："
            f"norm=({nx:.3f},{ny:.3f})"
        )
        return False

    # Refresh once at the action boundary. This is cheap compared with
    # a wrong click, and it catches a current Android Override size.
    try:
        ADB_DEVICE.wm_size()
    except Exception:
        pass

    dw, dh = getattr(
        ADB_DEVICE,
        "_input_size",
        (
            1080,
            2400,
        ),
    )

    device_x = (
        float(
            dw
        )
        * float(
            nx
        )
    )
    device_y = (
        float(
            dh
        )
        * float(
            ny
        )
    )

    started = time.perf_counter()

    ok = ADB_DEVICE.tap(
        device_x,
        device_y,
    )

    tap_ms = (
        time.perf_counter()
        - started
    ) * 1000.0

    _invalidate_async_frame()

    log(
        "[点击] 开始匹配 "
        f"→ Android({device_x:.0f},{device_y:.0f})"
    )

    event_log(
        "match_tap_safe",
        layout=layout,
        frame_w=int(W),
        frame_h=int(H),
        norm_x=round(float(nx), 5),
        norm_y=round(float(ny), 5),
        device_x=round(float(device_x), 2),
        device_y=round(float(device_y), 2),
        green=round(float(local_green), 4),
        ok=bool(ok),
        tap_ms=round(float(tap_ms), 2),
    )

    return bool(ok)



def click_rel(
    hwnd,
    rx,
    ry,
    wait=0.0,
):
    _, _, w, h = get_client_geometry(
        hwnd
    )

    return click_client(
        hwnd,
        w * rx,
        h * ry,
        wait=wait,
    )


def template_match(image, template, threshold=0.70, roi=None):
    H, W = image.shape[:2]
    if roi is None:
        x0, y0, x1, y1 = 0, 0, W, H
    else:
        x0, y0, x1, y1 = [int(v) for v in roi]
        x0 = max(0, x0)
        y0 = max(0, y0)
        x1 = min(W, x1)
        y1 = min(H, y1)

    search = image[y0:y1, x0:x1]
    if search.size == 0:
        return None

    search_gray = cv2.cvtColor(search, cv2.COLOR_RGB2GRAY)
    tpl_gray = cv2.cvtColor(template, cv2.COLOR_RGB2GRAY)

    base_scale = W / REFERENCE_W
    scales = base_scale * np.array([0.96, 1.00, 1.04])

    best = None
    for scale in scales:
        tw = int(round(tpl_gray.shape[1] * scale))
        th = int(round(tpl_gray.shape[0] * scale))
        if tw < 8 or th < 8:
            continue
        if tw >= search_gray.shape[1] or th >= search_gray.shape[0]:
            continue

        resized = cv2.resize(
            tpl_gray,
            (tw, th),
            interpolation=cv2.INTER_AREA if scale < 1.0 else cv2.INTER_CUBIC,
        )

        result = cv2.matchTemplate(
            search_gray,
            resized,
            cv2.TM_CCOEFF_NORMED,
        )
        _, score, _, loc = cv2.minMaxLoc(result)

        if best is None or score > best["score"]:
            xx = loc[0] + x0
            yy = loc[1] + y0
            best = {
                "score": float(score),
                "rect": (xx, yy, tw, th),
                "center": (xx + tw / 2.0, yy + th / 2.0),
            }

    if best is None or best["score"] < threshold:
        return None
    return best

def find_game_window():
    """
    V1.9：
    “游戏窗口”就是脚本专用的scrcpy镜像窗口。
    """
    _ensure_adb()

    item = SCRCPY_MANAGER.ensure_window()

    if item is None:
        return None

    return {
        "hwnd": int(
            item["hwnd"]
        ),
        "title": item["title"],
        "class": "scrcpy",
        "x": int(item["x"]),
        "y": int(item["y"]),
        "w": int(item["w"]),
        "h": int(item["h"]),
    }


def save_debug(image, name):
    """Save bounded phone frames when a state cannot be confirmed."""
    if image is None or getattr(image, "size", 0) <= 0:
        return None
    now = time.monotonic()
    with _LOG_LOCK:
        if now - _DEBUG_SAVED_AT.get(name, 0.0) < 20.0:
            return None
        _DEBUG_SAVED_AT[name] = now
        folder = _session_paths()
        if len(list(folder.glob("*.jpg"))) >= 80:
            return None
        path = folder / f"{time.strftime('%H%M%S')}_{name}.jpg"
        bgr = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
        if cv2.imwrite(str(path), bgr):
            log(f"[诊断截图] {path}")
            return path
    return None


# ------------------------------------------------------------------
# Card recognition
# ------------------------------------------------------------------

def _detect_card_left_edges(image):
    H, W = image.shape[:2]

    y0 = int(H * 0.58)
    y1 = int(H * 0.94)

    gray = cv2.cvtColor(
        image[y0:y1],
        cv2.COLOR_RGB2GRAY,
    )

    edges = cv2.Canny(gray, 50, 150)
    strength = edges.sum(axis=0) / 255.0

    threshold = max(
        55.0 * (H / REFERENCE_H),
        (y1 - y0) * 0.43,
    )

    candidates = np.where(
        strength >= threshold
    )[0].tolist()

    clusters = []
    for x in candidates:
        if (
            not clusters
            or x - clusters[-1][-1] > max(4, int(W * 0.010))
        ):
            clusters.append([x])
        else:
            clusters[-1].append(x)

    reps = []
    for cluster in clusters:
        rep = max(
            cluster,
            key=lambda xx: strength[xx],
        )
        reps.append(rep)

    # Find the longest roughly arithmetic series of card left edges.
    best = []
    min_step = W * 0.052
    max_step = W * 0.115

    for i, x0 in enumerate(reps):
        seq = [x0]
        last = x0
        steps = []

        for x in reps[i + 1:]:
            d = x - last

            if (
                min_step <= d <= max_step
                and (
                    not steps
                    or abs(d - np.median(steps))
                    <= W * 0.020
                )
            ):
                seq.append(x)
                steps.append(d)
                last = x

            elif d > W * 0.14 and len(seq) >= 2:
                break

        if len(seq) > len(best):
            best = seq

    # Regularize one noisy edge by fitting x_i = intercept + spacing*i.
    if 3 <= len(best) <= 10:
        slope, intercept = np.polyfit(
            np.arange(len(best), dtype=float),
            np.asarray(best, dtype=float),
            1,
        )

        if W * 0.055 <= slope <= W * 0.115:
            best = [
                int(round(intercept + slope * i))
                for i in range(len(best))
            ]

    if not (1 <= len(best) <= 10):
        return []

    return best

def _card_number_patch(image, left_x):
    H, W = image.shape[:2]

    y = int(H * 0.694)

    x0 = max(
        0,
        int(left_x - 2 * W / REFERENCE_W),
    )
    x1 = min(
        W,
        int(left_x + 36 * W / REFERENCE_W),
    )

    y1 = min(
        H,
        int(y + 31 * H / REFERENCE_H),
    )

    patch = image[y:y1, x0:x1]

    if patch.size == 0:
        return None

    return cv2.resize(
        patch,
        (38, 31),
        interpolation=cv2.INTER_AREA,
    )

def _binary_number_patch(patch):
    gray = cv2.cvtColor(
        patch,
        cv2.COLOR_RGB2GRAY,
    )

    _, binary = cv2.threshold(
        gray,
        185,
        255,
        cv2.THRESH_BINARY_INV,
    )

    # Focus on the actual number / top-left corner.
    binary[27:, :] = 0
    binary[:, 32:] = 0

    return binary

DIGIT_BINARY = {
    value: [
        _binary_number_patch(sample)
        for sample in samples
    ]
    for value, samples in DIGIT_SAMPLES.items()
}

def _binary_similarity(a, b):
    aa = (a.astype(np.float32) / 255.0)
    bb = (b.astype(np.float32) / 255.0)

    best = -1.0

    for dy in range(-2, 3):
        for dx in range(-2, 3):
            shifted = np.roll(
                np.roll(bb, dy, axis=0),
                dx,
                axis=1,
            )

            if dy > 0:
                shifted[:dy, :] = 0
            elif dy < 0:
                shifted[dy:, :] = 0

            if dx > 0:
                shifted[:, :dx] = 0
            elif dx < 0:
                shifted[:, dx:] = 0

            av = aa.ravel()
            bv = shifted.ravel()

            if av.std() < 1e-6 or bv.std() < 1e-6:
                continue

            score = float(
                np.corrcoef(av, bv)[0, 1]
            )

            best = max(best, score)

    return best

_V31_DIGIT_GRAY = {
    int(value): [
        (
            cv2.cvtColor(
                sample,
                cv2.COLOR_RGB2GRAY,
            )[
                :25,
                :32,
            ].astype(
                np.float32
            )
        )
        for sample in samples
    ]
    for value, samples in DIGIT_SAMPLES.items()
}

for _value, _samples in _V31_DIGIT_GRAY.items():
    for _i in range(
        len(_samples)
    ):
        _g = _samples[
            _i
        ]
        _samples[
            _i
        ] = (
            _g
            - float(
                _g.mean()
            )
        ) / (
            float(
                _g.std()
            )
            + 1e-6
        )


def _v31_gray_digit_similarity(
    patch,
    sample,
):
    gray = cv2.cvtColor(
        patch,
        cv2.COLOR_RGB2GRAY,
    )[
        :25,
        :32,
    ].astype(
        np.float32
    )

    gray = (
        gray
        - float(
            gray.mean()
        )
    ) / (
        float(
            gray.std()
        )
        + 1e-6
    )

    # Direct alignment is highly stable in the user's scrcpy recording.
    best = float(
        np.mean(
            gray
            * sample
        )
    )

    # Only spend extra work on borderline matches.
    if best >= 0.82:
        return best

    for dy, dx in (
        (-1, 0),
        (1, 0),
        (0, -1),
        (0, 1),
    ):
        shifted = np.roll(
            np.roll(
                sample,
                dy,
                axis=0,
            ),
            dx,
            axis=1,
        ).copy()

        if dy > 0:
            shifted[:dy, :] = 0
        elif dy < 0:
            shifted[dy:, :] = 0

        if dx > 0:
            shifted[:, :dx] = 0
        elif dx < 0:
            shifted[:, dx:] = 0

        score = float(
            np.mean(
                gray
                * shifted
            )
        )

        if score > best:
            best = score

    return best



def _classify_value(patch):
    if (
        patch is None
        or getattr(
            patch,
            "size",
            0,
        )
        <= 0
        or not _V31_DIGIT_GRAY
    ):
        return (
            None,
            0.0,
            {},
        )

    scores = {}

    for value, samples in (
        _V31_DIGIT_GRAY.items()
    ):
        if not samples:
            continue

        scores[
            int(value)
        ] = max(
            _v31_gray_digit_similarity(
                patch,
                sample,
            )
            for sample in samples
        )

    if not scores:
        return (
            None,
            0.0,
            {},
        )

    best_value = max(
        scores,
        key=scores.get,
    )

    return (
        int(
            best_value
        ),
        float(
            scores[
                best_value
            ]
        ),
        scores,
    )


def _is_special_card(patch):
    hsv = cv2.cvtColor(
        patch,
        cv2.COLOR_RGB2HSV,
    )

    yellow = (
        (hsv[:, :, 0] >= 12)
        & (hsv[:, :, 0] <= 42)
        & (hsv[:, :, 1] >= 80)
        & (hsv[:, :, 2] >= 100)
    )

    return float(np.mean(yellow)) >= 0.13

def _read_hand_legacy(image):
    xs = _detect_card_left_edges(image)

    if not xs:
        return []

    cards = []

    for index, x in enumerate(xs):
        patch = _card_number_patch(
            image,
            x,
        )

        if patch is None:
            return []

        value, score, _ = _classify_value(
            patch
        )

        cards.append({
            "index": index,
            "value": value,
            "left_x": float(x),
            "click_x": float(
                x + 12 * image.shape[1] / REFERENCE_W
            ),
            "score": score,
            "special": _is_special_card(patch),
        })

    if not cards:
        return []

    avg = float(
        np.mean(
            [card["score"] for card in cards]
        )
    )

    min_score = min(
        card["score"]
        for card in cards
    )

    if (
        avg < float(CFG["card_avg_confidence"])
        or min_score < float(CFG["card_min_confidence"])
    ):
        return []

    return cards


_HAND_ADAPTIVE_LAST_LOG = 0.0


def _best_card_edge_series_from_band(
    image,
    y0_ratio,
    y1_ratio,
):
    """
    Android比赛页兜底卡牌边缘检测。
    """
    H, W = image.shape[:2]

    y0 = int(
        H * float(
            y0_ratio
        )
    )
    y1 = int(
        H * float(
            y1_ratio
        )
    )

    if y1 <= y0 + 20:
        return []

    gray = cv2.cvtColor(
        image[
            y0:y1
        ],
        cv2.COLOR_RGB2GRAY,
    )

    edges = cv2.Canny(
        gray,
        38,
        125,
    )

    strength = (
        edges.sum(
            axis=0
        )
        / 255.0
    )

    # 比旧版更宽松，但最终必须形成规律的卡牌间距序列。
    threshold = max(
        24.0
        * (
            H
            / REFERENCE_H
        ),
        (
            y1 - y0
        )
        * 0.20,
    )

    candidate_xs = np.where(
        strength
        >= threshold
    )[0].tolist()

    clusters = []

    for x in candidate_xs:
        if (
            not clusters
            or x
            - clusters[
                -1
            ][
                -1
            ]
            > max(
                4,
                int(
                    W * 0.009
                ),
            )
        ):
            clusters.append(
                [
                    x,
                ]
            )
        else:
            clusters[
                -1
            ].append(
                x
            )

    reps = []

    for cluster in clusters:
        reps.append(
            max(
                cluster,
                key=lambda xx: strength[
                    xx
                ],
            )
        )

    min_step = (
        W * 0.045
    )
    max_step = (
        W * 0.125
    )

    best = []

    for i, first in enumerate(
        reps
    ):
        seq = [
            first,
        ]
        last = first
        spacings = []

        for x in reps[
            i + 1:
        ]:
            d = (
                x
                - last
            )

            if (
                min_step
                <= d
                <= max_step
                and (
                    not spacings
                    or abs(
                        d
                        - np.median(
                            spacings
                        )
                    )
                    <= W
                    * 0.024
                )
            ):
                seq.append(
                    x
                )
                spacings.append(
                    d
                )
                last = x

            elif (
                d
                > W * 0.16
                and len(
                    seq
                ) >= 3
            ):
                break

        if len(
            seq
        ) > len(
            best
        ):
            best = seq

    if (
        3
        <= len(
            best
        )
        <= 10
    ):
        slope, intercept = np.polyfit(
            np.arange(
                len(
                    best
                ),
                dtype=float,
            ),
            np.asarray(
                best,
                dtype=float,
            ),
            1,
        )

        if (
            min_step
            <= slope
            <= max_step
        ):
            best = [
                int(
                    round(
                        intercept
                        + slope * i
                    )
                )
                for i in range(
                    len(
                        best
                    )
                )
            ]

    if not (
        3
        <= len(
            best
        )
        <= 10
    ):
        return []

    return best


def _card_number_patch_at_ratio(
    image,
    left_x,
    y_ratio,
):
    H, W = image.shape[:2]

    y = int(
        H
        * float(
            y_ratio
        )
    )

    x0 = max(
        0,
        int(
            left_x
            - 2
            * W
            / REFERENCE_W
        ),
    )
    x1 = min(
        W,
        int(
            left_x
            + 36
            * W
            / REFERENCE_W
        ),
    )

    y1 = min(
        H,
        int(
            y
            + 31
            * H
            / REFERENCE_H
        ),
    )

    patch = image[
        y:y1,
        x0:x1,
    ]

    if patch.size == 0:
        return None

    return cv2.resize(
        patch,
        (
            38,
            31,
        ),
        interpolation=cv2.INTER_AREA,
    )


def _classify_hand_at_y(
    image,
    xs,
    y_ratio,
):
    cards = []

    for index, x in enumerate(
        xs
    ):
        patch = _card_number_patch_at_ratio(
            image,
            x,
            y_ratio,
        )

        if patch is None:
            return (
                [],
                -1.0,
                -1.0,
            )

        value, score, _ = _classify_value(
            patch
        )

        cards.append(
            {
                "index": index,
                "value": value,
                "left_x": float(
                    x
                ),
                "click_x": float(
                    x
                    + 12
                    * image.shape[
                        1
                    ]
                    / REFERENCE_W
                ),
                "score": float(
                    score
                ),
                "special": _is_special_card(
                    patch
                ),
                "_android_digit_y_ratio": float(
                    y_ratio
                ),
            }
        )

    if not cards:
        return (
            [],
            -1.0,
            -1.0,
        )

    counts = Counter(
        int(
            card[
                "value"
            ]
        )
        for card in cards
    )

    # 一副牌同数字最多4张。
    if any(
        count > 4
        for count in counts.values()
    ):
        return (
            [],
            -1.0,
            -1.0,
        )

    avg = float(
        np.mean(
            [
                card[
                    "score"
                ]
                for card in cards
            ]
        )
    )
    minimum = float(
        min(
            card[
                "score"
            ]
            for card in cards
        )
    )

    return (
        cards,
        avg,
        minimum,
    )


_HAND_FAST_Y_RATIO = None


def _read_hand_android_adaptive(
    image,
):
    """
    Android比赛手牌自适应读取。

    先沿用旧X边缘；失败后用更宽区域重找X。
    数字Y不再写死0.694，而是在候选行中选模板相关性最高的一行。
    """
    xs = _detect_card_left_edges(
        image
    )

    if len(
        xs
    ) < 3:
        xs = _best_card_edge_series_from_band(
            image,
            ANDROID_HAND_EDGE_Y0,
            ANDROID_HAND_EDGE_Y1,
        )

    if not (
        3
        <= len(
            xs
        )
        <= 10
    ):
        return []

    # V2.0：绝大多数比赛过程中数字行Y不会变化。
    # 先直接尝试上一次成功的Y，成功时不再扫描8个候选Y。
    global _HAND_FAST_Y_RATIO

    avg_need = max(
        0.38,
        float(
            CFG[
                "card_avg_confidence"
            ]
        )
        - 0.08,
    )
    min_need = max(
        0.30,
        float(
            CFG[
                "card_min_confidence"
            ]
        )
        - 0.10,
    )

    if _HAND_FAST_Y_RATIO is not None:
        cached_cards, cached_avg, cached_min = (
            _classify_hand_at_y(
                image,
                xs,
                float(
                    _HAND_FAST_Y_RATIO
                ),
            )
        )

        if (
            cached_cards
            and cached_avg >= avg_need
            and cached_min >= min_need
        ):
            return cached_cards

    best = None

    # 先用最多前三张快速确定数字Y。
    probe_xs = xs[
        :min(
            3,
            len(
                xs
            ),
        )
    ]

    for y_ratio in ANDROID_HAND_DIGIT_Y_RATIOS:
        probe_cards, avg, minimum = _classify_hand_at_y(
            image,
            probe_xs,
            y_ratio,
        )

        if not probe_cards:
            continue

        score = (
            avg
            + minimum
            * 0.35
        )

        if (
            best is None
            or score
            > best[
                "score"
            ]
        ):
            best = {
                "y_ratio": float(
                    y_ratio
                ),
                "score": float(
                    score
                ),
            }

    if best is None:
        return []

    cards, avg, minimum = _classify_hand_at_y(
        image,
        xs,
        best[
            "y_ratio"
        ],
    )

    if not cards:
        return []

    # Android自适应路径略微放宽单帧门槛；
    # 多线程仍有2/3帧一致校验作为第二层保护。
    if (
        avg
        < avg_need
        or minimum
        < min_need
    ):
        return []

    _HAND_FAST_Y_RATIO = float(
        best[
            "y_ratio"
        ]
    )

    global _HAND_ADAPTIVE_LAST_LOG

    now = time.monotonic()

    if (
        now
        - _HAND_ADAPTIVE_LAST_LOG
        >= 4.0
    ):
        log(
            "[手牌] "
            f"识别{len(cards)}张，"
            f"digit_y={best['y_ratio']:.3f}，"
            f"avg={avg:.3f}，min={minimum:.3f}"
        )
        _HAND_ADAPTIVE_LAST_LOG = now

    return cards


def _v31_recover_hand_edges(
    image,
):
    xs = _detect_card_left_edges(
        image
    )

    H, W = image.shape[:2]

    # Single false edge at an extreme side is common when one card remains.
    if (
        len(xs) == 1
        and (
            xs[0] < W * 0.08
            or xs[0] > W * 0.88
        )
    ):
        xs = []

    if not xs:
        # One-card fallback: scan only when the fast geometric detector failed.
        candidates = []

        for x in range(
            0,
            max(
                1,
                W - 26,
            ),
            3,
        ):
            patch = _card_number_patch_at_ratio(
                image,
                x,
                0.728,
            )

            if patch is None:
                continue

            value, score, _ = _classify_value(
                patch
            )

            if (
                value is not None
                and score >= 0.80
            ):
                candidates.append(
                    (
                        float(score),
                        int(x),
                    )
                )

        candidates.sort(
            reverse=True
        )

        picked = []

        for score, x in candidates:
            if all(
                abs(
                    x - old_x
                )
                >= max(
                    16,
                    int(
                        W * 0.045
                    ),
                )
                for old_x in picked
            ):
                picked.append(
                    int(x)
                )

            if len(picked) >= 10:
                break

        picked.sort()

        return picked

    if len(xs) < 2:
        return xs

    spacing = float(
        np.median(
            np.diff(
                xs
            )
        )
    )

    if spacing <= 0:
        return xs

    y0 = int(
        H * 0.58
    )
    y1 = int(
        H * 0.94
    )

    gray = cv2.cvtColor(
        image[
            y0:y1
        ],
        cv2.COLOR_RGB2GRAY,
    )

    edges = cv2.Canny(
        gray,
        50,
        150,
    )

    strength = (
        edges.sum(
            axis=0
        )
        / 255.0
    )

    def strong(pred_x):
        xx = int(
            round(
                pred_x
            )
        )

        lo = max(
            0,
            xx - 5,
        )

        hi = min(
            W,
            xx + 6,
        )

        return (
            hi > lo
            and float(
                np.max(
                    strength[
                        lo:hi
                    ]
                )
            )
            >= 45.0
        )

    result = [
        float(x)
        for x in xs
    ]

    first = result[
        0
    ]

    while True:
        nxt = (
            first
            - spacing
        )

        if (
            nxt < 0
            or not strong(
                nxt
            )
        ):
            break

        result.insert(
            0,
            nxt,
        )
        first = nxt

    last = result[
        -1
    ]

    while True:
        nxt = (
            last
            + spacing
        )

        if (
            nxt >= W
            or not strong(
                nxt
            )
        ):
            break

        result.append(
            nxt
        )
        last = nxt

    return [
        int(
            round(
                x
            )
        )
        for x in result[
            :10
        ]
    ]


def _v31_read_hand_calibrated(
    image,
):
    # Never interpret the horse page as a race hand.
    if _v31_is_unselected_horse_page(
        image
    ):
        return []

    xs = _v31_recover_hand_edges(
        image
    )

    if not (
        1
        <= len(
            xs
        )
        <= 10
    ):
        return []

    cards = []
    scores = []

    for index, x in enumerate(
        xs
    ):
        patch = _card_number_patch_at_ratio(
            image,
            x,
            0.728,
        )

        if patch is None:
            return []

        value, score, _ = _classify_value(
            patch
        )

        if value is None:
            return []

        cards.append({
            "index": int(
                index
            ),
            "value": int(
                value
            ),
            "left_x": float(
                x
            ),
            "click_x": float(
                x
                + 12
                * image.shape[
                    1
                ]
                / REFERENCE_W
            ),
            "score": float(
                score
            ),
            "special": bool(
                _is_special_card(
                    patch
                )
            ),
            "_android_digit_y_ratio": 0.728,
        })

        scores.append(
            float(
                score
            )
        )

    if (
        float(
            np.mean(
                scores
            )
        )
        < 0.82
        or min(
            scores
        )
        < 0.68
    ):
        return []

    counts = Counter(
        int(
            card[
                "value"
            ]
        )
        for card in cards
    )

    if any(
        count > 4
        for count in counts.values()
    ):
        return []

    return cards



def read_hand(
    image,
):
    if not DIGIT_SAMPLES:
        return []

    if SCRCPY_REALTIME_ENABLED:
        return _v31_read_hand_calibrated(
            image
        )

    cards = _read_hand_legacy(
        image
    )

    if cards:
        return cards

    return _read_hand_android_adaptive(
        image
    )



# ------------------------------------------------------------------
# V3 persistent online learning
# ------------------------------------------------------------------

def _new_learning_db():
    return {
        "version": 1,
        "matches_learned": 0,
        "rank_counts": {
            "1": 0,
            "2": 0,
            "3": 0,
            "4": 0,
        },
        # exact state/action values
        "horse_q": {},
        "play_q": {},
        # generalized state/action values
        "horse_general_q": {},
        "play_general_q": {},
        "history": [],
    }


def load_learning_db():
    default = _new_learning_db()

    if not LEARNING_PATH.exists():
        return default

    try:
        data = json.loads(
            LEARNING_PATH.read_text(
                encoding="utf-8"
            )
        )

        if not isinstance(
            data,
            dict,
        ):
            return default

        for key, value in default.items():
            if key not in data:
                data[key] = value

        return data

    except Exception as exc:
        log(
            f"学习数据读取失败：{exc}；"
            "本次从空模型开始"
        )
        return default


def save_learning_db():
    try:
        temp = LEARNING_PATH.with_suffix(
            ".tmp"
        )

        temp.write_text(
            json.dumps(
                LEARNING_DB,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        temp.replace(
            LEARNING_PATH
        )

    except Exception as exc:
        log(
            f"学习数据保存失败：{exc}"
        )


LEARNING_DB = load_learning_db()


def _new_match_memory():
    return {
        "started_at": time.strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
        "horse_decisions": [],
        "play_decisions": [],
        "rank_events": [],
        "reward_applied": False,
    }


MATCH_MEMORY = _new_match_memory()


def reset_match_memory():
    global MATCH_MEMORY

    MATCH_MEMORY = (
        _new_match_memory()
    )


def learning_enabled():
    return bool(
        int(
            CFG.get(
                "learning_enabled",
                1,
            )
        )
    )


def _update_q(
    table_name,
    key,
    reward,
):
    table = LEARNING_DB[
        table_name
    ]

    item = table.get(
        key,
        {
            "n": 0,
            "mean": 0.0,
        },
    )

    n = int(
        item.get(
            "n",
            0,
        )
    ) + 1

    old_mean = float(
        item.get(
            "mean",
            0.0,
        )
    )

    mean = (
        old_mean
        + (
            float(reward)
            - old_mean
        ) / n
    )

    table[key] = {
        "n": n,
        "mean": float(
            mean
        ),
    }


def _q_effect(
    table_name,
    key,
):
    item = LEARNING_DB[
        table_name
    ].get(
        key
    )

    if not item:
        return (
            0.0,
            0,
        )

    n = int(
        item.get(
            "n",
            0,
        )
    )

    mean = float(
        item.get(
            "mean",
            0.0,
        )
    )

    confidence_samples = max(
        1,
        int(
            CFG.get(
                "learning_confidence_samples",
                14,
            )
        ),
    )

    # Smooth confidence:
    # 1 sample -> tiny effect
    # many samples -> asymptotically approaches full effect
    confidence = (
        n
        / (
            n
            + confidence_samples
        )
    )

    return (
        mean * confidence,
        n,
    )


def _longest_unique_run(
    values,
):
    unique = sorted(
        set(
            int(v)
            for v in values
        )
    )

    if not unique:
        return 0

    best = 1
    current = 1

    for a, b in zip(
        unique,
        unique[1:],
    ):
        if b == a + 1:
            current += 1
            best = max(
                best,
                current,
            )
        else:
            current = 1

    return best


def _hand_profile(
    values,
):
    values = [
        int(v)
        for v in values
    ]

    counts = Counter(
        values
    )

    pairs = sum(
        1
        for count in counts.values()
        if count >= 2
    )

    triples = sum(
        1
        for count in counts.values()
        if count >= 3
    )

    quads = sum(
        1
        for count in counts.values()
        if count >= 4
    )

    run = (
        _longest_unique_run(
            values
        )
    )

    best_multiplier = (
        _best_pattern_multiplier(
            values
        )
        if values
        else 1.0
    )

    multiplier_bucket = int(
        round(
            best_multiplier * 10
        )
    )

    low = sum(
        1
        for v in values
        if v <= 3
    )
    mid = sum(
        1
        for v in values
        if 4 <= v <= 7
    )
    high = sum(
        1
        for v in values
        if v >= 8
    )

    return (
        f"n{len(values)}"
        f"_p{pairs}"
        f"_t{triples}"
        f"_q{quads}"
        f"_run{run}"
        f"_m{multiplier_bucket}"
        f"_l{low}"
        f"_c{mid}"
        f"_h{high}"
    )


def _availability_mask(
    availability,
):
    return "".join(
        char
        for name, char in (
            ("lower", "L"),
            ("middle", "M"),
            ("upper", "U"),
        )
        if availability.get(
            name,
            {},
        ).get(
            "available",
            False,
        )
    ) or "NONE"


def _horse_learning_keys(
    hand_values,
    availability,
    horse,
    round_index,
):
    sorted_hand = ",".join(
        map(
            str,
            sorted(
                int(v)
                for v in hand_values
            ),
        )
    )

    mask = _availability_mask(
        availability
    )

    exact = (
        f"r{round_index}"
        f"|hand={sorted_hand}"
        f"|avail={mask}"
        f"|a={horse}"
    )

    general = (
        f"r{round_index}"
        f"|{_hand_profile(hand_values)}"
        f"|avail={mask}"
        f"|a={horse}"
    )

    return (
        exact,
        general,
    )


def horse_learning_adjustment(
    hand_values,
    availability,
    horse,
    round_index,
):
    if not learning_enabled():
        return (
            0.0,
            0,
        )

    exact, general = (
        _horse_learning_keys(
            hand_values,
            availability,
            horse,
            round_index,
        )
    )

    exact_value, exact_n = (
        _q_effect(
            "horse_q",
            exact,
        )
    )

    general_value, general_n = (
        _q_effect(
            "horse_general_q",
            general,
        )
    )

    # Generalized evidence dominates early; exact evidence dominates
    # once the same hand context has been seen repeatedly.
    if exact_n > 0:
        blended = (
            exact_value * 0.58
            + general_value * 0.42
        )
    else:
        blended = general_value

    weight = float(
        CFG.get(
            "learning_horse_weight",
            0.72,
        )
    )

    return (
        blended * weight,
        exact_n + general_n,
    )


def record_horse_decision(
    hand_values,
    availability,
    horse,
    round_index,
):
    if not learning_enabled():
        return

    exact, general = (
        _horse_learning_keys(
            hand_values,
            availability,
            horse,
            round_index,
        )
    )

    MATCH_MEMORY[
        "horse_decisions"
    ].append({
        "round": int(
            round_index
        ),
        "horse": horse,
        "hand": [
            int(v)
            for v in hand_values
        ],
        "exact": exact,
        "general": general,
    })


def _play_family(
    play,
):
    name = str(
        play.get(
            "name",
            "",
        )
    )

    if name.startswith(
        "炸弹"
    ):
        return "bomb"

    if name.startswith(
        "连三"
    ):
        return "triple_run"

    if "连对" in name:
        # Preserve 2/3/4 pair-run distinction.
        prefix = name.split(
            "连对",
            1,
        )[0].strip()

        digits = "".join(
            ch
            for ch in prefix
            if ch.isdigit()
        )

        return (
            f"pair_run_{digits}"
            if digits
            else "pair_run"
        )

    if "张顺子" in name:
        prefix = name.split(
            "张顺子",
            1,
        )[0].strip()

        digits = "".join(
            ch
            for ch in prefix
            if ch.isdigit()
        )

        return (
            f"straight_{digits}"
            if digits
            else "straight"
        )

    if name.startswith(
        "三张"
    ):
        return "triple"

    if name.startswith(
        "对子"
    ):
        return "pair"

    if name.startswith(
        "释放补牌位"
    ):
        count = len(
            play.get(
                "indices",
                [],
            )
        )
        return (
            f"discard_{count}"
        )

    return "other"


def _time_bucket(
    elapsed,
):
    elapsed = float(
        elapsed
    )

    if elapsed < 25:
        return "early"

    if elapsed < 48:
        return "mid"

    if elapsed < 57:
        return "late"

    return "emergency"


def _play_learning_keys(
    cards,
    hand_limit,
    elapsed,
    round_index,
    horse,
    play,
):
    values = [
        int(
            card["value"]
        )
        for card in cards
    ]

    action = (
        _play_family(
            play
        )
    )

    exact_hand = ",".join(
        map(
            str,
            sorted(
                values
            ),
        )
    )

    stage = (
        refill_stage()
    )

    exact = (
        f"r{round_index}"
        f"|h={horse or 'unknown'}"
        f"|hand={exact_hand}"
        f"|limit={hand_limit}"
        f"|refill={stage}"
        f"|time={_time_bucket(elapsed)}"
        f"|a={action}"
    )

    general = (
        f"r{round_index}"
        f"|h={horse or 'unknown'}"
        f"|{_hand_profile(values)}"
        f"|limit={hand_limit}"
        f"|refill={stage}"
        f"|time={_time_bucket(elapsed)}"
        f"|a={action}"
    )

    return (
        exact,
        general,
        action,
    )


def play_learning_adjustment(
    cards,
    hand_limit,
    elapsed,
    round_index,
    horse,
    play,
):
    if not learning_enabled():
        return (
            0.0,
            0,
        )

    exact, general, _ = (
        _play_learning_keys(
            cards,
            hand_limit,
            elapsed,
            round_index,
            horse,
            play,
        )
    )

    exact_value, exact_n = (
        _q_effect(
            "play_q",
            exact,
        )
    )

    general_value, general_n = (
        _q_effect(
            "play_general_q",
            general,
        )
    )

    if exact_n > 0:
        blended = (
            exact_value * 0.52
            + general_value * 0.48
        )
    else:
        blended = (
            general_value
        )

    weight = float(
        CFG.get(
            "learning_play_weight",
            460.0,
        )
    )

    return (
        blended * weight,
        exact_n + general_n,
    )


def attach_play_learning(
    cards,
    hand_limit,
    elapsed,
    round_index,
    horse,
    play,
):
    exact, general, action = (
        _play_learning_keys(
            cards,
            hand_limit,
            elapsed,
            round_index,
            horse,
            play,
        )
    )

    play = dict(
        play
    )

    play[
        "_learn_exact"
    ] = exact

    play[
        "_learn_general"
    ] = general

    play[
        "_learn_action"
    ] = action

    return play


def record_play_decision(
    play,
    round_index,
):
    if not learning_enabled():
        return

    exact = play.get(
        "_learn_exact"
    )

    general = play.get(
        "_learn_general"
    )

    if not exact or not general:
        return

    MATCH_MEMORY[
        "play_decisions"
    ].append({
        "round": int(
            round_index
        ),
        "name": str(
            play.get(
                "name",
                ""
            )
        ),
        "values": [
            int(v)
            for v in play.get(
                "values",
                []
            )
        ],
        "action": str(
            play.get(
                "_learn_action",
                "other",
            )
        ),
        "exact": exact,
        "general": general,
    })


def detect_user_rank(
    image,
):
    """
    结果页按最终名次排序。
    用账号头像在左侧4行中定位用户所在行，即得到1~4名。

    如果你以后换头像，把新的头像裁图放在脚本目录：
        horse_race_user_avatar.png
    即可覆盖内置模板。
    """
    H, W = image.shape[:2]

    match = template_match(
        image,
        USER_AVATAR_TEMPLATE,
        threshold=float(
            CFG.get(
                "learning_result_avatar_threshold",
                0.72,
            )
        ),
        roi=(
            W * 0.04,
            H * 0.34,
            W * 0.30,
            H * 0.83,
        ),
    )

    if match is None:
        return (
            None,
            0.0,
        )

    cy = float(
        match["center"][1]
    )

    row_centers = [
        H * 0.432,
        H * 0.545,
        H * 0.658,
        H * 0.771,
    ]

    rank = (
        min(
            range(4),
            key=lambda i: abs(
                cy
                - row_centers[i]
            ),
        )
        + 1
    )

    # Reject impossible vertical mismatch.
    if (
        abs(
            cy
            - row_centers[
                rank - 1
            ]
        )
        > H * 0.060
    ):
        return (
            None,
            float(
                match["score"]
            ),
        )

    return (
        rank,
        float(
            match["score"]
        ),
    )


def apply_match_learning(
    image,
):
    """
    A completed 3-round match gives one terminal reward.
    That reward is propagated to all horse choices and card-play
    decisions made in this match.

    This is deliberately conservative:
    - rank cannot be identified -> do not learn
    - early samples have low influence
    - learned score is bounded and layered on top of legal/heuristic rules
    """
    if not learning_enabled():
        return False

    if MATCH_MEMORY.get(
        "reward_applied",
        False,
    ):
        return True

    rank, avatar_score = (
        detect_user_rank(
            image
        )
    )

    if rank is None:
        log(
            "AI学习：结果页已出现，但无法可靠定位你的头像；"
            f"score={avatar_score:.3f}，本场不写入学习数据"
        )

        save_debug(
            image,
            "learning_rank_unknown",
        )

        return False

    rewards = dict(
        CFG.get(
            "learning_rank_rewards",
            {},
        )
    )

    reward = float(
        rewards.get(
            str(rank),
            {
                1: 1.00,
                2: 0.35,
                3: -0.25,
                4: -0.75,
            }[rank],
        )
    )

    for decision in MATCH_MEMORY[
        "horse_decisions"
    ]:
        _update_q(
            "horse_q",
            decision["exact"],
            reward,
        )

        _update_q(
            "horse_general_q",
            decision["general"],
            reward,
        )

    for decision in MATCH_MEMORY[
        "play_decisions"
    ]:
        play_reward = (
            reward
            + float(
                decision.get(
                    "rank_shaping",
                    0.0,
                )
            )
        )

        _update_q(
            "play_q",
            decision["exact"],
            play_reward,
        )

        _update_q(
            "play_general_q",
            decision["general"],
            play_reward,
        )

    LEARNING_DB[
        "matches_learned"
    ] = (
        int(
            LEARNING_DB.get(
                "matches_learned",
                0,
            )
        )
        + 1
    )

    rank_counts = LEARNING_DB.setdefault(
        "rank_counts",
        {
            "1": 0,
            "2": 0,
            "3": 0,
            "4": 0,
        },
    )

    rank_counts[
        str(rank)
    ] = (
        int(
            rank_counts.get(
                str(rank),
                0,
            )
        )
        + 1
    )

    history = LEARNING_DB.setdefault(
        "history",
        [],
    )

    history.append({
        "time": time.strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
        "rank": int(rank),
        "reward": reward,
        "avatar_score": avatar_score,
        "horse_choices": [
            {
                "round": d["round"],
                "horse": d["horse"],
                "hand": d["hand"],
            }
            for d in MATCH_MEMORY[
                "horse_decisions"
            ]
        ],
        "plays": [
            {
                "round": d["round"],
                "name": d["name"],
                "values": d["values"],
                "rank_shaping": d.get(
                    "rank_shaping",
                    0.0,
                ),
            }
            for d in MATCH_MEMORY[
                "play_decisions"
            ]
        ],
    })

    history_limit = max(
        20,
        int(
            CFG.get(
                "learning_history_limit",
                300,
            )
        ),
    )

    if len(history) > history_limit:
        del history[
            :-history_limit
        ]

    MATCH_MEMORY[
        "reward_applied"
    ] = True

    save_learning_db()

    total = int(
        LEARNING_DB[
            "matches_learned"
        ]
    )

    log(
        f"AI学习：识别你本场第{rank}名 "
        f"(头像score={avatar_score:.3f})，"
        f"reward={reward:+.2f}"
    )

    log(
        f"AI学习：本场写入 "
        f"{len(MATCH_MEMORY['horse_decisions'])} 次选马决策 + "
        f"{len(MATCH_MEMORY['play_decisions'])} 次出牌决策；"
        f"累计学习 {total} 场"
    )

    log(
        "AI学习排名统计："
        + " ".join(
            f"{i}名={rank_counts.get(str(i),0)}"
            for i in range(
                1,
                5,
            )
        )
    )

    return True



# ------------------------------------------------------------------
# V5 shared 40-card deck / refill checkpoint state
# ------------------------------------------------------------------

FULL_DECK_COUNTS = {
    value: 4
    for value in range(1, 11)
}


def _new_deck_state():
    return {
        "remaining": dict(
            FULL_DECK_COUNTS
        ),
        "hand": {},
        "played": {
            value: 0
            for value in range(1, 11)
        },
        "dealt": {
            value: 0
            for value in range(1, 11)
        },
        "initialized": False,

        # number of the three track refill checkpoints already observed
        "refill_count": 0,

        # start fill after horse selection is NOT one of the three checkpoints
        "race_started_at": None,
        "last_refill_at": 0.0,

        # stable-hand guard against one-frame recognition noise
        "candidate_signature": None,
        "candidate_frames": 0,
    }


DECK_STATE = _new_deck_state()


def reset_shared_deck():
    global DECK_STATE

    DECK_STATE = (
        _new_deck_state()
    )

    log(
        "牌库重置：三局共用40张，"
        "1~10每个数字各4张"
    )


def _counter_from_values(
    values,
):
    counter = Counter()

    for value in values:
        value = int(
            value
        )

        if 1 <= value <= 10:
            counter[value] += 1

    return counter


def _dict_counter(
    data,
):
    return Counter({
        int(k): int(v)
        for k, v in data.items()
        if int(v) > 0
    })


def _remaining_deck_list():
    cards = []

    for value in range(
        1,
        11,
    ):
        count = max(
            0,
            int(
                DECK_STATE[
                    "remaining"
                ].get(
                    value,
                    0,
                )
            ),
        )

        cards.extend(
            [value] * count
        )

    return cards


def _deck_remaining_text():
    return " ".join(
        f"{value}:{int(DECK_STATE['remaining'].get(value,0))}"
        for value in range(
            1,
            11,
        )
    )


def _log_deck_state(
    prefix="牌库",
):
    if not int(
        CFG.get(
            "deck_state_log",
            1,
        )
    ):
        return

    hand = _dict_counter(
        DECK_STATE[
            "hand"
        ]
    )

    hand_text = " ".join(
        f"{value}x{count}"
        for value, count in sorted(
            hand.items()
        )
    ) or "空"

    log(
        f"{prefix}："
        f"当前手牌[{hand_text}]；"
        f"剩余未发={sum(DECK_STATE['remaining'].values())}；"
        f"补牌点={DECK_STATE['refill_count']}/"
        f"{int(CFG.get('refill_checkpoint_count',3))}"
    )

    log(
        "剩余牌分布："
        + _deck_remaining_text()
    )


def _subtract_newly_dealt(
    newly_dealt,
):
    """
    Cards are removed from the unseen draw pool when they ENTER the hand,
    not when they are later played.
    """
    for value, count in newly_dealt.items():
        if count <= 0:
            continue

        available = int(
            DECK_STATE[
                "remaining"
            ].get(
                value,
                0,
            )
        )

        actual = min(
            available,
            int(count),
        )

        DECK_STATE[
            "remaining"
        ][value] = (
            available
            - actual
        )

        DECK_STATE[
            "dealt"
        ][value] = (
            int(
                DECK_STATE[
                    "dealt"
                ].get(
                    value,
                    0,
                )
            )
            + actual
        )


def initialize_deck_from_hand(
    values,
):
    """
    First reliable visible hand of a match.
    Those cards have already left the 40-card draw pool.
    """
    if not int(
        CFG.get(
            "shared_deck_enabled",
            1,
        )
    ):
        return

    observed = (
        _counter_from_values(
            values
        )
    )

    # Reject impossible recognition.
    if any(
        count > 4
        for count in observed.values()
    ):
        return

    DECK_STATE[
        "remaining"
    ] = dict(
        FULL_DECK_COUNTS
    )

    DECK_STATE[
        "dealt"
    ] = {
        value: 0
        for value in range(
            1,
            11,
        )
    }

    _subtract_newly_dealt(
        observed
    )

    DECK_STATE[
        "hand"
    ] = dict(
        observed
    )

    DECK_STATE[
        "initialized"
    ] = True

    _log_deck_state(
        "牌库初始化"
    )


def mark_race_started():
    if (
        DECK_STATE[
            "race_started_at"
        ]
        is None
    ):
        DECK_STATE[
            "race_started_at"
        ] = time.monotonic()


def finish_shared_deck_round():
    """Keep dealt-card history while clearing this round's hand and refill clock."""
    if not DECK_STATE.get("initialized", False):
        return
    old_hand = dict(DECK_STATE.get("hand", {}))
    DECK_STATE["hand"] = {}
    DECK_STATE["refill_count"] = 0
    DECK_STATE["race_started_at"] = None
    DECK_STATE["last_refill_at"] = 0.0
    DECK_STATE["candidate_signature"] = None
    DECK_STATE["candidate_frames"] = 0
    event_log("round_deck_boundary", discarded_hand=old_hand,
              remaining=dict(DECK_STATE["remaining"]))


def deck_mark_successful_play(
    values,
):
    """
    Successful play removes cards from the hand but NOT from remaining deck:
    they were already removed from the unseen deck when dealt.
    """
    if not DECK_STATE[
        "initialized"
    ]:
        return

    hand = _dict_counter(
        DECK_STATE[
            "hand"
        ]
    )

    selected = (
        _counter_from_values(
            values
        )
    )

    for value, count in selected.items():
        actual = min(
            count,
            hand.get(
                value,
                0,
            ),
        )

        hand[value] -= actual

        if hand[value] <= 0:
            hand.pop(
                value,
                None,
            )

        DECK_STATE[
            "played"
        ][value] = (
            int(
                DECK_STATE[
                    "played"
                ].get(
                    value,
                    0,
                )
            )
            + actual
        )

    DECK_STATE[
        "hand"
    ] = dict(
        hand
    )

    # Force the next visible hand through a fresh stability check.
    DECK_STATE[
        "candidate_signature"
    ] = None

    DECK_STATE[
        "candidate_frames"
    ] = 0


def _apply_stable_observed_hand(
    observed,
):
    """
    Compare a stable observed hand against our tracked post-play hand.

    Positive differences = new cards were dealt from the shared deck.
    Negative differences are allowed because a successful play may have
    happened immediately before this observation.
    """
    tracked = _dict_counter(
        DECK_STATE[
            "hand"
        ]
    )

    positive = Counter()

    for value in range(
        1,
        11,
    ):
        diff = (
            observed.get(
                value,
                0,
            )
            - tracked.get(
                value,
                0,
            )
        )

        if diff > 0:
            positive[value] = diff

    new_cards = sum(
        positive.values()
    )

    if new_cards > 0:
        # A visible increase in hand cards means newly dealt cards.
        _subtract_newly_dealt(
            positive
        )

        now = time.monotonic()

        race_started_at = (
            DECK_STATE.get(
                "race_started_at"
            )
        )

        elapsed = (
            now
            - race_started_at
            if race_started_at
            is not None
            else 0.0
        )

        grace = float(
            CFG.get(
                "initial_fill_grace_seconds",
                5.5,
            )
        )

        max_refill = int(
            CFG.get(
                "refill_max_cards",
                3,
            )
        )

        gap = float(
            CFG.get(
                "refill_event_min_gap_seconds",
                4.0,
            )
        )

        since_last = (
            now
            - float(
                DECK_STATE.get(
                    "last_refill_at",
                    0.0,
                )
            )
        )

        # Initial fill immediately after choosing a horse does not count
        # as one of the 3 track refill checkpoints.
        if (
            race_started_at
            is not None
            and elapsed > grace
            and 1
            <= new_cards
            <= max_refill
            and since_last >= gap
            and DECK_STATE[
                "refill_count"
            ]
            < int(
                CFG.get(
                    "refill_checkpoint_count",
                    3,
                )
            )
        ):
            DECK_STATE[
                "refill_count"
            ] += 1

            DECK_STATE[
                "last_refill_at"
            ] = now

            log(
                f"检测到赛道补牌点："
                f"本次补{new_cards}张，"
                f"已经过 {DECK_STATE['refill_count']}/"
                f"{int(CFG.get('refill_checkpoint_count',3))}"
            )

        else:
            log(
                f"检测到新发牌：{new_cards}张"
                + (
                    "（开赛初始补满，不计赛道补牌点）"
                    if elapsed <= grace
                    else ""
                )
            )

    DECK_STATE[
        "hand"
    ] = dict(
        observed
    )

    _log_deck_state(
        "牌库更新"
    )


def observe_shared_hand(
    values,
):
    """
    Two-frame stabilization prevents one bad visual recognition from
    corrupting the 40-card memory.
    """
    if not int(
        CFG.get(
            "shared_deck_enabled",
            1,
        )
    ):
        return

    values = [
        int(v)
        for v in values
    ]

    observed = (
        _counter_from_values(
            values
        )
    )

    if any(
        count > 4
        for count in observed.values()
    ):
        return

    if not DECK_STATE[
        "initialized"
    ]:
        initialize_deck_from_hand(
            values
        )
        return

    signature = tuple(
        sorted(
            observed.items()
        )
    )

    if (
        signature
        == DECK_STATE[
            "candidate_signature"
        ]
    ):
        DECK_STATE[
            "candidate_frames"
        ] += 1

    else:
        DECK_STATE[
            "candidate_signature"
        ] = signature

        DECK_STATE[
            "candidate_frames"
        ] = 1

    if (
        DECK_STATE[
            "candidate_frames"
        ]
        < 2
    ):
        return

    tracked_signature = tuple(
        sorted(
            _dict_counter(
                DECK_STATE[
                    "hand"
                ]
            ).items()
        )
    )

    if (
        signature
        == tracked_signature
    ):
        return

    _apply_stable_observed_hand(
        observed
    )


def refill_stage():
    return int(
        DECK_STATE.get(
            "refill_count",
            0,
        )
    )


def future_refills_left():
    return max(
        0,
        int(
            CFG.get(
                "refill_checkpoint_count",
                3,
            )
        )
        - refill_stage(),
    )


# ------------------------------------------------------------------
# V7 exact shared-deck probability engine
# ------------------------------------------------------------------

def _counts_tuple(
    values,
):
    counter = Counter(
        int(v)
        for v in values
    )

    return tuple(
        int(
            counter.get(
                value,
                0,
            )
        )
        for value in range(
            1,
            11,
        )
    )


def _remaining_tuple():
    return tuple(
        int(
            DECK_STATE[
                "remaining"
            ].get(
                value,
                0,
            )
        )
        for value in range(
            1,
            11,
        )
    )


def _longest_straight_from_counts(
    counts,
):
    present = [
        i + 1
        for i, count in enumerate(
            counts
        )
        if count >= 1
    ]

    if not present:
        return 0

    best = 1
    current = 1

    for left, right in zip(
        present,
        present[1:],
    ):
        if right == left + 1:
            current += 1
            best = max(
                best,
                current,
            )
        else:
            current = 1

    return min(
        best,
        8,
    )


def _longest_pair_run_from_counts(
    counts,
):
    present = [
        i + 1
        for i, count in enumerate(
            counts
        )
        if count >= 2
    ]

    if not present:
        return 0

    best = 1
    current = 1

    for left, right in zip(
        present,
        present[1:],
    ):
        if right == left + 1:
            current += 1
            best = max(
                best,
                current,
            )
        else:
            current = 1

    return min(
        best,
        4,
    )


def _has_triple_run_counts(
    counts,
):
    return any(
        counts[value - 1] >= 3
        and counts[value] >= 3
        for value in range(
            1,
            10,
        )
    )


def _best_multiplier_from_counts(
    counts,
):
    # Bomb
    if any(
        count >= 4
        for count in counts
    ):
        best = 6.00
    else:
        best = 1.00

    # Triple run 666777-style
    if _has_triple_run_counts(
        counts
    ):
        best = max(
            best,
            5.00,
        )

    pair_run = (
        _longest_pair_run_from_counts(
            counts
        )
    )

    if pair_run >= 4:
        best = max(
            best,
            5.00,
        )
    elif pair_run >= 3:
        best = max(
            best,
            4.20,
        )
    elif pair_run >= 2:
        best = max(
            best,
            2.50,
        )

    straight = (
        _longest_straight_from_counts(
            counts
        )
    )

    if straight >= 8:
        best = max(best, 4.20)
    elif straight >= 7:
        best = max(best, 3.90)
    elif straight >= 6:
        best = max(best, 3.50)
    elif straight >= 5:
        best = max(best, 2.40)
    elif straight >= 4:
        best = max(best, 1.40)

    if any(
        count >= 3
        for count in counts
    ):
        best = max(
            best,
            3.00,
        )

    if any(
        count >= 2
        for count in counts
    ):
        best = max(
            best,
            1.50,
        )

    return float(
        best
    )


@lru_cache(maxsize=4096)
def _draw_outcomes_cached(
    remaining,
    draw_count,
):
    """
    Exact multivariate-hypergeometric outcomes over values 1..10.

    Returns:
        tuple((draw_counts_tuple, probability), ...)
    """
    remaining = tuple(
        int(v)
        for v in remaining
    )

    total = sum(
        remaining
    )

    k = min(
        int(draw_count),
        total,
    )

    if k <= 0:
        return (
            (
                (0,) * 10,
                1.0,
            ),
        )

    denominator = math.comb(
        total,
        k,
    )

    outcomes = []

    chosen = [
        0
    ] * 10

    def rec(
        index,
        left,
        weight,
    ):
        if index == 10:
            if left == 0:
                outcomes.append(
                    (
                        tuple(
                            chosen
                        ),
                        float(
                            weight
                            / denominator
                        ),
                    )
                )
            return

        if left < 0:
            return

        max_take = min(
            remaining[index],
            left,
        )

        for take in range(
            max_take + 1
        ):
            chosen[index] = take

            rec(
                index + 1,
                left - take,
                weight
                * math.comb(
                    remaining[index],
                    take,
                ),
            )

        chosen[index] = 0

    rec(
        0,
        k,
        1,
    )

    return tuple(
        outcomes
    )


@lru_cache(maxsize=8192)
def exact_future_pattern_probabilities(
    hand_counts,
    remaining,
    draw_count,
):
    hand_counts = tuple(
        int(v)
        for v in hand_counts
    )

    remaining = tuple(
        int(v)
        for v in remaining
    )

    k = min(
        int(draw_count),
        sum(
            remaining
        ),
    )

    result = {
        "draw_count": int(k),
        "bomb": 0.0,
        "triple_run": 0.0,
        "pair_run_4": 0.0,
        "pair_run_3": 0.0,
        "pair_run_2": 0.0,
        "straight_8": 0.0,
        "straight_7": 0.0,
        "straight_6": 0.0,
        "straight_5": 0.0,
        "straight_4": 0.0,
        "triple": 0.0,
        "pair": 0.0,
        "ge_3x": 0.0,
        "ge_4_2x": 0.0,
        "expected_best_multiplier": 0.0,
    }

    for draw_counts, probability in (
        _draw_outcomes_cached(
            remaining,
            k,
        )
    ):
        final_counts = tuple(
            hand_counts[i]
            + draw_counts[i]
            for i in range(
                10
            )
        )

        bomb = any(
            count >= 4
            for count in final_counts
        )

        triple_run = (
            _has_triple_run_counts(
                final_counts
            )
        )

        pair_run = (
            _longest_pair_run_from_counts(
                final_counts
            )
        )

        straight = (
            _longest_straight_from_counts(
                final_counts
            )
        )

        triple = any(
            count >= 3
            for count in final_counts
        )

        pair = any(
            count >= 2
            for count in final_counts
        )

        best = (
            _best_multiplier_from_counts(
                final_counts
            )
        )

        if bomb:
            result["bomb"] += probability

        if triple_run:
            result[
                "triple_run"
            ] += probability

        if pair_run >= 4:
            result[
                "pair_run_4"
            ] += probability

        if pair_run >= 3:
            result[
                "pair_run_3"
            ] += probability

        if pair_run >= 2:
            result[
                "pair_run_2"
            ] += probability

        if straight >= 8:
            result[
                "straight_8"
            ] += probability

        if straight >= 7:
            result[
                "straight_7"
            ] += probability

        if straight >= 6:
            result[
                "straight_6"
            ] += probability

        if straight >= 5:
            result[
                "straight_5"
            ] += probability

        if straight >= 4:
            result[
                "straight_4"
            ] += probability

        if triple:
            result[
                "triple"
            ] += probability

        if pair:
            result[
                "pair"
            ] += probability

        if best >= 3.0:
            result[
                "ge_3x"
            ] += probability

        if best >= 4.2:
            result[
                "ge_4_2x"
            ] += probability

        result[
            "expected_best_multiplier"
        ] += (
            probability
            * best
        )

    return result


def _remove_play_from_values(
    hand_values,
    play,
):
    counter = Counter(
        int(v)
        for v in hand_values
    )

    requirements = play.get(
        "requirements"
    )

    if not requirements:
        requirements = dict(
            Counter(
                int(v)
                for v in play.get(
                    "values",
                    []
                )
            )
        )

    for raw_value, raw_count in (
        requirements.items()
    ):
        value = int(
            raw_value
        )

        count = int(
            raw_count
        )

        counter[value] -= count

        if counter[value] <= 0:
            counter.pop(
                value,
                None,
            )

    result = []

    for value in range(
        1,
        11,
    ):
        result.extend(
            [value]
            * max(
                0,
                counter.get(
                    value,
                    0,
                ),
            )
        )

    return result


def future_after_play_probabilities(
    cards,
    play,
    hand_limit,
):
    hand_values = [
        int(
            card["value"]
        )
        for card in cards
    ]

    after_values = (
        _remove_play_from_values(
            hand_values,
            play,
        )
    )

    remaining = (
        _remaining_tuple()
    )

    if (
        future_refills_left()
        <= 0
    ):
        draw_count = 0
    else:
        draw_count = min(
            int(
                CFG.get(
                    "refill_max_cards",
                    3,
                )
            ),
            max(
                0,
                int(
                    hand_limit
                )
                - len(
                    after_values
                ),
            ),
            sum(
                remaining
            ),
        )

    return (
        exact_future_pattern_probabilities(
            _counts_tuple(
                after_values
            ),
            remaining,
            draw_count,
        )
    )


def probability_strategy_bonus(
    probabilities,
):
    """
    Secondary strategic value.
    Immediate acceleration remains the dominant score.
    """
    weight = float(
        CFG.get(
            "future_probability_weight",
            1.0,
        )
    )

    return weight * (
        probabilities[
            "expected_best_multiplier"
        ] * 85.0
        + probabilities[
            "bomb"
        ] * 520.0
        + probabilities[
            "triple_run"
        ] * 360.0
        + probabilities[
            "pair_run_4"
        ] * 260.0
        + probabilities[
            "straight_8"
        ] * 240.0
        + probabilities[
            "ge_4_2x"
        ] * 160.0
    )


def probability_summary_text(
    probabilities,
):
    return (
        f"补{probabilities['draw_count']}张："
        f"炸弹={probabilities['bomb']:.1%}，"
        f"连三={probabilities['triple_run']:.1%}，"
        f"4连对={probabilities['pair_run_4']:.1%}，"
        f"8顺={probabilities['straight_8']:.1%}，"
        f">=3x={probabilities['ge_3x']:.1%}，"
        f">=4.2x={probabilities['ge_4_2x']:.1%}，"
        f"期望最佳={probabilities['expected_best_multiplier']:.2f}x"
    )


def current_bomb_probabilities(
    hand_values,
    max_draw=3,
):
    """
    Per-number bomb chance from the remembered unseen deck.
    """
    hand = Counter(
        int(v)
        for v in hand_values
    )

    remaining = {
        value: int(
            DECK_STATE[
                "remaining"
            ].get(
                value,
                0,
            )
        )
        for value in range(
            1,
            11,
        )
    }

    total = sum(
        remaining.values()
    )

    k = min(
        int(max_draw),
        total,
    )

    result = {}

    if k <= 0:
        return result

    denominator = math.comb(
        total,
        k,
    )

    for value in range(
        1,
        11,
    ):
        need = max(
            0,
            4
            - hand.get(
                value,
                0,
            )
        )

        available = remaining[
            value
        ]

        if need <= 0:
            result[value] = 1.0
            continue

        if (
            need > k
            or need > available
        ):
            result[value] = 0.0
            continue

        good = 0

        for take in range(
            need,
            min(
                k,
                available,
            )
            + 1
        ):
            rest = (
                k
                - take
            )

            other = (
                total
                - available
            )

            if (
                0
                <= rest
                <= other
            ):
                good += (
                    math.comb(
                        available,
                        take,
                    )
                    * math.comb(
                        other,
                        rest,
                    )
                )

        result[value] = (
            good
            / denominator
        )

    return result

# ------------------------------------------------------------------
# Pattern solver
# ------------------------------------------------------------------

# Screenshot rules are "额外加速收益", so final speed multiplier is:
# base 100% + extra percentage.
STRAIGHT_MULTIPLIER = {
    4: 1.40,  # extra +40%
    5: 2.40,  # extra +140%
    6: 3.50,  # extra +250%
    7: 3.90,  # extra +290%
    8: 4.20,  # extra +320%
}

PAIR_RUN_MULTIPLIER = {
    2: 2.50,  # +150%
    3: 4.20,  # +320%, confirmed in user's video
    4: 5.00,  # +400%, confirmed by the in-game rules panel
}

def _pattern_score(multiplier, values, size_bonus=0.0):
    # Main objective: acceleration multiplier.
    # Tie-break: more/higher cards contribute more base acceleration.
    return (
        multiplier * 1000.0
        + sum(values) * 3.0
        + len(values) * 10.0
        + size_bonus
    )

def generate_patterns(cards):
    by_value = defaultdict(list)

    for card in cards:
        by_value[card["value"]].append(card["index"])

    patterns = []

    def add(name, indices, multiplier, priority_bonus=0.0):
        values = [
            cards[i]["value"]
            for i in indices
        ]

        # V6: the semantic rule is represented by exact value counts.
        # Physical card indices are resolved immediately before clicking.
        requirements = dict(
            Counter(
                int(v)
                for v in values
            )
        )

        patterns.append({
            "name": name,
            "indices": list(indices),
            "values": values,
            "requirements": requirements,
            "multiplier": float(multiplier),
            "score": _pattern_score(
                multiplier,
                values,
                priority_bonus,
            ),
        })

    # Bomb: four equal cards.
    for value in range(1, 11):
        if len(by_value[value]) >= 4:
            add(
                f"炸弹 {value}×4",
                by_value[value][:4],
                6.00,
                120,
            )

    # 连三: two adjacent triples; the rules panel shows extra +400%.
    for start in range(1, 10):
        if (
            len(by_value[start]) >= 3
            and len(by_value[start + 1]) >= 3
        ):
            add(
                f"连三 {start}{start}{start}-{start+1}{start+1}{start+1}",
                by_value[start][:3]
                + by_value[start + 1][:3],
                5.00,
                110,
            )

    # Four / three / two consecutive pairs.
    for pair_len in (4, 3, 2):
        multiplier = PAIR_RUN_MULTIPLIER[pair_len]

        for start in range(
            1,
            11 - pair_len + 1,
        ):
            values = list(
                range(
                    start,
                    start + pair_len,
                )
            )

            if all(
                len(by_value[v]) >= 2
                for v in values
            ):
                indices = []
                for value in values:
                    indices.extend(
                        by_value[value][:2]
                    )

                add(
                    f"{pair_len}连对 "
                    + "-".join(map(str, values)),
                    indices,
                    multiplier,
                    80 + pair_len * 5,
                )

    # Long straight first.
    for length in (8, 7, 6, 5, 4):
        multiplier = STRAIGHT_MULTIPLIER[length]

        for start in range(
            1,
            11 - length + 1,
        ):
            values = list(
                range(
                    start,
                    start + length,
                )
            )

            if all(
                len(by_value[v]) >= 1
                for v in values
            ):
                indices = [
                    max(
                        by_value[v],
                        key=lambda idx: float(
                            cards[idx].get(
                                "score",
                                0.0,
                            )
                        ),
                    )
                    for v in values
                ]

                add(
                    f"{length}张顺子 "
                    + "-".join(map(str, values)),
                    indices,
                    multiplier,
                    length * 7,
                )

    # Triple.
    for value in range(1, 11):
        if len(by_value[value]) >= 3:
            add(
                f"三张 {value}×3",
                by_value[value][:3],
                3.00,
                45,
            )

    # Pair.
    for value in range(1, 11):
        if len(by_value[value]) >= 2:
            add(
                f"对子 {value}×2",
                by_value[value][:2],
                1.50,
                20,
            )

    # Single cards are ALWAYS legal according to the confirmed rules.
    # Keep them at the bottom of the heuristic list unless the strategy
    # needs to free slots for an upcoming refill checkpoint.
    for value in range(1, 11):
        for index in by_value[value]:
            add(
                f"单张 {value}",
                [index],
                1.00,
                -80,
            )

    patterns.sort(
        key=lambda item: -item["score"]
    )

    return patterns

def play_context_signature(
    cards,
    play,
):
    """
    A context-specific invalid signature.
    If the game does not show the 出牌 button, that exact selection in
    that exact hand is considered invalid for this round.
    """
    hand = tuple(
        sorted(
            int(card["value"])
            for card in cards
        )
    )

    selected = tuple(
        sorted(
            int(value)
            for value in play.get(
                "values",
                []
            )
        )
    )

    family = _play_family(
        play
    )

    return (
        hand,
        selected,
        family,
    )


ROUND_INVALID_PLAYS = set()


def reset_round_invalid_plays():
    ROUND_INVALID_PLAYS.clear()


def choose_single_release(
    cards,
):
    """
    V4 rule correction:

    The old bot tried to free draw slots by selecting several unrelated
    singles at once. That can produce illegal selections such as 3,5,6.

    Now, if no listed combo is available, it releases EXACTLY ONE card.
    One click = one single play attempt. It never bundles disconnected
    singles into a fake combo.
    """
    counts = Counter(
        card["value"]
        for card in cards
    )

    candidates = []

    for card in cards:
        value = int(
            card["value"]
        )

        duplicate_bonus = (
            (counts[value] - 1) * 8
        )

        neighbor_bonus = 0

        for delta in (
            -2,
            -1,
            1,
            2,
        ):
            if counts.get(
                value + delta,
                0,
            ):
                neighbor_bonus += (
                    4
                    if abs(delta) == 1
                    else 1
                )

        special_bonus = (
            7
            if card.get(
                "special",
                False,
            )
            else 0
        )

        center_bonus = (
            2
            if 3 <= value <= 8
            else 0
        )

        keep_score = (
            duplicate_bonus
            + neighbor_bonus
            + special_bonus
            + center_bonus
            + value * 0.05
        )

        play = {
            "name": f"单张 {value}",
            "indices": [
                card["index"]
            ],
            "values": [
                value
            ],
            "multiplier": 1.0,
            "score": 5.0 - keep_score,
        }

        signature = (
            play_context_signature(
                cards,
                play,
            )
        )

        if (
            signature
            in ROUND_INVALID_PLAYS
        ):
            continue

        candidates.append(
            (
                keep_score,
                play,
            )
        )

    if not candidates:
        return None

    candidates.sort(
        key=lambda item: item[0]
    )

    return candidates[0][1]


def choose_play(
    cards,
    hand_limit,
    race_elapsed,
    round_index=1,
    horse=None,
    realtime_rank=None,
):
    patterns = generate_patterns(
        cards
    )

    # If the game itself previously refused this exact selection
    # (no 出牌 button), never repeat it for the same visible hand.
    patterns = [
        pattern
        for pattern in patterns
        if play_context_signature(
            cards,
            pattern,
        )
        not in ROUND_INVALID_PLAYS
    ]

    if not patterns:
        return None

    current_refill_stage = (
        refill_stage()
    )

    refills_left = (
        future_refills_left()
    )

    reserve_slots = int(
        CFG.get(
            "reserve_slots_before_refill",
            3,
        )
    )

    # Before all 3 refill checkpoints have passed, try to keep enough
    # room to accept the checkpoint's maximum 3-card refill.
    if refills_left > 0:
        target_hand_count = max(
            0,
            hand_limit
            - reserve_slots
        )
    else:
        target_hand_count = 0

    must_release = (
        int(
            CFG.get(
                "always_release_for_refill",
                1,
            )
        )
        and len(cards)
        > target_hand_count
    )

    # Once the last refill point has passed, there is no reason to hoard.
    if (
        refills_left <= 0
        and int(
            CFG.get(
                "empty_hand_after_last_refill",
                1,
            )
        )
        and len(cards) > 0
    ):
        must_release = True

    aggressive_after = float(
        CFG.get(
            "aggressive_after_seconds",
            8.0,
        )
    )

    if realtime_rank in (
        1,
        2,
        3,
        4,
    ):
        aggressive_after = float(
            CFG.get(
                f"rank_{realtime_rank}_aggressive_after",
                aggressive_after,
            )
        )

    aggressive = (
        race_elapsed
        >= aggressive_after
    )

    emergency = (
        race_elapsed
        >= float(
            CFG.get(
                "emergency_after_seconds",
                14.0,
            )
        )
    )

    learned_patterns = []

    for pattern in patterns:
        adjustment, samples = (
            play_learning_adjustment(
                cards,
                hand_limit,
                race_elapsed,
                round_index,
                horse,
                pattern,
            )
        )

        item = dict(
            pattern
        )

        item[
            "learn_adjust"
        ] = float(
            adjustment
        )

        item[
            "learn_samples"
        ] = int(
            samples
        )

        item[
            "ai_score"
        ] = (
            float(
                item["score"]
            )
            + adjustment
        )

        # V8:
        # 这里先不做精确概率计算。
        # 先完成便宜的基础评分/学习评分/腾牌评分，
        # 最后只给Top-N候选做精确多元超几何计算。

        # V5 refill-aware bonus:
        # if we need open slots, favor legal patterns that clear more cards.
        if must_release:
            item[
                "ai_score"
            ] += (
                len(
                    item["indices"]
                )
                * 95.0
            )

        # After the final refill, clearing more cards is even more valuable.
        if refills_left <= 0:
            item[
                "ai_score"
            ] += (
                len(
                    item["indices"]
                )
                * 145.0
            )

        learned_patterns.append(
            item
        )

    learned_patterns.sort(
        key=lambda p: -p[
            "ai_score"
        ]
    )

    # V8 fast probability stage:
    # 只计算最有希望的前N个动作，而不是所有顺子/对子/单张逐个计算。
    if (
        int(
            CFG.get(
                "probability_enabled",
                1,
            )
        )
        and learned_patterns
    ):
        top_n = max(
            1,
            int(
                CFG.get(
                    "probability_top_candidates",
                    6,
                )
            ),
        )

        for item in learned_patterns[
            :top_n
        ]:
            future_probs = (
                future_after_play_probabilities(
                    cards,
                    item,
                    hand_limit,
                )
            )

            item[
                "future_probabilities"
            ] = future_probs

            item[
                "future_probability_bonus"
            ] = (
                probability_strategy_bonus(
                    future_probs
                )
            )

            item[
                "ai_score"
            ] += item[
                "future_probability_bonus"
            ]

        learned_patterns[
            :top_n
        ] = sorted(
            learned_patterns[
                :top_n
            ],
            key=lambda p: -p[
                "ai_score"
            ],
        )

    patterns = (
        learned_patterns
    )

    def finalize(play):
        play = attach_play_learning(
            cards,
            hand_limit,
            race_elapsed,
            round_index,
            horse,
            play,
        )

        play[
            "_refill_stage"
        ] = int(
            current_refill_stage
        )

        play[
            "_refills_left"
        ] = int(
            refills_left
        )

        if (
            "learn_adjust"
            in play
        ):
            log(
                f"AI出牌评分：{play['name']} "
                f"基础={play['score']:.0f} "
                f"学习修正={play['learn_adjust']:+.1f} "
                f"概率修正={play.get('future_probability_bonus',0):+.1f} "
                f"样本={play.get('learn_samples',0)}；"
                f"补牌点={current_refill_stage}/3，"
                f"当前{len(cards)}/{hand_limit}，"
                f"目标<={target_hand_count}"
            )

        if (
            int(
                CFG.get(
                    "probability_log",
                    1,
                )
            )
            and play.get(
                "future_probabilities"
            )
            is not None
        ):
            log(
                "记牌概率：出完该组合后 -> "
                + probability_summary_text(
                    play[
                        "future_probabilities"
                    ]
                )
            )

        return play

    best = patterns[0]

    # 1) Highest priority: do not block a future 3-card refill.
    if must_release:
        return finalize(
            best
        )

    # 2) Even with enough free slots, a clearly profitable combo should
    #    be converted into speed instead of being held forever.
    proactive_threshold = float(
        CFG.get(
            "proactive_combo_multiplier",
            2.40,
        )
    )

    if (
        best["multiplier"]
        >= proactive_threshold
    ):
        return finalize(
            best
        )

    # 3) Rank/time pressure -> play the best legal option.
    if (
        aggressive
        or emergency
    ):
        return finalize(
            best
        )

    # 4) Otherwise wait briefly because 3 free refill slots are already open.
    return None



# ------------------------------------------------------------------
# Horse-selection AI
# ------------------------------------------------------------------

HORSE_CAPACITY = {
    "lower": 8,
    "middle": 9,
    "upper": 10,
}

HORSE_LABEL = {
    "lower": "下等马",
    "middle": "中等马",
    "upper": "上等马",
}


def _selection_card_edges(image):
    """
    选马页的“本轮手牌”比比赛中手牌叠得更紧，
    所以单独检测，不能复用比赛阶段的卡牌间距。
    """
    H, W = image.shape[:2]

    y0 = int(H * 0.60)
    y1 = int(H * 0.80)
    x0 = int(W * 0.16)
    x1 = int(W * 0.84)

    gray = cv2.cvtColor(
        image[y0:y1, x0:x1],
        cv2.COLOR_RGB2GRAY,
    )

    edges = cv2.Canny(
        gray,
        45,
        140,
    )

    strength = (
        edges.sum(axis=0)
        / 255.0
    )

    threshold = max(
        52.0 * (H / REFERENCE_H),
        (y1 - y0) * 0.48,
    )

    candidate_local = np.where(
        strength >= threshold
    )[0].tolist()

    clusters = []

    for local_x in candidate_local:
        x = int(
            local_x + x0
        )

        if (
            not clusters
            or x - clusters[-1][-1]
            > max(3, int(W * 0.009))
        ):
            clusters.append([x])
        else:
            clusters[-1].append(x)

    reps = []

    for cluster in clusters:
        reps.append(
            max(
                cluster,
                key=lambda xx: strength[
                    int(xx - x0)
                ],
            )
        )

    # 选马页叠牌间距约 24~28px（497宽窗口）。
    min_step = W * 0.038
    max_step = W * 0.066

    best = []

    for i, first in enumerate(reps):
        seq = [first]
        last = first
        spacings = []

        for x in reps[i + 1:]:
            distance = x - last

            if (
                min_step
                <= distance
                <= max_step
                and (
                    not spacings
                    or abs(
                        distance
                        - np.median(
                            spacings
                        )
                    )
                    <= W * 0.012
                )
            ):
                seq.append(x)
                spacings.append(
                    distance
                )
                last = x

            elif (
                distance
                > W * 0.10
                and len(seq) >= 2
            ):
                break

        if len(seq) > len(best):
            best = seq

    if len(best) >= 3:
        slope, intercept = np.polyfit(
            np.arange(
                len(best),
                dtype=float,
            ),
            np.asarray(
                best,
                dtype=float,
            ),
            1,
        )

        if (
            min_step
            <= slope
            <= max_step
        ):
            best = [
                int(round(
                    intercept
                    + slope * i
                ))
                for i in range(
                    len(best)
                )
            ]

    return best[:10]


def _selection_number_patch(
    image,
    left_x,
):
    H, W = image.shape[:2]

    # V3.2：两段实机录像标定，选马数字约在57.5%高度。
    y = int(
        H * 0.575
    )

    x0 = max(
        0,
        int(
            left_x
            - 1 * W / REFERENCE_W
        ),
    )

    x1 = min(
        W,
        int(
            left_x
            + 22 * W / REFERENCE_W
        ),
    )

    y1 = min(
        H,
        int(
            y
            + 32 * H / REFERENCE_H
        ),
    )

    patch = image[
        y:y1,
        x0:x1,
    ]

    if patch.size == 0:
        return None

    return cv2.resize(
        patch,
        (28, 32),
        interpolation=cv2.INTER_AREA,
    )


def _selection_patch_has_number_corner(
    patch,
):
    """
    排除最后那张蓝色牌背。

    数字牌左上角通常是白底数字，或者黄色道具牌；
    牌背则主要是蓝色，不应参与数字分类。
    """
    hsv = cv2.cvtColor(
        patch,
        cv2.COLOR_RGB2HSV,
    )

    corner = hsv[
        0:27,
        0:28,
    ]

    white_ratio = float(
        np.mean(
            (corner[:, :, 1] <= 70)
            & (corner[:, :, 2] >= 165)
        )
    )

    yellow_ratio = float(
        np.mean(
            (corner[:, :, 0] >= 10)
            & (corner[:, :, 0] <= 42)
            & (corner[:, :, 1] >= 75)
            & (corner[:, :, 2] >= 105)
        )
    )

    return (
        white_ratio >= 0.20
        or yellow_ratio >= 0.16
    )


def _selection_patch_similarity(
    patch,
    sample,
):
    """
    对选马页卡牌左上角做小范围平移匹配。
    """
    a = cv2.cvtColor(
        patch,
        cv2.COLOR_RGB2GRAY,
    ).astype(np.float32)

    b = cv2.cvtColor(
        sample,
        cv2.COLOR_RGB2GRAY,
    ).astype(np.float32)

    # 只看数字所在上部，减少卡面图案影响。
    a = a[:29, :34]
    b = b[:29, :34]

    best = -1.0

    for dy in range(-2, 3):
        for dx in range(-2, 3):
            shifted = np.roll(
                np.roll(
                    b,
                    dy,
                    axis=0,
                ),
                dx,
                axis=1,
            )

            if dy > 0:
                shifted[:dy, :] = (
                    shifted[dy:dy + 1, :]
                )
            elif dy < 0:
                shifted[dy:, :] = (
                    shifted[dy - 1:dy, :]
                )

            if dx > 0:
                shifted[:, :dx] = (
                    shifted[:, dx:dx + 1]
                )
            elif dx < 0:
                shifted[:, dx:] = (
                    shifted[:, dx - 1:dx]
                )

            av = a.ravel()
            bv = shifted.ravel()

            if (
                av.std() < 1e-6
                or bv.std() < 1e-6
            ):
                continue

            score = float(
                np.corrcoef(
                    av,
                    bv,
                )[0, 1]
            )

            best = max(
                best,
                score,
            )

    return best


def _classify_selection_value(
    patch,
):
    scores = {}

    for value, samples in (
        SELECTION_DIGIT_SAMPLES.items()
    ):
        if not samples:
            continue

        scores[value] = max(
            _selection_patch_similarity(
                patch,
                sample,
            )
            for sample in samples
        )

    if not scores:
        return (
            None,
            0.0,
            {},
        )

    best_value = max(
        scores,
        key=scores.get,
    )

    return (
        int(best_value),
        float(
            scores[best_value]
        ),
        scores,
    )


def _v32_selection_back_ratio(
    image,
    left_x,
):
    H, W = image.shape[:2]

    x0 = max(
        0,
        int(
            left_x
            + 5
            * W
            / REFERENCE_W
        ),
    )

    x1 = min(
        W,
        int(
            left_x
            + 70
            * W
            / REFERENCE_W
        ),
    )

    y0 = int(
        H * 0.585
    )

    y1 = int(
        H * 0.690
    )

    roi = image[
        y0:y1,
        x0:x1,
    ]

    if roi.size == 0:
        return 0.0

    hsv = cv2.cvtColor(
        roi,
        cv2.COLOR_RGB2HSV,
    )

    blue = (
        (hsv[:, :, 0] >= 90)
        & (hsv[:, :, 0] <= 130)
        & (hsv[:, :, 1] >= 60)
        & (hsv[:, :, 2] >= 70)
    )

    yellow = (
        (hsv[:, :, 0] >= 12)
        & (hsv[:, :, 0] <= 42)
        & (hsv[:, :, 1] >= 70)
        & (hsv[:, :, 2] >= 100)
    )

    blue_ratio = float(
        np.mean(
            blue
        )
    )

    yellow_ratio = float(
        np.mean(
            yellow
        )
    )

    if (
        blue_ratio >= 0.90
        and yellow_ratio <= 0.05
    ):
        return blue_ratio

    return 0.0


def _v32_selection_value_at(
    image,
    x,
):
    patch = _selection_number_patch(
        image,
        x,
    )

    if patch is None:
        return (
            None,
            0.0,
            0.0,
            {},
        )

    return _v32_fast_selection_value(
        patch
    )


def _v32_recover_selection_edges(
    image,
):
    """
    Fast recovery for tightly overlapped selection cards.

    Base Canny detection normally finds the right side correctly but can miss
    1~3 cards on the left. We extrapolate the spacing and search only a few
    pixels around each predicted position using the fast classifier.
    """
    base = _selection_card_edges(
        image
    )

    if not base:
        return []

    H, W = image.shape[:2]

    if len(base) >= 2:
        spacing = float(
            np.median(
                np.diff(
                    base
                )
            )
        )
    else:
        spacing = (
            W * 0.049
        )

    if not (
        W * 0.035
        <= spacing
        <= W * 0.070
    ):
        spacing = (
            W * 0.049
        )

    result = [
        int(
            x
        )
        for x in base
    ]

    first = float(
        result[
            0
        ]
    )

    radius = max(
        4,
        int(
            W * 0.014
        ),
    )

    while len(
        result
    ) < 9:
        predicted = (
            first
            - spacing
        )

        if predicted < (
            W * 0.16
        ):
            break

        best = None

        start_x = max(
            0,
            int(
                round(
                    predicted
                )
            )
            - radius,
        )

        end_x = min(
            W,
            int(
                round(
                    predicted
                )
            )
            + radius
            + 1,
        )

        for candidate_x in range(
            start_x,
            end_x,
        ):
            (
                value,
                score,
                margin,
                _,
            ) = _v32_selection_value_at(
                image,
                candidate_x,
            )

            if value is None:
                continue

            rank_score = (
                float(
                    score
                )
                + 0.15
                * float(
                    margin
                )
            )

            if (
                best is None
                or rank_score
                > best[
                    0
                ]
            ):
                best = (
                    rank_score,
                    float(
                        score
                    ),
                    int(
                        candidate_x
                    ),
                )

        if (
            best is None
            or best[
                1
            ]
            < 0.72
        ):
            break

        x = int(
            best[
                2
            ]
        )

        result.insert(
            0,
            x,
        )

        first = float(
            x
        )

    result.sort()

    deduped = []

    for x in result:
        if (
            not deduped
            or x
            - deduped[
                -1
            ]
            >= max(
                8,
                int(
                    W * 0.025
                ),
            )
        ):
            deduped.append(
                int(
                    x
                )
            )

    return deduped[
        :9
    ]


def read_selection_hand(
    image,
):
    if not SELECTION_DIGIT_SAMPLES:
        return []

    edges = _v32_recover_selection_edges(
        image
    )

    if not edges:
        return []

    cards = []

    rightmost = max(
        edges
    )

    for left_x in edges:
        # Face-down blue card appears as the rightmost candidate.
        if (
            left_x
            == rightmost
            and _v32_selection_back_ratio(
                image,
                left_x,
            )
            >= 0.90
        ):
            continue

        (
            value,
            score,
            margin,
            _,
        ) = _v32_selection_value_at(
            image,
            left_x,
        )

        if (
            value is None
            or float(
                score
            )
            < 0.62
        ):
            continue

        if (
            float(
                score
            )
            < 0.76
            and float(
                margin
            )
            < 0.025
        ):
            continue

        cards.append({
            "value": int(
                value
            ),
            "score": float(
                score
            ),
            "left_x": int(
                left_x
            ),
        })

    if not cards:
        return []

    counts = Counter(
        card[
            "value"
        ]
        for card in cards
    )

    if any(
        count > 4
        for count in counts.values()
    ):
        return []

    return cards


def _cards_from_values(
    values,
):
    return [
        {
            "index": i,
            "value": int(value),
            "special": False,
        }
        for i, value in enumerate(values)
    ]


def _best_pattern_multiplier(
    values,
):
    if not values:
        return 1.0

    patterns = generate_patterns(
        _cards_from_values(
            values
        )
    )

    if not patterns:
        return 1.0

    return max(
        float(
            item["multiplier"]
        )
        for item in patterns
    )



def _selection_remaining_counts(
    hand_values,
):
    """
    选马阶段可用于补牌的剩余牌库。

    规则：
    - 已有共享牌库状态：从当前剩余牌继续扣掉本轮已翻开的手牌；
    - 尚未初始化：使用标准40张（1~10各4张）再扣当前手牌。

    返回 1~10 的10元组。
    """
    if DECK_STATE.get(
        "initialized",
        False,
    ):
        remaining = [
            max(
                0,
                int(
                    DECK_STATE[
                        "remaining"
                    ].get(
                        value,
                        0,
                    )
                ),
            )
            for value in range(
                1,
                11,
            )
        ]
    else:
        remaining = [
            4
            for _ in range(
                10
            )
        ]

    # The persistent deck already excludes previously observed hand cards.
    # Only selection-page cards not yet present in tracked hand leave the
    # unseen pool now. Subtracting the whole hand again biases later rounds.
    visible = Counter(int(v) for v in hand_values if 1 <= int(v) <= 10)
    tracked = (_dict_counter(DECK_STATE["hand"])
               if DECK_STATE.get("initialized", False) else Counter())
    for value, count in visible.items():
        remaining[value - 1] = max(
            0, remaining[value - 1] - max(0, count - tracked.get(value, 0))
        )

    return tuple(
        remaining
    )


def _hand_counts_1_to_10(
    values,
):
    counts = [
        0
    ] * 10

    for value in values:
        value = int(
            value
        )

        if 1 <= value <= 10:
            counts[
                value - 1
            ] += 1

    return tuple(
        counts
    )


def _estimate_horse_capacity_v2(
    values,
    capacity,
):
    """
    V1.4：精确评估某匹马的手牌上限。

    与旧版900次随机模拟不同：
    - 使用共享40张牌的实际剩余数量；
    - 对补牌结果做精确多元超几何枚举；
    - 得到所有关键牌型的真实概率。
    """
    values = [
        int(
            v
        )
        for v in values
    ]

    missing = max(
        0,
        int(
            capacity
        )
        - len(
            values
        ),
    )

    current_best = (
        _best_pattern_multiplier(
            values
        )
        if values
        else 1.0
    )

    hand_counts = (
        _hand_counts_1_to_10(
            values
        )
    )

    remaining = (
        _selection_remaining_counts(
            values
        )
    )

    probs = (
        exact_future_pattern_probabilities(
            hand_counts,
            remaining,
            missing,
        )
    )

    expected = float(
        probs[
            "expected_best_multiplier"
        ]
    )

    return {
        "current": float(
            current_best
        ),
        "expected": expected,
        "capacity_gain": float(
            expected
            - current_best
        ),
        "strong_prob": float(
            probs[
                "ge_3x"
            ]
        ),
        "very_strong_prob": float(
            probs[
                "ge_4_2x"
            ]
        ),
        "bomb_prob": float(
            probs[
                "bomb"
            ]
        ),
        "triple_run_prob": float(
            probs[
                "triple_run"
            ]
        ),
        "pair4_prob": float(
            probs[
                "pair_run_4"
            ]
        ),
        "straight8_prob": float(
            probs[
                "straight_8"
            ]
        ),
        "draw_count": int(
            probs[
                "draw_count"
            ]
        ),
        "remaining_total": int(
            sum(
                remaining
            )
        ),
        "probabilities": probs,
    }


def _horse_round_reserve_factor(
    round_index,
):
    table = dict(
        CFG.get(
            "horse_round_reserve_factor",
            {
                "1": 1.0,
                "2": 0.42,
                "3": 0.0,
            },
        )
    )

    return float(
        table.get(
            str(
                int(
                    round_index
                )
            ),
            0.0
            if int(
                round_index
            ) >= 3
            else 1.0,
        )
    )


def _future_horse_capacity_values(hand_values, samples=64):
    """Estimate the option value of saving capacity for an unknown later hand.

    This is a bounded rollout from the shared unseen deck. It does not assume
    that each round starts with a fresh 40-card deck.
    """
    remaining = _selection_remaining_counts(hand_values)
    pool = [value for value, count in enumerate(remaining, 1)
            for _ in range(count)]
    if len(pool) < HORSE_CAPACITY["upper"]:
        return {name: 0.0 for name in HORSE_CAPACITY}
    seed = 20261014 + sum((i + 1) * count * 101 for i, count in enumerate(remaining))
    rng = random.Random(seed)
    totals = {name: 0.0 for name in HORSE_CAPACITY}
    for _ in range(samples):
        future = rng.sample(pool, HORSE_CAPACITY["upper"])
        for name, capacity in HORSE_CAPACITY.items():
            totals[name] += _best_pattern_multiplier(future[:capacity])
    return {name: total / samples for name, total in totals.items()}


def _horse_v2_utility(
    horse,
    data,
    round_index,
    smallest_expected,
    future_option_premium=0.0,
):
    """
    基础规则分：
      精确补牌期望
      + 强牌概率
      + 超强牌概率
      + 稀有大牌概率
      + 额外容量的边际收益
      + 马自身基础属性
      - 本轮提前消耗高等级马的机会成本
    """
    base_bonus = dict(
        CFG.get(
            "horse_base_bonus",
            {},
        )
    )

    reserve_penalty = dict(
        CFG.get(
            "horse_reserve_penalty",
            {},
        )
    )

    reserve_factor = (
        _horse_round_reserve_factor(
            round_index
        )
    )

    marginal_vs_smallest = max(
        0.0,
        float(
            data[
                "expected"
            ]
            - smallest_expected
        ),
    )

    # 如果更大的手牌容量确实能显著增加期望，
    # 自动抵消一部分“保留高马”的惩罚。
    raw_reserve = (
        float(
            reserve_penalty.get(
                horse,
                0.0,
            )
        )
        * reserve_factor
        + max(0.0, float(future_option_premium)) * reserve_factor
    )

    effective_reserve = max(
        0.0,
        raw_reserve
        - marginal_vs_smallest
        * 0.36,
    )

    utility = (
        float(
            data[
                "expected"
            ]
        )
        + float(
            CFG.get(
                "horse_capacity_gain_weight",
                0.72,
            )
        )
        * max(
            0.0,
            float(
                data[
                    "capacity_gain"
                ]
            ),
        )
        + float(
            CFG.get(
                "horse_prob_ge3_weight",
                0.88,
            )
        )
        * float(
            data[
                "strong_prob"
            ]
        )
        + float(
            CFG.get(
                "horse_prob_ge42_weight",
                0.58,
            )
        )
        * float(
            data[
                "very_strong_prob"
            ]
        )
        + float(
            CFG.get(
                "horse_prob_bomb_weight",
                0.46,
            )
        )
        * float(
            data[
                "bomb_prob"
            ]
        )
        + float(
            CFG.get(
                "horse_prob_triple_run_weight",
                0.34,
            )
        )
        * float(
            data[
                "triple_run_prob"
            ]
        )
        + float(
            CFG.get(
                "horse_prob_pair4_weight",
                0.24,
            )
        )
        * float(
            data[
                "pair4_prob"
            ]
        )
        + float(
            CFG.get(
                "horse_prob_straight8_weight",
                0.22,
            )
        )
        * float(
            data[
                "straight8_prob"
            ]
        )
        + float(
            base_bonus.get(
                horse,
                0.0,
            )
        )
        - effective_reserve
    )

    return (
        float(
            utility
        ),
        float(
            effective_reserve
        ),
        float(
            marginal_vs_smallest
        ),
    )


def _estimate_horse_capacity(
    values,
    capacity,
    simulations,
):
    """
    估计如果把当前手牌补到某匹马的上限，
    能形成多强的牌型。

    这里不假装知道尚未出现的牌；
    对未知补牌用1~10近似均匀模型，
    只比较“多一个/两个手牌位”的边际价值。
    """
    values = [
        int(v)
        for v in values
    ]

    missing = max(
        0,
        capacity - len(values),
    )

    current_multiplier = (
        _best_pattern_multiplier(
            values
        )
    )

    if missing <= 0:
        return {
            "current": current_multiplier,
            "expected": current_multiplier,
            "strong_prob": (
                1.0
                if current_multiplier
                >= float(
                    CFG.get(
                        "horse_strong_combo_threshold",
                        3.0,
                    )
                )
                else 0.0
            ),
            "very_strong_prob": (
                1.0
                if current_multiplier
                >= float(
                    CFG.get(
                        "horse_very_strong_combo_threshold",
                        4.2,
                    )
                )
                else 0.0
            ),
        }

    # Deterministic seed: same hand -> same estimate/log.
    seed = (
        9173
        + capacity * 1009
        + sum(
            (i + 1) * v * 37
            for i, v in enumerate(
                values
            )
        )
    )

    rng = random.Random(
        seed
    )

    strong_threshold = float(
        CFG.get(
            "horse_strong_combo_threshold",
            3.0,
        )
    )

    very_strong_threshold = float(
        CFG.get(
            "horse_very_strong_combo_threshold",
            4.2,
        )
    )

    multipliers = []

    # V5: use the ACTUAL shared-deck remainder when available.
    remaining_pool = (
        _remaining_deck_list()
        if DECK_STATE.get(
            "initialized",
            False,
        )
        else []
    )

    for _ in range(
        max(
            80,
            int(simulations),
        )
    ):
        simulated = list(
            values
        )

        if (
            remaining_pool
            and len(
                remaining_pool
            ) >= missing
        ):
            simulated.extend(
                rng.sample(
                    remaining_pool,
                    missing,
                )
            )
        elif remaining_pool:
            simulated.extend(
                list(
                    remaining_pool
                )
            )
        else:
            # Only before the shared deck has been reliably initialized.
            simulated.extend(
                rng.randint(
                    1,
                    10,
                )
                for _ in range(
                    missing
                )
            )

        multipliers.append(
            _best_pattern_multiplier(
                simulated
            )
        )

    arr = np.asarray(
        multipliers,
        dtype=np.float32,
    )

    return {
        "current": current_multiplier,
        "expected": float(
            arr.mean()
        ),
        "strong_prob": float(
            np.mean(
                arr >= strong_threshold
            )
        ),
        "very_strong_prob": float(
            np.mean(
                arr >= very_strong_threshold
            )
        ),
    }


def estimate_horse_choice(
    hand_values,
    availability,
    round_index=1,
):
    """
    V1.4 动态选马 V2。

    目标不是固定“下->中->上”，而是每轮重新算：
      1) 当前已经成什么牌；
      2) 下/中/上等马分别还能多拿几张牌；
      3) 基于当前共享40张牌剩余数量，精确计算补牌后牌型概率；
      4) 第1/2轮考虑保留高等马，第3轮保留成本=0；
      5) 最后叠加 AI Learning 历史结果。
    """
    available_names = [
        name
        for name in (
            "lower",
            "middle",
            "upper",
        )
        if availability.get(
            name,
            {},
        ).get(
            "available",
            False,
        )
    ]

    if not available_names:
        return (
            None,
            {},
        )

    # 只剩一匹时无需博弈，但仍计算指标和AI日志。
    metrics = {}

    use_v2 = bool(
        int(
            CFG.get(
                "horse_selection_v2_enabled",
                1,
            )
        )
    )

    for name in available_names:
        if use_v2:
            data = (
                _estimate_horse_capacity_v2(
                    hand_values,
                    HORSE_CAPACITY[
                        name
                    ],
                )
            )
        else:
            data = (
                _estimate_horse_capacity(
                    hand_values,
                    HORSE_CAPACITY[
                        name
                    ],
                    int(
                        CFG.get(
                            "horse_simulations",
                            900,
                        )
                    ),
                )
            )

            # 旧版字段补齐，保证后续统一处理。
            data.setdefault(
                "capacity_gain",
                float(
                    data[
                        "expected"
                    ]
                    - data[
                        "current"
                    ]
                ),
            )
            data.setdefault(
                "bomb_prob",
                0.0,
            )
            data.setdefault(
                "triple_run_prob",
                0.0,
            )
            data.setdefault(
                "pair4_prob",
                0.0,
            )
            data.setdefault(
                "straight8_prob",
                0.0,
            )
            data.setdefault(
                "draw_count",
                max(
                    0,
                    HORSE_CAPACITY[
                        name
                    ]
                    - len(
                        hand_values
                    ),
                ),
            )

        metrics[
            name
        ] = data

    smallest = min(
        available_names,
        key=lambda name: HORSE_CAPACITY[
            name
        ],
    )

    smallest_expected = float(
        metrics[
            smallest
        ][
            "expected"
        ]
    )

    future_capacity = (_future_horse_capacity_values(hand_values)
                       if use_v2 and round_index < 3 and len(available_names) > 1
                       else {name: 0.0 for name in HORSE_CAPACITY})
    future_base = future_capacity.get(smallest, 0.0)

    for name in available_names:
        data = metrics[
            name
        ]

        if use_v2:
            (
                utility,
                effective_reserve,
                marginal_vs_smallest,
            ) = _horse_v2_utility(
                name,
                data,
                round_index,
                smallest_expected,
                future_option_premium=max(0.0, future_capacity.get(name, 0.0) - future_base),
            )
        else:
            reserve_penalty = dict(
                CFG.get(
                    "horse_reserve_penalty",
                    {},
                )
            )
            base_bonus = dict(
                CFG.get(
                    "horse_base_bonus",
                    {},
                )
            )

            effective_reserve = (
                float(
                    reserve_penalty.get(
                        name,
                        0.0,
                    )
                )
                * _horse_round_reserve_factor(
                    round_index
                )
            )

            marginal_vs_smallest = max(
                0.0,
                float(
                    data[
                        "expected"
                    ]
                    - smallest_expected
                ),
            )

            utility = (
                float(
                    data[
                        "expected"
                    ]
                )
                + float(
                    data[
                        "strong_prob"
                    ]
                )
                * 0.90
                + float(
                    data[
                        "very_strong_prob"
                    ]
                )
                * 0.45
                + float(
                    base_bonus.get(
                        name,
                        0.0,
                    )
                )
                - effective_reserve
            )

        learned, samples = (
            horse_learning_adjustment(
                hand_values,
                availability,
                name,
                round_index,
            )
        )

        ai_extra = float(
            CFG.get(
                "horse_ai_v2_extra_weight",
                1.0,
            )
        )

        data[
            "utility"
        ] = float(
            utility
        )
        data[
            "reserve_penalty_effective"
        ] = float(
            effective_reserve
        )
        data[
            "marginal_vs_smallest"
        ] = float(
            marginal_vs_smallest
        )
        data[
            "learned_adjustment"
        ] = float(
            learned
        )
        data[
            "learned_samples"
        ] = int(
            samples
        )
        data[
            "final_utility"
        ] = float(
            utility
            + learned
            * ai_extra
        )

    chosen = max(
        available_names,
        key=lambda name: metrics[
            name
        ][
            "final_utility"
        ],
    )

    # 第3轮：没有后续轮次，保留高马没有意义。
    # 因此完全按当前局期望 + AI结果选。
    if int(
        round_index
    ) >= 3:
        for name in available_names:
            metrics[
                name
            ][
                "round3_no_reserve"
            ] = True

    # 当前已经有很强的现成牌时，默认用能容纳当前手牌的最小马，
    # 除非更高容量确实带来足够大的“最终效用”优势。
    current_best = (
        _best_pattern_multiplier(
            hand_values
        )
        if hand_values
        else 1.0
    )

    if (
        int(
            round_index
        ) < 3
        and current_best
        >= float(
            CFG.get(
                "horse_very_strong_combo_threshold",
                4.2,
            )
        )
        and len(
            hand_values
        )
        <= HORSE_CAPACITY[
            smallest
        ]
    ):
        margin = float(
            CFG.get(
                "horse_conserve_strong_hand_margin",
                0.46,
            )
        )

        if (
            metrics[
                chosen
            ][
                "final_utility"
            ]
            - metrics[
                smallest
            ][
                "final_utility"
            ]
            < margin
        ):
            chosen = smallest
            metrics[
                smallest
            ][
                "reason_override"
            ] = (
                "当前已持有>=4.2x强牌；"
                "更高马的边际收益不足，保留高等马给后续轮次"
            )

    # 仅在几乎同分时做极低概率探索，为AI学习积累反事实样本。
    exploration_rate = float(
        CFG.get(
            "learning_exploration_rate",
            0.01,
        )
    )

    if (
        learning_enabled()
        and exploration_rate > 0
        and len(
            available_names
        ) >= 2
        and random.random()
        < exploration_rate
    ):
        best_score = float(
            metrics[
                chosen
            ][
                "final_utility"
            ]
        )

        near = [
            name
            for name in available_names
            if (
                best_score
                - float(
                    metrics[
                        name
                    ][
                        "final_utility"
                    ]
                )
                <= 0.14
            )
        ]

        if len(
            near
        ) >= 2:
            chosen = random.choice(
                near
            )
            metrics[
                chosen
            ][
                "exploration"
            ] = True

    return (
        chosen,
        metrics,
    )


# ------------------------------------------------------------------
# V4 real-time rank + fast hand cache
# ------------------------------------------------------------------

_HAND_CACHE = {
    "thumb": None,
    "cards": None,
}


def invalidate_hand_cache():
    _HAND_CACHE[
        "thumb"
    ] = None

    _HAND_CACHE[
        "cards"
    ] = None


def _hand_thumbnail(
    image,
):
    H, W = image.shape[:2]

    roi = image[
        int(H * 0.58):
        int(H * 0.94),
        int(W * 0.08):
        int(W * 0.96),
    ]

    if roi.size == 0:
        return None

    gray = cv2.cvtColor(
        roi,
        cv2.COLOR_RGB2GRAY,
    )

    return cv2.resize(
        gray,
        (72, 28),
        interpolation=cv2.INTER_AREA,
    ).astype(
        np.float32
    )


def read_hand_fast(
    image,
):
    """
    Avoid re-running expensive digit classification while the visible hand
    has not materially changed.
    """
    if not DIGIT_BINARY:
        return []

    if not int(
        CFG.get(
            "hand_cache_enabled",
            1,
        )
    ):
        return read_hand(
            image
        )

    thumb = _hand_thumbnail(
        image
    )

    if thumb is None:
        return []

    old_thumb = _HAND_CACHE.get(
        "thumb"
    )

    old_cards = _HAND_CACHE.get(
        "cards"
    )

    if (
        old_thumb is not None
        and old_cards is not None
        and old_thumb.shape
        == thumb.shape
    ):
        diff = float(
            np.mean(
                np.abs(
                    thumb
                    - old_thumb
                )
            )
        )

        if (
            diff
            <= float(
                CFG.get(
                    "hand_cache_diff_threshold",
                    2.2,
                )
            )
        ):
            return [
                dict(card)
                for card in old_cards
            ]

    cards = read_hand(
        image
    )

    _HAND_CACHE[
        "thumb"
    ] = thumb

    _HAND_CACHE[
        "cards"
    ] = [
        dict(card)
        for card in cards
    ]

    return cards


def detect_realtime_rank(
    image,
):
    """
    The user's live place is the fixed circular 1/4, 2/4, 3/4, 4/4 badge
    on the upper-right side of the race screen.

    Templates 1/2/3 are taken directly from the user's actual video.
    For 4th place we only infer 4 when the common "/4" widget is clearly
    present and 1/2/3 do not match reliably.
    """
    if (
        not REALTIME_RANK_TEMPLATES
        and (
            REALTIME_RANK_WIDGET is None
            or getattr(
                REALTIME_RANK_WIDGET,
                "size",
                0,
            )
            <= 0
        )
    ):
        return (
            None,
            0.0,
        )

    if not int(
        CFG.get(
            "realtime_rank_enabled",
            1,
        )
    ):
        return (
            None,
            0.0,
        )

    H, W = image.shape[:2]

    roi = (
        W * 0.76,
        H * 0.215,
        W,
        H * 0.345,
    )

    threshold = float(
        CFG.get(
            "realtime_rank_threshold",
            0.64,
        )
    )

    best_rank = None
    best_score = -1.0

    # We want scores even slightly below threshold so we can compare ranks.
    for rank, template in (
        REALTIME_RANK_TEMPLATES.items()
    ):
        match = template_match(
            image,
            template,
            threshold=0.40,
            roi=roi,
        )

        if (
            match is not None
            and match["score"]
            > best_score
        ):
            best_rank = int(
                rank
            )
            best_score = float(
                match["score"]
            )

    if (
        best_rank is not None
        and best_score >= threshold
    ):
        return (
            best_rank,
            best_score,
        )

    # Conservative 4th-place fallback.
    widget = template_match(
        image,
        REALTIME_RANK_WIDGET,
        threshold=0.70,
        roi=(
            W * 0.86,
            H * 0.24,
            W,
            H * 0.34,
        ),
    )

    if (
        widget is not None
        and best_score < threshold - 0.06
    ):
        return (
            4,
            float(
                widget["score"]
            ),
        )

    return (
        None,
        max(
            0.0,
            best_score,
        ),
    )


def record_realtime_rank_change(
    old_rank,
    new_rank,
    elapsed,
):
    """
    Real-time rank is used as a small shaping signal for the most recent
    successful play. Final placement remains the dominant reward.
    """
    if (
        old_rank is None
        or new_rank is None
        or old_rank == new_rank
    ):
        return

    event = {
        "time": float(
            elapsed
        ),
        "from": int(
            old_rank
        ),
        "to": int(
            new_rank
        ),
    }

    MATCH_MEMORY.setdefault(
        "rank_events",
        [],
    ).append(
        event
    )

    # Positive = rank improved, e.g. 3 -> 1 = +2.
    delta = int(
        old_rank
        - new_rank
    )

    plays = MATCH_MEMORY.get(
        "play_decisions",
        [],
    )

    if plays:
        plays[-1][
            "rank_shaping"
        ] = (
            float(
                plays[-1].get(
                    "rank_shaping",
                    0.0,
                )
            )
            + delta
            * float(
                CFG.get(
                    "realtime_rank_shaping_weight",
                    0.10,
                )
            )
        )

# ------------------------------------------------------------------
# Screen-state helpers
# ------------------------------------------------------------------

_MATCH_PAGE_DIAG_LAST = 0.0


def _green_dominance_ratio(
    image,
    x0r,
    y0r,
    x1r,
    y1r,
):
    if (
        image is None
        or getattr(image, "size", 0) <= 0
    ):
        return 0.0

    H, W = image.shape[:2]

    x0 = max(0, min(W - 1, int(round(W * x0r))))
    x1 = max(x0 + 1, min(W, int(round(W * x1r))))
    y0 = max(0, min(H - 1, int(round(H * y0r))))
    y1 = max(y0 + 1, min(H, int(round(H * y1r))))

    roi = image[y0:y1, x0:x1]

    if roi.size == 0:
        return 0.0

    # Green is channel 1 in both RGB and BGR.
    c0 = roi[:, :, 0].astype(np.int16)
    g = roi[:, :, 1].astype(np.int16)
    c2 = roi[:, :, 2].astype(np.int16)

    mask = (
        (g >= 75)
        & (g >= c0 + 7)
        & (g >= c2 + 8)
    )

    return float(np.mean(mask))


def _fixed_match_page_fingerprint(image):
    quick = _green_dominance_ratio(
        image,
        0.66,
        0.80,
        0.95,
        0.875,
    )

    arena = _green_dominance_ratio(
        image,
        0.03,
        0.875,
        0.96,
        0.93,
    )

    weekly = _green_dominance_ratio(
        image,
        0.28,
        0.82,
        0.72,
        0.90,
    )

    H, W = image.shape[:2]

    if quick >= 0.16 and arena >= 0.16:
        return {
            "layout": "quick_card_fixed",
            "x": int(W * 0.68),
            "y": int(H * 0.815),
            "w": max(1, int(W * 0.25)),
            "h": max(1, int(H * 0.050)),
            "cx": float(W * 0.806),
            "cy": float(H * 0.840),
            "fill": float(quick),
            "score": 10.0 + quick + arena,
        }

    if weekly >= 0.28 and quick < 0.14:
        return {
            "layout": "weekly_center_fixed",
            "x": int(W * 0.30),
            "y": int(H * 0.835),
            "w": max(1, int(W * 0.40)),
            "h": max(1, int(H * 0.055)),
            "cx": float(W * 0.500),
            "cy": float(H * 0.863),
            "fill": float(weekly),
            "score": 9.0 + weekly,
        }

    return None


def _match_page_diag(image):
    global _MATCH_PAGE_DIAG_LAST

    now = time.monotonic()

    if now - _MATCH_PAGE_DIAG_LAST < 2.0:
        return

    _MATCH_PAGE_DIAG_LAST = now

    quick = _green_dominance_ratio(
        image, 0.66, 0.80, 0.95, 0.875
    )
    arena = _green_dominance_ratio(
        image, 0.03, 0.875, 0.96, 0.93
    )
    weekly = _green_dominance_ratio(
        image, 0.28, 0.82, 0.72, 0.90
    )

    log(
        "[V2.7匹配页诊断] "
        f"quick={quick:.3f}；"
        f"arena={arena:.3f}；"
        f"weekly={weekly:.3f}；"
        f"frame={image.shape[1]}x{image.shape[0]}"
    )



def find_android_start_match_button(
    image,
):
    """
    V2.1 匹配页专用检测。

    支持用户提供的两种真实布局：

    A. 图1“欢乐模式/快速匹配”：
       绿色“开始匹配”位于右下卡片内，
       中心约 (0.806W, 0.840H)。

    B. 用户上传视频中的周常/单人匹配页：
       绿色“开始匹配”位于底部中央，
       中心约 (0.500W, 0.863H)。

    不使用OCR，只识别真正的绿色横向按钮，并按布局几何评分。
    """
    if (
        image is None
        or getattr(
            image,
            "size",
            0,
        )
        <= 0
    ):
        return None

    H, W = image.shape[
        :2
    ]

    hsv = cv2.cvtColor(
        image,
        cv2.COLOR_RGB2HSV,
    )

    if hsv is None:
        return None

    hh, ss, vv = cv2.split(
        hsv
    )

    # 真按钮是高饱和亮绿色。
    green = (
        (hh >= 35)
        & (hh <= 95)
        & (ss >= 85)
        & (vv >= 80)
    ).astype(
        np.uint8
    ) * 255

    # 只允许屏幕下部。
    green[
        :int(
            H
            * 0.72
        ),
        :
    ] = 0

    # 不看最底部返回键/宣传条区域。
    green[
        int(
            H
            * 0.92
        ):
        ,
        :
    ] = 0

    kw = max(
        3,
        int(
            round(
                W
                * 0.010
            )
        ),
    )

    kh = max(
        3,
        int(
            round(
                H
                * 0.004
            )
        ),
    )

    green = cv2.morphologyEx(
        green,
        cv2.MORPH_CLOSE,
        np.ones(
            (
                kh,
                kw,
            ),
            dtype=np.uint8,
        ),
        iterations=2,
    )

    count, labels, stats, centroids = (
        cv2.connectedComponentsWithStats(
            green,
            connectivity=8,
        )
    )

    candidates = []

    # 两种真实页面的标准几何中心。
    profiles = (
        {
            "layout": "quick_card",
            "cx": 0.806,
            "cy": 0.840,
            "wr": 0.254,
            "hr": 0.037,
            "cx_min": 0.66,
            "cx_max": 0.94,
            "cy_min": 0.79,
            "cy_max": 0.89,
            "wr_min": 0.16,
            "wr_max": 0.35,
            "hr_min": 0.022,
            "hr_max": 0.075,
        },
        {
            "layout": "weekly_center",
            "cx": 0.500,
            "cy": 0.863,
            "wr": 0.421,
            "hr": 0.046,
            "cx_min": 0.34,
            "cx_max": 0.66,
            "cy_min": 0.80,
            "cy_max": 0.90,
            "wr_min": 0.28,
            "wr_max": 0.54,
            "hr_min": 0.028,
            "hr_max": 0.080,
        },
    )

    for i in range(
        1,
        count,
    ):
        x, y, bw, bh, area = [
            int(v)
            for v
            in stats[
                i
            ]
        ]

        if (
            bw <= 0
            or bh <= 0
            or area <= 0
        ):
            continue

        cx, cy = centroids[
            i
        ]

        xr = (
            float(cx)
            / float(
                max(
                    W,
                    1,
                )
            )
        )

        yr = (
            float(cy)
            / float(
                max(
                    H,
                    1,
                )
            )
        )

        wr = (
            float(bw)
            / float(
                max(
                    W,
                    1,
                )
            )
        )

        hr = (
            float(bh)
            / float(
                max(
                    H,
                    1,
                )
            )
        )

        aspect = (
            float(bw)
            / float(
                max(
                    bh,
                    1,
                )
            )
        )

        fill = (
            float(area)
            / float(
                max(
                    bw * bh,
                    1,
                )
            )
        )

        # “开始匹配”一定是明显横向按钮。
        if not (
            2.4
            <= aspect
            <= 8.5
        ):
            continue

        if fill < 0.42:
            continue

        for profile in profiles:
            if not (
                profile[
                    "cx_min"
                ]
                <= xr
                <= profile[
                    "cx_max"
                ]
            ):
                continue

            if not (
                profile[
                    "cy_min"
                ]
                <= yr
                <= profile[
                    "cy_max"
                ]
            ):
                continue

            if not (
                profile[
                    "wr_min"
                ]
                <= wr
                <= profile[
                    "wr_max"
                ]
            ):
                continue

            if not (
                profile[
                    "hr_min"
                ]
                <= hr
                <= profile[
                    "hr_max"
                ]
            ):
                continue

            # 几何越接近真实截图/视频，分越高。
            geometry_error = (
                abs(
                    xr
                    - profile[
                        "cx"
                    ]
                )
                / 0.12
                + abs(
                    yr
                    - profile[
                        "cy"
                    ]
                )
                / 0.07
                + abs(
                    wr
                    - profile[
                        "wr"
                    ]
                )
                / 0.16
                + abs(
                    hr
                    - profile[
                        "hr"
                    ]
                )
                / 0.05
            )

            score = (
                4.0
                - geometry_error
                + 1.4
                * fill
                + 0.25
                * min(
                    aspect,
                    6.0,
                )
            )

            candidates.append(
                {
                    "layout": profile[
                        "layout"
                    ],
                    "x": x,
                    "y": y,
                    "w": bw,
                    "h": bh,
                    "cx": float(
                        cx
                    ),
                    "cy": float(
                        cy
                    ),
                    "fill": fill,
                    "score": score,
                }
            )

    if not candidates:
        fixed = _fixed_match_page_fingerprint(
            image
        )

        if fixed is not None:
            return fixed

        return None

    candidates.sort(
        key=lambda item: item[
            "score"
        ],
        reverse=True,
    )

    return candidates[0]


def confirm_android_start_match_button(
    image,
):
    """
    两帧确认开始匹配按钮。
    页面动画/比赛UI即使偶尔出现绿色块，也不会直接点击。
    """
    first = (
        find_android_start_match_button(
            image
        )
    )

    if first is None:
        return None

    engine = globals().get(
        "VISION_ENGINE"
    )

    if engine is None:
        return first

    second_frame = (
        engine.wait_for_new_frame(
            timeout=0.15,
            max_age=0.12,
        )
    )

    if second_frame is None:
        return None

    image2, _ = second_frame

    second = (
        find_android_start_match_button(
            image2
        )
    )

    if second is None:
        return None

    if (
        first[
            "layout"
        ]
        != second[
            "layout"
        ]
    ):
        return None

    H, W = image2.shape[
        :2
    ]

    if (
        abs(
            first[
                "cx"
            ]
            - second[
                "cx"
            ]
        )
        > W
        * 0.025
        or abs(
            first[
                "cy"
            ]
            - second[
                "cy"
            ]
        )
        > H
        * 0.020
    ):
        return None

    result = dict(
        second
    )

    result[
        "cx"
    ] = (
        first[
            "cx"
        ]
        + second[
            "cx"
        ]
    ) / 2.0

    result[
        "cy"
    ] = (
        first[
            "cy"
        ]
        + second[
            "cy"
        ]
    ) / 2.0

    return result


def is_lobby(
    image,
):
    # V2.1 Android实时模式：
    # 必须看到真实“开始匹配”按钮才认作匹配页。
    # 不再让空模板/兼容模板误触发大厅点击。
    button = find_android_start_match_button(
        image
    )

    if button is not None:
        return True

    if SCRCPY_REALTIME_ENABLED:
        _match_page_diag(
            image
        )
        return False

    # 非Android旧Windows路径才保留模板兼容。
    H, W = image.shape[:2]

    match = template_match(
        image,
        safe_template_get("lobby_title"),
        threshold=0.60,
        roi=(
            W * 0.10,
            H * 0.10,
            W * 0.90,
            H * 0.40,
        ),
    )

    return match is not None

_V31_HORSE_SEQUENCE = {
    1: "upper",
    2: "middle",
    3: "lower",
}

_V31_HORSE_TAP_NORM = {
    "upper": (0.22, 0.48),
    "middle": (0.50, 0.48),
    "lower": (0.78, 0.48),
}


def _v31_region_mask_ratio(
    image,
    x0r,
    y0r,
    x1r,
    y1r,
    kind,
):
    H, W = image.shape[:2]
    roi = image[
        int(H * y0r):int(H * y1r),
        int(W * x0r):int(W * x1r),
    ]

    if roi.size == 0:
        return 0.0

    hsv = cv2.cvtColor(
        roi,
        cv2.COLOR_RGB2HSV,
    )

    if kind == "yellow":
        mask = (
            (hsv[:, :, 0] >= 15)
            & (hsv[:, :, 0] <= 40)
            & (hsv[:, :, 1] >= 90)
            & (hsv[:, :, 2] >= 125)
        )

    elif kind == "blue":
        mask = (
            (hsv[:, :, 0] >= 90)
            & (hsv[:, :, 0] <= 130)
            & (hsv[:, :, 1] >= 50)
            & (hsv[:, :, 2] >= 50)
        )

    else:
        return 0.0

    return float(
        np.mean(mask)
    )


def _v31_selection_page_metrics(image):
    """
    Calibrated from the user's 2026-10-08 full race recording.

    The unselected horse page has three simultaneous fixed cues:
      1) yellow "点击选择本轮出战马" strip;
      2) blue round/countdown upper background;
      3) dark-blue "本轮手牌" panel.

    Requiring all three separates it from result/reveal pages.
    """
    yellow = _v31_region_mask_ratio(
        image,
        0.18,
        0.32,
        0.82,
        0.38,
        "yellow",
    )

    upper_blue = _v31_region_mask_ratio(
        image,
        0.00,
        0.08,
        1.00,
        0.31,
        "blue",
    )

    hand_blue = _v31_region_mask_ratio(
        image,
        0.03,
        0.53,
        0.97,
        0.75,
        "blue",
    )

    return (
        float(yellow),
        float(upper_blue),
        float(hand_blue),
    )


def _v31_is_unselected_horse_page(image):
    if (
        image is None
        or getattr(image, "size", 0) <= 0
    ):
        return False

    yellow, upper_blue, hand_blue = (
        _v31_selection_page_metrics(
            image
        )
    )

    return (
        yellow >= 0.20
        and upper_blue >= 0.85
        and hand_blue >= 0.65
    )


def _v31_tap_normalized(nx, ny):
    _ensure_adb()

    try:
        ADB_DEVICE.wm_size()
    except Exception:
        pass

    dw, dh = getattr(
        ADB_DEVICE,
        "_input_size",
        (
            1080,
            2376,
        ),
    )

    dx = float(dw) * float(nx)
    dy = float(dh) * float(ny)

    ok = ADB_DEVICE.tap(
        dx,
        dy,
    )

    _invalidate_async_frame()

    return (
        bool(ok),
        float(dx),
        float(dy),
    )


def _v31_click_horse_safe(horse, round_index):
    if horse not in _V31_HORSE_TAP_NORM:
        return False

    nx, ny = _V31_HORSE_TAP_NORM[
        horse
    ]

    ok, dx, dy = _v31_tap_normalized(
        nx,
        ny,
    )

    label = {
        "upper": "上等马",
        "middle": "中等马",
        "lower": "下等马",
    }[
        horse
    ]

    if ok:
        log(
            f"[点击] 第{round_index}轮{label} "
            f"→ Android({dx:.0f},{dy:.0f})"
        )
    else:
        log(
            f"[点击失败] 第{round_index}轮{label}"
        )

    return bool(ok)



_V32_USED_HORSES = set()

_V32_SELECTION_RUNTIME = {
    "round": None,
    "first_seen": 0.0,
    "last_hand": None,
    "stable_count": 0,
    "best_hand": [],
    "best_conf": 0.0,
}


def _v32_reset_selection_runtime():
    _V32_SELECTION_RUNTIME.update({
        "round": None,
        "first_seen": 0.0,
        "last_hand": None,
        "stable_count": 0,
        "best_hand": [],
        "best_conf": 0.0,
    })


def _v32_reset_match_selection():
    _V32_USED_HORSES.clear()
    _v32_reset_selection_runtime()


def _v32_legal_horses(
    image,
):
    info = horse_availability(
        image
    )

    centers = {
        "upper": (
            0.22,
            0.48,
        ),
        "middle": (
            0.50,
            0.48,
        ),
        "lower": (
            0.78,
            0.48,
        ),
    }

    # Game rule: each horse can be used once.
    # Program state is more reliable than saturation alone.
    for name in (
        "lower",
        "middle",
        "upper",
    ):
        info.setdefault(
            name,
            {},
        )

        info[
            name
        ][
            "available"
        ] = (
            name
            not in _V32_USED_HORSES
        )

        info[
            name
        ][
            "center"
        ] = (
            image.shape[
                1
            ]
            * centers[
                name
            ][
                0
            ],
            image.shape[
                0
            ]
            * centers[
                name
            ][
                1
            ],
        )

    return info


def _v32_smallest_remaining(
    availability,
):
    names = [
        name
        for name in (
            "lower",
            "middle",
            "upper",
        )
        if availability.get(
            name,
            {},
        ).get(
            "available",
            False,
        )
    ]

    if not names:
        return None

    return min(
        names,
        key=lambda name: HORSE_CAPACITY[
            name
        ],
    )



def is_horse_select(
    image,
):
    # V2.8：当前页只要还能看到“开始匹配”，就绝不允许识别成选马页。
    # 实测V2.7在大厅背景马匹区域上会误满足horse_availability颜色条件。
    if (
        SCRCPY_REALTIME_ENABLED
        and find_android_start_match_button(
            image
        )
        is not None
    ):
        return False

    H, W = image.shape[:2]

    # V3.1：实时版只使用视频标定后的“未选马页面”三重特征。
    # 不再用泛化的高饱和马匹区域，避免比赛画面/揭晓页误判。
    if SCRCPY_REALTIME_ENABLED:
        return _v31_is_unselected_horse_page(
            image
        )

    template = safe_template_get(
        "horse_select_prompt"
    )

    if template is None:
        return False

    match = template_match(
        image,
        template,
        threshold=0.56,
        roi=(
            W * 0.10,
            H * 0.28,
            W * 0.90,
            H * 0.45,
        ),
    )

    return match is not None

def is_race_end(image):
    H, W = image.shape[:2]

    template = safe_template_get(
        "race_end_title"
    )

    if template is None:
        return False

    return (
        template_match(
            image,
            template,
            threshold=0.58,
            roi=(
                W * 0.10,
                H * 0.10,
                W * 0.90,
                H * 0.38,
            ),
        )
        is not None
    )

def find_reward_button(image):
    H, W = image.shape[:2]

    template = safe_template_get(
        "reward_button"
    )

    if template is None:
        return None

    return template_match(
        image,
        template,
        threshold=0.54,
        roi=(
            W * 0.20,
            H * 0.80,
            W * 0.82,
            H,
        ),
    )

def horse_availability(image):
    H, W = image.shape[:2]

    hsv = cv2.cvtColor(
        image,
        cv2.COLOR_RGB2HSV,
    )

    rois = {
        "upper": (0.08, 0.40, 0.36, 0.56),
        "middle": (0.36, 0.40, 0.64, 0.56),
        "lower": (0.64, 0.40, 0.92, 0.56),
    }

    centers = {
        "upper": (0.22, 0.48),
        "middle": (0.50, 0.48),
        "lower": (0.78, 0.48),
    }

    result = {}

    for name, (x0, y0, x1, y1) in rois.items():
        crop = hsv[
            int(H * y0):int(H * y1),
            int(W * x0):int(W * x1),
        ]

        sat = float(
            crop[:, :, 1].mean()
        )

        saturated_ratio = float(
            np.mean(
                crop[:, :, 1] > 70
            )
        )

        result[name] = {
            "available": (
                sat >= 90
                and saturated_ratio >= 0.48
            ),
            "sat": sat,
            "ratio": saturated_ratio,
            "center": (
                W * centers[name][0],
                H * centers[name][1],
            ),
        }

    return result

def choose_horse(
    image,
    round_index=1,
):
    """
    V3.2 fast horse AI.

    R1/R2:
      - read visible cards;
      - prefer 2 identical consecutive reads;
      - dynamic expected-value comparison across remaining horses.

    R3:
      - only one unused horse remains -> click immediately.

    Timing:
      - normally 2 frames are enough;
      - no more than ~0.65s waiting for stable recognition;
      - ~1.20s hard fallback, far below the ~10s countdown.
    """
    if not _v31_is_unselected_horse_page(
        image
    ):
        return (
            None,
            None,
            {},
            [],
            {},
        )

    ri = max(
        1,
        min(
            3,
            int(
                round_index
            ),
        ),
    )

    availability = _v32_legal_horses(
        image
    )

    remaining = [
        name
        for name in (
            "lower",
            "middle",
            "upper",
        )
        if availability.get(
            name,
            {},
        ).get(
            "available",
            False,
        )
    ]

    if not remaining:
        return (
            None,
            None,
            availability,
            [],
            {},
        )

    # Third round: there is no strategic reason to wait.
    if (
        ri >= 3
        or len(
            remaining
        )
        == 1
    ):
        horse = (
            remaining[
                0
            ]
            if len(
                remaining
            )
            == 1
            else _v32_smallest_remaining(
                availability
            )
        )

        _v32_reset_selection_runtime()

        return (
            horse,
            availability[
                horse
            ],
            availability,
            [],
            {},
        )

    now = time.monotonic()

    state = _V32_SELECTION_RUNTIME

    if state[
        "round"
    ] != ri:
        _v32_reset_selection_runtime()

        state[
            "round"
        ] = ri

        state[
            "first_seen"
        ] = now

    cards = read_selection_hand(
        image
    )

    values = [
        int(
            card[
                "value"
            ]
        )
        for card in cards
    ]

    confidence = (
        float(
            np.mean([
                float(
                    card.get(
                        "score",
                        0.0,
                    )
                )
                for card in cards
            ])
        )
        if cards
        else 0.0
    )

    if values:
        key = tuple(
            values
        )

        if state[
            "last_hand"
        ] == key:
            state[
                "stable_count"
            ] += 1
        else:
            state[
                "last_hand"
            ] = key

            state[
                "stable_count"
            ] = 1

        if (
            len(
                values
            )
            > len(
                state[
                    "best_hand"
                ]
            )
            or (
                len(
                    values
                )
                == len(
                    state[
                        "best_hand"
                    ]
                )
                and confidence
                > float(
                    state[
                        "best_conf"
                    ]
                )
            )
        ):
            state[
                "best_hand"
            ] = list(
                values
            )

            state[
                "best_conf"
            ] = float(
                confidence
            )

    elapsed = max(
        0.0,
        now
        - float(
            state[
                "first_seen"
            ]
        ),
    )

    stable = (
        state[
            "stable_count"
        ]
        >= 2
        and bool(
            values
        )
    )

    if (
        not stable
        and elapsed
        < 0.65
    ):
        return (
            None,
            None,
            availability,
            values,
            {
                "_waiting": True,
                "_elapsed": elapsed,
            },
        )

    hand_values = (
        list(
            values
        )
        if values
        else list(
            state[
                "best_hand"
            ]
        )
    )

    if not hand_values:
        if elapsed < 1.20:
            return (
                None,
                None,
                availability,
                [],
                {
                    "_waiting": True,
                    "_elapsed": elapsed,
                },
            )

        horse = _v32_smallest_remaining(
            availability
        )

        return (
            horse,
            availability[
                horse
            ],
            availability,
            [],
            {},
        )

    horse, metrics = estimate_horse_choice(
        hand_values,
        availability,
        round_index=ri,
    )

    if horse is None:
        horse = _v32_smallest_remaining(
            availability
        )

        metrics = {}

    return (
        horse,
        availability[
            horse
        ],
        availability,
        hand_values,
        metrics,
    )


def _out_button_state(
    image,
):
    """
    V3.3 出牌按钮状态识别。

    用户实机录像确认：
      - 可出牌：按钮整体变成蓝色；
      - CD中：显示“出牌4秒/3秒/2秒/1秒”，按钮由灰白逐步变蓝；
      - 未选中有效牌/按钮不存在：不应点击。

    不做OCR，只看按钮本体的颜色填充和亮度结构。
    """
    if (
        image is None
        or getattr(
            image,
            "size",
            0,
        )
        <= 0
    ):
        return (
            "absent",
            {},
        )

    H, W = image.shape[:2]

    # 录像标定后的紧按钮区域。
    roi = image[
        int(
            H * 0.910
        ):int(
            H * 0.975
        ),
        int(
            W * 0.340
        ):int(
            W * 0.670
        ),
    ]

    if roi.size == 0:
        return (
            "absent",
            {},
        )

    hsv = cv2.cvtColor(
        roi,
        cv2.COLOR_RGB2HSV,
    )

    gray = cv2.cvtColor(
        roi,
        cv2.COLOR_RGB2GRAY,
    )

    blue = (
        (hsv[:, :, 0] >= 85)
        & (hsv[:, :, 0] <= 125)
        & (hsv[:, :, 1] >= 50)
        & (hsv[:, :, 2] >= 100)
    )

    hh, ww = gray.shape[:2]

    top_hsv = hsv[
        :max(
            1,
            hh // 2,
        ),
        :,
    ]

    top_white = (
        (top_hsv[:, :, 1] < 60)
        & (top_hsv[:, :, 2] > 120)
    )

    text_band = gray[
        int(
            hh * 0.10
        ):max(
            int(
                hh * 0.70
            ),
            int(
                hh * 0.10
            )
            + 1,
        ),
        int(
            ww * 0.05
        ):max(
            int(
                ww * 0.95
            ),
            int(
                ww * 0.05
            )
            + 1,
        ),
    ]

    dark_ratio = (
        float(
            np.mean(
                text_band < 105
            )
        )
        if text_band.size
        else 1.0
    )

    blue_ratio = float(
        np.mean(
            blue
        )
    )

    top_white_ratio = float(
        np.mean(
            top_white
        )
    )

    mean_v = float(
        np.mean(
            hsv[:, :, 2]
        )
    )

    metrics = {
        "blue": blue_ratio,
        "top_white": top_white_ratio,
        "dark": dark_ratio,
        "mean_v": mean_v,
    }

    # 真正“完全变蓝”的状态。
    # 用户录像：
    #   ready ≈ blue 0.62~0.73, top_white 0.08~0.20
    #   CD 1秒末 ≈ blue 0.55, top_white 0.23
    ready = (
        blue_ratio >= 0.58
        and top_white_ratio <= 0.25
        and dark_ratio <= 0.15
        and mean_v >= 175.0
    )

    if ready:
        return (
            "ready",
            metrics,
        )

    # 有按钮，但不是全蓝：区分CD和普通禁用状态。
    present = (
        mean_v >= 130.0
        and dark_ratio <= 0.40
        and (
            blue_ratio >= 0.08
            or top_white_ratio >= 0.20
        )
    )

    if present:
        # CD按钮有明显的蓝色“填充进度”；纯灰禁用按钮蓝色很少。
        if blue_ratio >= 0.16:
            return (
                "cooldown",
                metrics,
            )

        return (
            "disabled",
            metrics,
        )

    return (
        "absent",
        metrics,
    )


def _out_button_ready(
    image,
):
    state, _ = _out_button_state(
        image
    )

    return state == "ready"


def _wait_until_out_button_ready(
    hwnd,
    normal_timeout=None,
    cooldown_timeout=None,
):
    """
    只在按钮连续两帧为“全蓝ready”时返回成功。

    - 没看到CD：普通等待很短，避免错误选牌时白等；
    - 一旦看到CD：允许等待完整倒计时，再自动点击。
    """
    if normal_timeout is None:
        normal_timeout = float(
            CFG.get(
                "valid_button_poll_seconds",
                0.60,
            )
        )

    if cooldown_timeout is None:
        cooldown_timeout = float(
            CFG.get(
                "out_button_cooldown_wait_seconds",
                4.60,
            )
        )

    poll = float(
        CFG.get(
            "out_button_poll_interval",
            0.035,
        )
    )

    stable_required = max(
        1,
        int(
            CFG.get(
                "out_button_ready_stable_frames",
                2,
            )
        ),
    )

    started = time.monotonic()

    hard_deadline = (
        started
        + max(
            float(
                normal_timeout
            ),
            float(
                cooldown_timeout
            ),
        )
    )

    saw_cooldown = False
    ready_streak = 0
    last_state = "absent"
    last_image = None

    while (
        time.monotonic()
        < hard_deadline
    ):
        try:
            image, _ = capture(
                hwnd
            )
        except Exception:
            time.sleep(
                poll
            )
            continue

        last_image = image

        state, _ = _out_button_state(
            image
        )

        last_state = state

        if state == "ready":
            ready_streak += 1

            if (
                ready_streak
                >= stable_required
            ):
                return {
                    "ready": True,
                    "state": "ready",
                    "image": image,
                    "saw_cooldown": bool(
                        saw_cooldown
                    ),
                    "waited": (
                        time.monotonic()
                        - started
                    ),
                }
        else:
            ready_streak = 0

        if state == "cooldown":
            saw_cooldown = True

        elapsed = (
            time.monotonic()
            - started
        )

        # 普通禁用/不存在时，不要把“选牌错误”误当成CD白等4秒。
        if (
            not saw_cooldown
            and elapsed
            >= float(
                normal_timeout
            )
            and state
            in (
                "absent",
                "disabled",
            )
        ):
            break

        time.sleep(
            poll
        )

    return {
        "ready": False,
        "state": last_state,
        "image": last_image,
        "saw_cooldown": bool(
            saw_cooldown
        ),
        "waited": (
            time.monotonic()
            - started
        ),
    }


def _retry_submit_after_no_hand_change(
    hwnd,
    cards,
):
    """
    第一次点击后手牌没变化时：
      ready    -> 可能漏点，立即补点一次；
      cooldown -> 说明按钮还没好，等3/2/1结束、全蓝后再补点；
      absent   -> 视为游戏已接收，避免重复出牌。
    """
    try:
        verify_image, _ = capture(
            hwnd
        )
    except Exception:
        return (
            None,
            None,
        )

    state, _ = _out_button_state(
        verify_image
    )

    if state == "absent":
        return (
            None,
            "button_disappeared",
        )

    wait_result = None

    if state == "cooldown":
        log(
            "[出牌] 按钮CD中，等待倒计时结束"
        )

        wait_result = _wait_until_out_button_ready(
            hwnd,
            normal_timeout=0.35,
            cooldown_timeout=float(
                CFG.get(
                    "out_button_cooldown_wait_seconds",
                    4.60,
                )
            ),
        )

        if not wait_result[
            "ready"
        ]:
            return (
                None,
                None,
            )

        log(
            "[出牌] CD结束，按钮已全蓝，重新提交"
        )

    elif state == "ready":
        # 初次点击可能因为瞬时触控/画面时序没有真正落下。
        wait_result = {
            "ready": True,
        }

        log(
            "[出牌] 首次点击未生效，按钮仍可用，补点一次"
        )

    elif state == "disabled":
        # 很短的UI过渡，不按CD等待太久。
        wait_result = _wait_until_out_button_ready(
            hwnd,
            normal_timeout=0.80,
            cooldown_timeout=1.20,
        )

        if not wait_result[
            "ready"
        ]:
            return (
                None,
                None,
            )

    else:
        return (
            None,
            None,
        )

    click_rel(
        hwnd,
        0.50,
        0.947,
        wait=0.015,
    )

    old_timeout = CFG.get(
        "post_play_hand_change_timeout",
        PLAY_CONFIRM_TIMEOUT,
    )

    try:
        CFG[
            "post_play_hand_change_timeout"
        ] = max(
            float(
                PLAY_CONFIRM_RETRY_TIMEOUT
            ),
            0.55,
        )

        after_cards = (
            wait_for_post_play_hand_change(
                cards
            )
        )
    finally:
        CFG[
            "post_play_hand_change_timeout"
        ] = old_timeout

    if after_cards:
        return (
            after_cards,
            (
                "hand_changed_after_cd_retry"
                if state
                == "cooldown"
                else "hand_changed_after_retry"
            ),
        )

    try:
        verify2, _ = capture(
            hwnd
        )

        state2, _ = _out_button_state(
            verify2
        )

        if state2 == "absent":
            return (
                None,
                (
                    "button_disappeared_after_cd_retry"
                    if state
                    == "cooldown"
                    else "button_disappeared_after_retry"
                ),
            )
    except Exception:
        pass

    return (
        None,
        None,
    )


def _physical_card_candidates(
    cards,
    value,
):
    """
    Return distinct physical cards for one numeric value.

    A noisy edge detector can occasionally create two nearly-identical
    detections for the same physical card. Deduplicate by x-position first.
    """
    candidates = [
        card
        for card in cards
        if int(
            card["value"]
        ) == int(
            value
        )
    ]

    # Higher recognition confidence first; rightmost is a useful tie-break
    # because the visible corner is usually less occluded.
    candidates.sort(
        key=lambda card: (
            -float(
                card.get(
                    "score",
                    0.0,
                )
            ),
            -float(
                card.get(
                    "click_x",
                    0.0,
                )
            ),
        )
    )

    if not candidates:
        return []

    if cards:
        xs = [
            float(
                card.get(
                    "click_x",
                    0.0,
                )
            )
            for card in cards
        ]

        width_hint = max(
            xs
        ) if xs else 497.0
    else:
        width_hint = 497.0

    # Runtime window width is close to the largest x; use a conservative
    # minimum of 12px at ~497-wide UI.
    min_spacing = max(
        12.0,
        float(
            CFG.get(
                "physical_card_min_spacing_ratio",
                0.025,
            )
        )
        * max(
            497.0,
            width_hint,
        ),
    )

    distinct = []

    for card in candidates:
        x = float(
            card["click_x"]
        )

        if any(
            abs(
                x
                - float(
                    chosen["click_x"]
                )
            )
            < min_spacing
            for chosen in distinct
        ):
            continue

        distinct.append(
            card
        )

    return distinct


def resolve_play_indices(
    cards,
    play,
    attempt=0,
):
    """
    Convert a semantic legal pattern into exact physical card instances.

    Critical rule:
      straight 4-5-6-7-8 requires:
        {4:1, 5:1, 6:1, 7:1, 8:1}

    If there are two physical 4s, this function returns ONE of them,
    never both. A later retry may use the other 4, still exactly one.
    """
    requirements = play.get(
        "requirements"
    )

    if not requirements:
        requirements = dict(
            Counter(
                int(v)
                for v in play.get(
                    "values",
                    []
                )
            )
        )

    selected_cards = []

    for raw_value, raw_count in sorted(
        requirements.items()
    ):
        value = int(
            raw_value
        )

        count = int(
            raw_count
        )

        candidates = (
            _physical_card_candidates(
                cards,
                value,
            )
        )

        if len(
            candidates
        ) < count:
            return None

        # Build possible physical choices for this value.
        # At most 4 copies exist, so combinations stay tiny.
        variants = list(
            combinations(
                range(
                    len(
                        candidates
                    )
                ),
                count,
            )
        )

        if not variants:
            return None

        # Best-confidence variant first. Additional retry uses another
        # physical copy/combination when duplicates exist.
        variants.sort(
            key=lambda variant: -sum(
                float(
                    candidates[i].get(
                        "score",
                        0.0,
                    )
                )
                for i in variant
            )
        )

        variant = variants[
            attempt
            % len(
                variants
            )
        ]

        selected_cards.extend(
            candidates[i]
            for i in variant
        )

    # Exact-count safety check.
    selected_values = Counter(
        int(
            card["value"]
        )
        for card in selected_cards
    )

    required_counter = Counter({
        int(value): int(count)
        for value, count in requirements.items()
    })

    if selected_values != required_counter:
        return None

    indices = [
        int(
            card["index"]
        )
        for card in selected_cards
    ]

    if len(
        set(
            indices
        )
    ) != len(
        indices
    ):
        return None

    return indices


def _clear_any_selected_cards(
    hwnd,
    cards,
):
    """
    V1.7：
    正常每次出牌成功后游戏会自动清空选择。
    所以先看“出牌按钮”是否已激活：
      未激活 -> 直接返回，不再把8张牌全部点一遍
      已激活 -> 才执行旧的安全清除
    """
    if not cards:
        return True

    image, _ = capture(
        hwnd
    )

    if not _out_button_ready(
        image
    ):
        return True

    H, W = image.shape[:2]

    clear_y = (
        H
        * float(
            CFG.get(
                "selected_card_clear_y_ratio",
                0.662,
            )
        )
    )

    targets = sorted(
        cards,
        key=lambda card: -float(
            card["click_x"]
        )
    )

    for card in targets:
        click_client(
            hwnd,
            float(
                card["click_x"]
            ),
            clear_y,
            wait=float(
                CFG.get(
                    "clear_card_click_wait",
                    0.004,
                )
            ),
        )

    time.sleep(
        0.018
    )

    after, _ = capture(
        hwnd
    )

    return not _out_button_ready(
        after
    )



def _play_selection_description(
    cards,
    indices,
):
    chosen = [
        cards[i]
        for i in indices
    ]

    return ", ".join(
        f"{card['value']}@x{card['click_x']:.0f}"
        for card in chosen
    )


def click_card_indices(hwnd, image, cards, indices):
    H, W = image.shape[:2]

    targets = [
        cards[i]
        for i in indices
    ]

    # Right to left prevents a raised card from obscuring another visible corner.
    targets.sort(
        key=lambda c: -c["click_x"]
    )

    click_y = H * 0.780

    for card in targets:
        log(
            f"选择物理牌：{card['value']}"
            + ("(道具)" if card["special"] else "")
            + f" x={card['click_x']:.0f}"
            + f" score={card['score']:.3f}"
        )

        click_client(
            hwnd,
            card["click_x"],
            click_y,
            wait=float(
                CFG.get(
                    "card_click_wait",
                    0.045,
                )
            ),
        )


def _stable_hand_signature(
    cards,
):
    if not cards:
        return None

    return tuple(
        int(
            card["value"]
        )
        for card in cards
    )


def wait_for_post_play_hand_change(
    old_cards,
):
    """
    V8 event-driven continuation:
    成功点“出牌”后不再固定等待0.4~0.8秒。
    只要后台手牌线程确认新稳定手牌与旧手牌不同，就立即返回。
    """
    old_signature = (
        _stable_hand_signature(
            old_cards
        )
    )

    timeout = float(
        CFG.get(
            "post_play_hand_change_timeout",
            0.42,
        )
    )

    poll = float(
        CFG.get(
            "post_play_hand_change_poll",
            0.018,
        )
    )

    deadline = (
        time.monotonic()
        + timeout
    )

    while (
        time.monotonic()
        < deadline
    ):
        cards = (
            VISION_ENGINE.get_stable_hand(
                max_age=0.35
            )
        )

        if cards:
            signature = (
                _stable_hand_signature(
                    cards
                )
            )

            if (
                signature is not None
                and signature
                != old_signature
            ):
                return cards

        time.sleep(
            poll
        )

    return None


def execute_play(
    hwnd,
    cards,
    play,
):
    """
    V6 exact physical selection.

    A semantic legal combo can have multiple physical realizations:
      hand: 4,4,5,6,7,8
      straight 45678:
        attempt 0 -> first physical 4 + 5+6+7+8
        attempt 1 -> other physical 4 + 5+6+7+8

    Both attempts ALWAYS contain exactly one 4.
    """
    play_started_at = time.monotonic()

    before_count = len(
        cards
    )

    max_attempts = max(
        1,
        int(
            CFG.get(
                "physical_selection_retries",
                2,
            )
        ),
    )

    # Prevent background hand-recognition from learning the temporary
    # raised-card layout while we click.
    VISION_ENGINE.suppress_hand_for(
        float(
            CFG.get(
                "vision_suppress_after_action",
                0.85,
            )
        )
    )

    # Always clean any stale raised cards before a fresh attempt.
    _clear_any_selected_cards(
        hwnd,
        cards,
    )

    for attempt in range(
        max_attempts
    ):
        indices = resolve_play_indices(
            cards,
            play,
            attempt=attempt,
        )

        if indices is None:
            log(
                f"精确选牌失败：{play.get('name')} "
                "当前识别出的物理牌数量不足"
            )

            return False

        # Defensive: overwrite stale semantic indices with this exact
        # physical realization.
        attempt_play = dict(
            play
        )

        attempt_play[
            "indices"
        ] = list(
            indices
        )

        attempt_play[
            "values"
        ] = [
            int(
                cards[i][
                    "value"
                ]
            )
            for i in indices
        ]

        required = Counter({
            int(k): int(v)
            for k, v in (
                play.get(
                    "requirements",
                    Counter(
                        play.get(
                            "values",
                            []
                        )
                    ),
                )
            ).items()
        })

        actual = Counter(
            attempt_play[
                "values"
            ]
        )

        if actual != required:
            log(
                "精确选牌保护触发："
                f"要求={dict(required)}，"
                f"实际={dict(actual)}；"
                "本次不点击"
            )

            return False

        log(
            f"准备出牌：{play['name']}；"
            f"要求={dict(required)}；"
            f"物理方案{attempt + 1}/{max_attempts}："
            + _play_selection_description(
                cards,
                indices,
            )
        )

        image, _ = capture(
            hwnd
        )

        invalidate_hand_cache()

        click_card_indices(
            hwnd,
            image,
            cards,
            indices,
        )

        # V3.3：按钮可能正在3/2/1 CD。
        # 只有连续两帧“整体全蓝”才允许点击。
        button_wait = _wait_until_out_button_ready(
            hwnd,
            normal_timeout=float(
                CFG.get(
                    "valid_button_poll_seconds",
                    0.60,
                )
            ),
            cooldown_timeout=float(
                CFG.get(
                    "out_button_cooldown_wait_seconds",
                    4.60,
                )
            ),
        )

        selected_image = button_wait.get(
            "image"
        )

        ready = bool(
            button_wait.get(
                "ready",
                False,
            )
        )

        if (
            ready
            and button_wait.get(
                "saw_cooldown",
                False,
            )
        ):
            log(
                "[出牌] CD结束，按钮已全蓝"
            )

        if ready:
            selected_at = time.monotonic()

            log(
                f"出牌按钮已出现：{play['name']} "
                f"values={attempt_play['values']} "
                f"倍率={play['multiplier']:.2f}"
            )

            event_log(
                "play_button_ready",
                play=play.get("name"),
                values=list(attempt_play["values"]),
                attempt=int(attempt + 1),
                button_wait_ms=round(
                    (selected_at - play_started_at) * 1000.0,
                    2,
                ),
            )

            tap_started = time.monotonic()
            tap_ok = click_rel(
                hwnd,
                0.50,
                0.947,
                wait=float(
                    CFG.get(
                        "after_play_wait",
                        0.025,
                    )
                ),
            )
            tap_finished = time.monotonic()

            VISION_ENGINE.suppress_hand_for(
                float(
                    CFG.get(
                        "vision_suppress_after_action",
                        0.12,
                    )
                )
            )

            invalidate_hand_cache()

            # 第一次确认：手牌签名变化就是最强证据。
            after_cards = wait_for_post_play_hand_change(
                cards
            )

            confirmed_by = None

            if after_cards:
                confirmed_by = "hand_changed"
            else:
                (
                    retry_cards,
                    retry_confirmed_by,
                ) = _retry_submit_after_no_hand_change(
                    hwnd,
                    cards,
                )

                if retry_cards:
                    after_cards = retry_cards

                if retry_confirmed_by:
                    confirmed_by = retry_confirmed_by

            if confirmed_by is None:
                log(
                    "[出牌校验] 无法确认本次出牌已提交；"
                    "不更新牌库，尝试重新识别当前画面"
                )
                event_log(
                    "play_unconfirmed",
                    play=play.get("name"),
                    values=list(attempt_play["values"]),
                    tap_ok=bool(tap_ok),
                )

                try:
                    current_image, _ = capture(
                        hwnd
                    )
                    save_debug(
                        current_image,
                        "play_unconfirmed",
                    )
                except Exception:
                    pass

                invalidate_hand_cache()
                return False

            # 只有得到视觉确认后才从40张牌库扣牌。
            deck_mark_successful_play(
                attempt_play[
                    "values"
                ]
            )

            if after_cards:
                log(
                    f"[出牌校验] 成功({confirmed_by})；"
                    f"手牌数 {before_count} -> {len(after_cards)}"
                )
            else:
                log(
                    f"[出牌校验] 成功({confirmed_by})；"
                    "稳定手牌尚未刷新"
                )

            play[
                "indices"
            ] = list(
                indices
            )
            play[
                "values"
            ] = list(
                attempt_play[
                    "values"
                ]
            )

            total_ms = (
                time.monotonic()
                - play_started_at
            ) * 1000.0

            event_log(
                "play_confirmed",
                play=play.get("name"),
                values=list(attempt_play["values"]),
                confirmed_by=confirmed_by,
                before_count=int(before_count),
                after_count=(
                    len(after_cards)
                    if after_cards
                    else None
                ),
                tap_ok=bool(tap_ok),
                out_tap_ms=round(
                    (tap_finished - tap_started) * 1000.0,
                    2,
                ),
                total_ms=round(total_ms, 2),
            )

            log(
                f"[极速出牌] 本次执行+确认总耗时={total_ms:.0f}ms；"
                f"确认={confirmed_by}"
            )

            return True

        # IMPORTANT:
        # This pattern is legal by the confirmed rules. A missing button here
        # can be a bad physical click or visual mapping, not proof that the
        # semantic straight/pair itself is illegal.
        log(
            f"物理选牌方案{attempt + 1}没有出现出牌按钮；"
            "先安全清空已抬起的牌"
        )

        if selected_image is not None:
            save_debug(
                selected_image,
                f"physical_selection_failed_{attempt+1}",
            )

        _clear_any_selected_cards(
            hwnd,
            cards,
        )

        invalidate_hand_cache()

    # Avoid an infinite loop on this exact semantic combo in the same visible hand.
    signature = (
        play_context_signature(
            cards,
            play,
        )
    )

    ROUND_INVALID_PLAYS.add(
        signature
    )

    log(
        f"本手牌下 {play['name']} 的物理选牌方案均未成功，"
        "本轮暂时跳过这个组合，改试下一种合法牌型"
    )

    return False



# ------------------------------------------------------------------
# V7 asynchronous vision engine
# ------------------------------------------------------------------

def _v25_rank_resources_available():
    if REALTIME_RANK_TEMPLATES:
        return True

    widget = globals().get(
        "REALTIME_RANK_WIDGET"
    )

    if (
        widget is None
        or getattr(
            widget,
            "size",
            0,
        )
        <= 0
    ):
        return False

    try:
        h, w = widget.shape[
            :2
        ]

        # 1x1透明兼容占位图不算资源。
        return (
            h > 12
            and w > 12
            and float(
                np.std(
                    widget
                )
            )
            > 1.0
        )
    except Exception:
        return False




class AsyncVisionEngine:
    def __init__(self):
        self.hwnd = None

        self._stop = (
            threading.Event()
        )

        self._lock = (
            threading.RLock()
        )

        # V2.0：新帧事件。点击后的主线程直接等待下一张DXGI帧，
        # 不再每25ms轮询一次。
        self._frame_condition = threading.Condition(
            self._lock
        )

        self._threads = []

        self._frame = None
        self._frame_box = None
        self._frame_time = 0.0
        self._frame_seq = 0
        self._min_valid_frame_seq = 0
        self._capture_error_count = 0
        self._capture_consecutive_errors = 0
        self._last_capture_error_log = 0.0

        # V1.8.2：供主线程判断当前是实时H264还是兼容screencap。
        self.capture_backend = "starting"
        self._frame_times = deque(maxlen=180)
        self._hand_frames_seen = 0
        self._hand_valid_frames = 0
        self._hand_stable_updates = 0
        self._hand_last_raw_signature = None
        self._hand_last_raw_log = 0.0
        self._hand_last_stable_event_signature = None
        self._hand_last_error = None

        self._current_limit = None

        self._stable_cards = None
        self._stable_hand_time = 0.0
        self._hand_history = deque(
            maxlen=max(
                3,
                int(
                    CFG.get(
                        "hand_consensus_window",
                        3,
                    )
                ),
            )
        )

        self._rank = None
        self._rank_score = 0.0
        self._rank_time = 0.0

        self._suppress_hand_until = 0.0

    def stop(self):
        self._stop.set()

        for thread in list(
            self._threads
        ):
            try:
                thread.join(
                    timeout=0.35
                )
            except Exception:
                pass

        self._threads = []

        self._stop = (
            threading.Event()
        )

    def start(
        self,
        hwnd,
    ):
        if (
            self.hwnd == hwnd
            and self._threads
            and any(
                thread.is_alive()
                for thread in self._threads
            )
        ):
            return

        self.stop()

        self.hwnd = hwnd

        with self._lock:
            self._frame = None
            self._frame_box = None
            self._frame_time = 0.0
            self._frame_seq = 0
            self._min_valid_frame_seq = 0
            self._stable_cards = None
            self._stable_hand_time = 0.0
            self._hand_history.clear()
            self._rank = None
            self._rank_score = 0.0
            self._rank_time = 0.0
            self._frame_times.clear()
            self._hand_frames_seen = 0
            self._hand_valid_frames = 0
            self._hand_stable_updates = 0
            self._hand_last_raw_signature = None
            self._hand_last_raw_log = 0.0
            self._hand_last_error = None

        if not int(
            CFG.get(
                "async_vision_enabled",
                1,
            )
        ):
            return

        threads = [
            threading.Thread(
                target=self._capture_loop,
                name="RaceCapture",
                daemon=True,
            ),
        ]

        enabled_names = [
            "截图线程",
        ]

        if DIGIT_BINARY:
            threads.append(
                threading.Thread(
                    target=self._hand_loop,
                    name="RaceHandRecognition",
                    daemon=True,
                )
            )
            enabled_names.append(
                "手牌识别线程"
            )

        if _v25_rank_resources_available():
            threads.append(
                threading.Thread(
                    target=self._rank_loop,
                    name="RaceRankRecognition",
                    daemon=True,
                )
            )
            enabled_names.append(
                "排名识别线程"
            )

        self._threads = threads

        for thread in self._threads:
            thread.start()

        log(
            "[V2.5线程减负] 已启动："
            + " + ".join(
                enabled_names
            )
        )

        if not DIGIT_BINARY:
            log(
                "[V2.5线程减负] 手牌模板为空，暂不启动手牌线程"
            )

        if not _v25_rank_resources_available():
            log(
                "[V2.5线程减负] 排名资源为空，暂不启动排名线程"
            )

    def set_current_limit(
        self,
        hand_limit,
    ):
        with self._lock:
            self._current_limit = (
                None
                if hand_limit is None
                else int(
                    hand_limit
                )
            )

    def suppress_hand_for(
        self,
        seconds,
    ):
        with self._lock:
            self._suppress_hand_until = max(
                self._suppress_hand_until,
                time.monotonic()
                + float(
                    seconds
                ),
            )

    def invalidate_after_action(
        self,
    ):
        """
        点击之后要求下一次读取必须来自“点击后的新截图”。

        不删除旧帧，也不让主线程自己再开一个 screencap；
        只记录需要的最小 frame_seq。
        """
        with self._lock:
            self._min_valid_frame_seq = max(
                self._min_valid_frame_seq,
                self._frame_seq + 1,
            )


    def wait_for_frame(
        self,
        timeout=ADB_CAPTURE_WAIT_TIMEOUT,
        max_age=ADB_CAPTURE_MAX_STALE_AGE,
    ):
        """
        V2.0：Condition事件驱动等待新帧。
        正常60FPS时点击后约16ms即可拿到新画面。
        """
        deadline = (
            time.monotonic()
            + float(
                timeout
            )
        )

        with self._frame_condition:
            while True:
                if (
                    self._frame is not None
                    and self._frame_seq
                    >= self._min_valid_frame_seq
                ):
                    age = (
                        time.monotonic()
                        - self._frame_time
                    )

                    if age <= float(
                        max_age
                    ):
                        return (
                            self._frame.copy(),
                            tuple(
                                self._frame_box
                            ),
                        )

                remaining = (
                    deadline
                    - time.monotonic()
                )

                if remaining <= 0:
                    return None

                self._frame_condition.wait(
                    timeout=min(
                        remaining,
                        0.10,
                    )
                )


    def wait_for_new_frame(
        self,
        timeout=0.15,
        max_age=0.12,
    ):
        """
        V2.1：等待“比调用时更新”的一张DXGI帧。
        用于按钮两帧确认。
        """
        deadline = (
            time.monotonic()
            + float(
                timeout
            )
        )

        with self._frame_condition:
            start_seq = int(
                self._frame_seq
            )

            while True:
                if (
                    self._frame is not None
                    and self._frame_seq
                    > start_seq
                    and self._frame_seq
                    >= self._min_valid_frame_seq
                ):
                    age = (
                        time.monotonic()
                        - self._frame_time
                    )

                    if age <= float(
                        max_age
                    ):
                        return (
                            self._frame.copy(),
                            tuple(
                                self._frame_box
                            ),
                        )

                remaining = (
                    deadline
                    - time.monotonic()
                )

                if remaining <= 0:
                    return None

                self._frame_condition.wait(
                    timeout=min(
                        remaining,
                        0.05,
                    )
                )


    def get_frame(
        self,
        max_age=0.20,
    ):
        with self._lock:
            if (
                self._frame is None
                or self._frame_seq
                < self._min_valid_frame_seq
            ):
                return None

            age = (
                time.monotonic()
                - self._frame_time
            )

            if age > float(
                max_age
            ):
                return None

            return (
                self._frame.copy(),
                tuple(
                    self._frame_box
                ),
            )

    def _get_frame_for_worker(
        self,
    ):
        with self._lock:
            if self._frame is None:
                return (
                    None,
                    -1,
                )

            return (
                self._frame.copy(),
                int(
                    self._frame_seq
                ),
            )

    def get_stable_hand(
        self,
        max_age=None,
    ):
        if max_age is None:
            max_age = float(
                CFG.get(
                    "stable_hand_max_age",
                    0.75,
                )
            )

        with self._lock:
            if self._stable_cards is None:
                return None

            if (
                time.monotonic()
                - self._stable_hand_time
                > float(
                    max_age
                )
            ):
                return None

            return [
                dict(
                    card
                )
                for card in self._stable_cards
            ]

    def get_rank(
        self,
        max_age=0.9,
    ):
        with self._lock:
            if self._rank is None:
                return (
                    None,
                    0.0,
                )

            if (
                time.monotonic()
                - self._rank_time
                > float(
                    max_age
                )
            ):
                return (
                    None,
                    0.0,
                )

            return (
                int(
                    self._rank
                ),
                float(
                    self._rank_score
                ),
            )

    def diagnostic_snapshot(
        self,
    ):
        now = time.monotonic()

        with self._lock:
            frame_age = (
                None
                if self._frame is None
                else now - self._frame_time
            )
            hand_age = (
                None
                if self._stable_cards is None
                else now - self._stable_hand_time
            )
            rank_age = (
                None
                if self._rank is None
                else now - self._rank_time
            )

            times = list(
                self._frame_times
            )

            if len(times) >= 2:
                span = max(
                    0.001,
                    times[-1] - times[0],
                )
                fps = (
                    len(times) - 1
                ) / span
            else:
                fps = 0.0

            return {
                "frame_seq": int(self._frame_seq),
                "frame_age": frame_age,
                "fps": float(fps),
                "stable_hand_count": (
                    0
                    if self._stable_cards is None
                    else len(self._stable_cards)
                ),
                "hand_age": hand_age,
                "hand_frames_seen": int(self._hand_frames_seen),
                "hand_valid_frames": int(self._hand_valid_frames),
                "hand_stable_updates": int(self._hand_stable_updates),
                "hand_last_error": self._hand_last_error,
                "rank": self._rank,
                "rank_age": rank_age,
                "capture_errors": int(self._capture_error_count),
            }


    def _publish_frame(
        self,
        frame,
        box,
    ):
        now = time.monotonic()

        with self._frame_condition:
            self._frame = frame
            self._frame_box = box
            self._frame_time = now
            self._frame_seq += 1
            self._frame_times.append(
                now
            )
            self._frame_condition.notify_all()


    def _capture_loop(
        self,
    ):
        """
        V1.9实时视觉生产者：
          scrcpy窗口 -> DXGI -> RGB ndarray -> 游戏视口

        永远只保存最新帧，不排队处理旧画面。
        """
        self.capture_backend = (
            "scrcpy_dxgi"
        )

        target_interval = (
            1.0
            / max(
                1.0,
                DXGI_CAPTURE_FPS,
            )
        )

        frames_seen = 0
        started = time.monotonic()
        last_fps_log = started
        next_tick = started
        consecutive_errors = 0

        log(
            "[实时视觉] scrcpy + DXGI采集已启动；"
            f"目标={DXGI_CAPTURE_FPS:.0f} FPS"
        )

        event_log(
            "capture_backend",
            backend="scrcpy_dxgi",
            target_fps=DXGI_CAPTURE_FPS,
        )

        while not self._stop.is_set():
            loop_started = time.monotonic()

            try:
                if self.hwnd is None:
                    time.sleep(0.05)
                    continue

                frame, box = capture_direct(
                    self.hwnd
                )

                if not dxgi_frame_ready(frame):
                    raise RuntimeError("DXGI帧校验失败，跳过空帧")

                self._publish_frame(
                    frame,
                    box,
                )

                frames_seen += 1
                consecutive_errors = 0
                self._capture_consecutive_errors = 0

                now = time.monotonic()

                if (
                    now - last_fps_log
                    >= FAST_STREAM_FPS_LOG_INTERVAL
                ):
                    elapsed = max(
                        0.001,
                        now - started,
                    )

                    fps_value = (
                        frames_seen
                        / elapsed
                    )

                    diag = self.diagnostic_snapshot()

                    log(
                        f"[实时视觉] DXGI≈{fps_value:.1f} FPS；"
                        f"有效FPS={diag['fps']:.1f}；"
                        f"游戏帧={frame.shape[1]}x{frame.shape[0]}；"
                        if dxgi_frame_ready(frame)
                        else "游戏帧=invalid；"
                        f"seq={diag['frame_seq']}"
                    )

                    event_log(
                        "dxgi_fps",
                        fps=float(
                            fps_value
                        ),
                        effective_fps=float(
                            diag["fps"]
                        ),
                        frames=int(
                            frames_seen
                        ),
                        width=int(
                            frame.shape[1]
                        ),
                        height=int(
                            frame.shape[0]
                        ),
                    )

                    last_fps_log = now

            except Exception as exc:
                consecutive_errors += 1
                self._capture_error_count += 1
                self._capture_consecutive_errors = (
                    consecutive_errors
                )

                if (
                    consecutive_errors == 5
                    or consecutive_errors % 20 == 0
                ):
                    log(
                        "[实时视觉] DXGI抓帧失败："
                        f"{type(exc).__name__}: {exc}；"
                        f"连续={consecutive_errors}"
                    )

                    event_log(
                        "dxgi_capture_error",
                        error_type=type(
                            exc
                        ).__name__,
                        error=str(
                            exc
                        ),
                        consecutive=consecutive_errors,
                    )

                # 窗口可能被关闭/重开。刷新专用scrcpy窗口句柄。
                if consecutive_errors >= 8:
                    try:
                        item = (
                            SCRCPY_MANAGER.ensure_window()
                        )

                        if item is not None:
                            self.hwnd = int(
                                item["hwnd"]
                            )
                            SCRCPY_CAPTURE._refresh_region(
                                self.hwnd,
                                force=True,
                            )
                            consecutive_errors = 0
                    except Exception:
                        pass

                time.sleep(
                    min(
                        0.20,
                        0.015
                        * max(
                            1,
                            consecutive_errors,
                        ),
                    )
                )

            next_tick += target_interval

            delay = (
                next_tick
                - time.monotonic()
            )

            if delay > 0:
                time.sleep(
                    delay
                )
            elif (
                time.monotonic()
                - next_tick
                > 0.20
            ):
                next_tick = (
                    time.monotonic()
                )

    def _hand_is_plausible(
        self,
        cards,
    ):
        if not cards:
            return False

        values = [
            int(
                card["value"]
            )
            for card in cards
        ]

        counts = Counter(
            values
        )

        if any(
            count > 4
            for count in counts.values()
        ):
            return False

        with self._lock:
            limit = (
                self._current_limit
            )

        if limit is not None:
            max_allowed = (
                int(
                    limit
                )
                + int(
                    CFG.get(
                        "allowed_over_capacity",
                        1,
                    )
                )
            )

            if len(
                cards
            ) > max_allowed:
                return False

        return (
            1
            <= len(
                cards
            )
            <= 10
        )

    def _hand_loop(
        self,
    ):
        fps = max(
            2.0,
            float(
                CFG.get(
                    "hand_worker_fps",
                    12.0,
                )
            ),
        )

        interval = (
            1.0
            / fps
        )

        last_seq = -1

        while not self._stop.is_set():
            started = (
                time.monotonic()
            )

            try:
                with self._lock:
                    suppress = (
                        time.monotonic()
                        < self._suppress_hand_until
                    )

                if suppress:
                    time.sleep(
                        min(
                            0.04,
                            interval,
                        )
                    )
                    continue

                frame, seq = (
                    self._get_frame_for_worker()
                )

                if (
                    frame is None
                    or seq == last_seq
                ):
                    time.sleep(
                        0.01
                    )
                    continue

                last_seq = seq

                with self._lock:
                    self._hand_frames_seen += 1

                cards = read_hand_fast(
                    frame
                )

                if not self._hand_is_plausible(
                    cards
                ):
                    continue

                with self._lock:
                    self._hand_valid_frames += 1

                signature = tuple(
                    int(
                        card["value"]
                    )
                    for card in cards
                )

                now = (
                    time.monotonic()
                )

                if (
                    signature != self._hand_last_raw_signature
                    or now - self._hand_last_raw_log
                    >= 1.0
                ):
                    scores = [
                        float(card.get("score", 0.0))
                        for card in cards
                    ]
                    event_log(
                        "hand_raw",
                        frame_seq=int(seq),
                        values=list(signature),
                        count=len(cards),
                        avg_score=(
                            round(float(np.mean(scores)), 4)
                            if scores else 0.0
                        ),
                        min_score=(
                            round(float(min(scores)), 4)
                            if scores else 0.0
                        ),
                        xs=[round(float(card.get("click_x", 0.0)), 1) for card in cards],
                    )
                    self._hand_last_raw_signature = signature
                    self._hand_last_raw_log = now

                with self._lock:
                    self._hand_history.append(
                        (
                            signature,
                            [
                                dict(
                                    card
                                )
                                for card in cards
                            ],
                            now,
                        )
                    )

                    required = max(
                        1,
                        int(
                            CFG.get(
                                "hand_consensus_required",
                                2,
                            )
                        ),
                    )

                    counts = Counter(
                        item[0]
                        for item in self._hand_history
                    )

                    best_sig, best_count = (
                        counts.most_common(
                            1
                        )[0]
                    )

                    if (
                        best_count
                        >= required
                    ):
                        # Use newest physical coordinates for that consensus.
                        for (
                            hist_sig,
                            hist_cards,
                            hist_time,
                        ) in reversed(
                            self._hand_history
                        ):
                            if (
                                hist_sig
                                == best_sig
                            ):
                                self._stable_cards = [
                                    dict(
                                        card
                                    )
                                    for card in hist_cards
                                ]

                                self._stable_hand_time = (
                                    hist_time
                                )
                                self._hand_stable_updates += 1

                                if (
                                    best_sig
                                    != self._hand_last_stable_event_signature
                                ):
                                    event_log(
                                        "hand_stable",
                                        frame_seq=int(seq),
                                        values=list(best_sig),
                                        count=len(hist_cards),
                                        stable_updates=int(self._hand_stable_updates),
                                    )
                                    self._hand_last_stable_event_signature = (
                                        best_sig
                                    )

                                break

            except Exception as exc:
                with self._lock:
                    self._hand_last_error = (
                        f"{type(exc).__name__}: {exc}"
                    )
                event_log(
                    "hand_worker_error",
                    error_type=type(exc).__name__,
                    error=str(exc),
                )

            elapsed = (
                time.monotonic()
                - started
            )

            time.sleep(
                max(
                    0.001,
                    interval
                    - elapsed,
                )
            )

    def _rank_loop(
        self,
    ):
        fps = max(
            1.0,
            float(
                CFG.get(
                    "rank_worker_fps",
                    8.0,
                )
            ),
        )

        interval = (
            1.0
            / fps
        )

        last_seq = -1

        while not self._stop.is_set():
            started = (
                time.monotonic()
            )

            try:
                frame, seq = (
                    self._get_frame_for_worker()
                )

                if (
                    frame is None
                    or seq == last_seq
                ):
                    time.sleep(
                        0.01
                    )
                    continue

                last_seq = seq

                rank, score = (
                    detect_realtime_rank(
                        frame
                    )
                )

                if rank is not None:
                    with self._lock:
                        self._rank = (
                            rank
                        )
                        self._rank_score = (
                            score
                        )
                        self._rank_time = (
                            time.monotonic()
                        )

            except Exception:
                pass

            elapsed = (
                time.monotonic()
                - started
            )

            time.sleep(
                max(
                    0.001,
                    interval
                    - elapsed,
                )
            )


VISION_ENGINE = AsyncVisionEngine()

# ------------------------------------------------------------------
# Main loop
# ------------------------------------------------------------------

# ============================================================
# V2.3 TRUE PHONE VIEWPORT CROP
# ============================================================

def _v23_crop_phone_viewport(
    self,
    image,
):
    """
    scrcpy 的 Windows client 有时是横向 1200x623，
    但真实手机画面仍是中间的竖屏视口。

    例如：
        client = 1200x623
        Android = 1264x2780
        Android aspect = 0.4547

    真实手机视口应约：
        283x623
        x ≈ (1200-283)/2 ≈ 458

    识别、点击坐标都必须基于这个真实视口，而不是1200x623宿主client。
    """
    if (
        image is None
        or getattr(
            image,
            "size",
            0,
        )
        <= 0
    ):
        return image

    H, W = image.shape[
        :2
    ]

    device_w, device_h = getattr(
        self,
        "_input_size",
        (
            1264,
            2780,
        ),
    )

    device_w = float(
        device_w
    )
    device_h = float(
        device_h
    )

    if (
        device_w <= 0
        or device_h <= 0
    ):
        self.last_viewport = (
            0,
            0,
            W,
            H,
        )
        return image

    device_aspect = (
        device_w
        / device_h
    )

    client_aspect = (
        float(W)
        / float(
            max(
                H,
                1,
            )
        )
    )

    # 宿主比手机“宽”：左右黑边/桌面背景，按高度确定真实宽度。
    if client_aspect > device_aspect * 1.05:
        target_h = H
        target_w = int(
            round(
                H
                * device_aspect
            )
        )

        target_w = max(
            2,
            min(
                W,
                target_w,
            ),
        )

        x0 = int(
            round(
                (
                    W
                    - target_w
                )
                / 2.0
            )
        )
        y0 = 0

    # 宿主比手机“窄”：上下黑边，按宽度确定真实高度。
    elif client_aspect < device_aspect / 1.05:
        target_w = W
        target_h = int(
            round(
                W
                / device_aspect
            )
        )

        target_h = max(
            2,
            min(
                H,
                target_h,
            ),
        )

        x0 = 0
        y0 = int(
            round(
                (
                    H
                    - target_h
                )
                / 2.0
            )
        )

    else:
        x0 = 0
        y0 = 0
        target_w = W
        target_h = H

    x1 = min(
        W,
        x0
        + target_w,
    )
    y1 = min(
        H,
        y0
        + target_h,
    )

    cropped = image[
        y0:y1,
        x0:x1,
    ]

    if (
        cropped is None
        or cropped.size == 0
    ):
        self.last_viewport = (
            0,
            0,
            W,
            H,
        )
        return image

    # 注意：
    # 后续识别坐标是相对于 cropped 的，所以 mapping 只需要 viewport宽高。
    self.last_viewport = (
        int(
            x0
        ),
        int(
            y0
        ),
        int(
            cropped.shape[
                1
            ]
        ),
        int(
            cropped.shape[
                0
            ]
        ),
    )

    return np.ascontiguousarray(
        cropped
    )


AndroidADB._crop_viewport = (
    _v23_crop_phone_viewport
)


# ============================================================
# V2.0 REALTIME CORE FINAL OVERRIDES
# ============================================================

_FAST_EVENT_QUEUE = queue.Queue(
    maxsize=4096
)
_FAST_EVENT_WRITER_STARTED = False
_FAST_EVENT_WRITER_LOCK = threading.Lock()


def _fast_event_writer_loop():
    return


def _ensure_fast_event_writer():
    return


def event_log(event, **kwargs):
    """The later compatibility definition uses the same session journal."""
    try:
        with _LOG_LOCK:
            _session_paths()
            payload = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"),
                       "event": str(event), **kwargs}
            with SESSION_EVENTS_PATH.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def _v20_adb_ensure_shell(self):
    proc = getattr(
        self,
        "_fast_shell",
        None,
    )

    if (
        proc is not None
        and proc.poll() is None
        and proc.stdin is not None
    ):
        return proc

    self._fast_shell = subprocess.Popen(
        [
            ADB_PATH,
            "-s",
            self.serial,
            "shell",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )

    return self._fast_shell


def _v20_adb_close_shell(self):
    proc = getattr(
        self,
        "_fast_shell",
        None,
    )
    self._fast_shell = None

    if proc is None:
        return

    try:
        if proc.stdin:
            proc.stdin.close()
    except Exception:
        pass

    try:
        proc.terminate()
    except Exception:
        pass


def _v20_adb_tap(self, x, y):
    """
    持久 adb shell。
    旧版每点一张牌都 subprocess.run('adb shell input tap ...')，
    Windows上单击就可能耗几十~上百ms。
    """
    x = int(
        round(
            float(x)
        )
    )
    y = int(
        round(
            float(y)
        )
    )

    for attempt in range(2):
        try:
            proc = _v20_adb_ensure_shell(
                self
            )
            proc.stdin.write(
                f"input tap {x} {y}\n"
            )
            proc.stdin.flush()
            return True

        except Exception:
            _v20_adb_close_shell(
                self
            )

            if attempt:
                return False

    return False


def _v20_viewport_to_device(self, x, y):
    """
    scrcpy图像坐标 -> Android input坐标。
    旧兼容层是恒等映射，在1080高度scrcpy窗口上会映射错误。
    """
    try:
        _, _, vw, vh = (
            self.last_viewport
            or (
                0,
                0,
                0,
                0,
            )
        )

        dw, dh = getattr(
            self,
            "_input_size",
            (
                1080,
                2376,
            ),
        )

        vw = float(vw)
        vh = float(vh)
        dw = float(dw)
        dh = float(dh)

        if (
            vw <= 0
            or vh <= 0
            or dw <= 0
            or dh <= 0
        ):
            return (
                float(x),
                float(y),
            )

        return (
            float(x)
            * dw
            / vw,
            float(y)
            * dh
            / vh,
        )

    except Exception:
        return (
            float(x),
            float(y),
        )


def _v20_device_to_viewport(self, x, y):
    try:
        _, _, vw, vh = (
            self.last_viewport
            or (
                0,
                0,
                0,
                0,
            )
        )

        dw, dh = getattr(
            self,
            "_input_size",
            (
                1080,
                2376,
            ),
        )

        if (
            vw <= 0
            or vh <= 0
            or dw <= 0
            or dh <= 0
        ):
            return (
                float(x),
                float(y),
            )

        return (
            float(x)
            * float(vw)
            / float(dw),
            float(y)
            * float(vh)
            / float(dh),
        )

    except Exception:
        return (
            float(x),
            float(y),
        )


AndroidADB._ensure_fast_shell = (
    _v20_adb_ensure_shell
)
AndroidADB.close_shell = (
    _v20_adb_close_shell
)
AndroidADB.tap = (
    _v20_adb_tap
)
AndroidADB.viewport_to_device = (
    _v20_viewport_to_device
)
AndroidADB.device_to_viewport = (
    _v20_device_to_viewport
)


def _v20_cleanup_fast_io():
    try:
        if globals().get(
            "ADB_DEVICE"
        ) is not None:
            ADB_DEVICE.close_shell()
    except Exception:
        pass

    try:
        _FAST_EVENT_QUEUE.put_nowait(
            None
        )
    except Exception:
        pass


atexit.register(
    _v20_cleanup_fast_io
)



def main():
    log("[版本] 田忌赛马 Android ADB V3.3（动态选马 + 出牌CD识别 + 自动重试）")
    print(
        "田忌赛马 V3.3 已启动："
        "匹配 → 选马 → 等待发牌 → 自动出牌 → 结算",
        flush=True,
    )

    global ADB_DEVICE

    try:
        serial = discover_adb_device()
        ADB_DEVICE = AndroidADB(
            serial
        )

        ADB_DEVICE.device_input_size = (
            ADB_DEVICE.input_size()
        )

        model = ADB_DEVICE.getprop(
            "ro.product.model"
        )
        android_version = ADB_DEVICE.getprop(
            "ro.build.version.release"
        )

        log(
            f"已连接Android：serial={serial}，"
            f"model={model or '<未知>'}，"
            f"Android={android_version or '<未知>'}"
        )

        wm_size_text = (
            ADB_DEVICE.wm_size()
            or "<未知>"
        )

        # wm_size() 会刷新 _input_size；同步诊断字段，避免仍显示旧的1080x2400。
        ADB_DEVICE.device_input_size = tuple(
            getattr(
                ADB_DEVICE,
                "_input_size",
                (
                    1080,
                    2400,
                ),
            )
        )

        wm_source = getattr(
            ADB_DEVICE,
            "_wm_size_source",
            "unknown",
        )

        log(
            "Android输入坐标："
            f"{ADB_DEVICE.device_input_size[0]}x"
            f"{ADB_DEVICE.device_input_size[1]}"
            f"（{wm_source}）"
        )

        if not SCRCPY_REALTIME_ENABLED:
            raise RuntimeError(
                "V1.9要求scrcpy实时视觉，HORSE_SCRCPY_REALTIME不能关闭"
            )

        if not DXCAM_AVAILABLE:
            raise RuntimeError(
                "缺少dxcam；请执行：python -m pip install dxcam"
            )

        log(
            f"[V3.3] scrcpy实时视觉已启用："
            f"max-size={SCRCPY_MAX_SIZE}，"
            f"max-fps={SCRCPY_MAX_FPS}，"
            f"bit-rate={SCRCPY_VIDEO_BITRATE}；"
            "比赛阶段不再调用adb screencap"
        )

    except Exception as exc:
        log(
            f"ADB初始化失败：{exc}"
        )
        return

    hwnd = None
    current_horse = None
    current_limit = None
    race_started_at = None
    last_play_at = 0.0
    last_hand_log = None
    matches_completed = 0
    matches_started = 0
    waiting_result_flow = False
    reward_click_attempts = 0
    reward_pending_at = 0.0
    reward_lobby_confirmations = 0

    # 1,2,3 across a complete match.
    current_round_index = 0

    current_realtime_rank = None
    last_rank_check_at = 0.0

    # V1.5 页面状态机：
    # auto -> lobby -> wait_horse -> wait_race -> race
    # race_end 后回 wait_horse；完整三轮奖励后回 lobby。
    screen_phase = "auto"
    phase_since = time.monotonic()
    horse_selected_at = 0.0
    wait_race_last_diag = 0.0
    wait_race_last_debug_save = 0.0
    last_no_play_log = 0.0
    last_diag_heartbeat = 0.0
    capture_recovery_failures = 0

    if learning_enabled():
        log(
            "AI学习已开启："
            f"历史累计 {LEARNING_DB.get('matches_learned',0)} 场"
        )

    log(
        "[状态机V3.3] 大厅→选马→等待发牌→比赛→结算；"
        "选完马后不会再被大厅/选马误识别抢走控制权"
    )

    log(
        "[实时视觉V3.3] scrcpy窗口由DXGI实时抓取；"
        "手牌/排名线程共享最新帧，不再调用screenrecord/screencap"
    )

    log(
        "[手牌V3.3] 双帧一致识别；出牌按钮支持CD倒计时等待"
    )

    log(
        "[V2.5配置] "
        f"scan_interval={CFG.get('scan_interval', 0.010):.3f}s；"
        f"aggressive_after={CFG.get('aggressive_after_seconds', 8.0):.1f}s；"
        f"emergency_after={CFG.get('emergency_after_seconds', 14.0):.1f}s"
    )

    log(
        "[V2.4识别资源] "
        f"比赛数字模板={len(DIGIT_BINARY)}组；"
        f"选马数字模板={len(SELECTION_DIGIT_SAMPLES)}组；"
        f"排名模板={len(REALTIME_RANK_TEMPLATES)}组；"
        f"页面模板={len(SCREEN_TEMPLATES)}组"
    )

    if not DIGIT_BINARY:
        log(
            "[V2.4保护] 比赛数字模板为空；"
            "已禁用空模板手牌分类，避免max()异常与无意义CPU占用"
        )



    while True:
        try:
            active_hwnd = VISION_ENGINE.hwnd
            if hwnd is not None and active_hwnd is not None and active_hwnd != hwnd:
                log(f"[scrcpy恢复] 主循环句柄 {hwnd} -> 抓帧句柄 {active_hwnd}")
                hwnd = active_hwnd
                VISION_ENGINE.invalidate_after_action()
                invalidate_hand_cache()
            if (
                hwnd is None
            ):
                item = find_game_window()

                if item is None:
                    log("暂未找到田忌赛马小游戏窗口")
                    time.sleep(1.2)
                    continue

                hwnd = item["hwnd"]

                log(
                    "[V2.7窗口锁定] "
                    f"title={item['title']}；"
                    f"class={item.get('class','?')}；"
                    f"pid={item.get('pid','?')}；"
                    f"client={item['w']}x{item['h']}；"
                    "已排除ConsoleWindowClass"
                )

                VISION_ENGINE.start(
                    hwnd
                )

                time.sleep(0.22)

            image, _ = capture(hwnd)
            capture_recovery_failures = 0
            H, W = image.shape[:2]

            if reward_pending_at:
                if is_lobby(image):
                    reward_lobby_confirmations += 1
                else:
                    reward_lobby_confirmations = 0
                if reward_lobby_confirmations >= 2:
                    finish_race_report()
                    matches_completed += 1
                    reward_click_attempts = 0
                    reward_pending_at = 0.0
                    reward_lobby_confirmations = 0
                    screen_phase = "lobby"
                    phase_since = time.monotonic()
                    current_round_index = 0
                    current_realtime_rank = None
                    last_rank_check_at = 0.0
                    reset_round_invalid_plays()
                    invalidate_hand_cache()
                    _log_deck_state("三局结束牌库最终状态")
                    log(f"完整三轮已处理完成，场次数={matches_completed}")
                    race_started_at = None
                    current_horse = None
                    current_limit = None
                    last_hand_log = None
                elif time.monotonic() - reward_pending_at < 6.0:
                    time.sleep(0.15)
                    continue
                else:
                    save_debug(image, "reward_result_unknown")
                    log("[领奖] 6秒内未连续两帧确认大厅；重新识别领奖按钮")
                    reward_pending_at = 0.0
                    reward_lobby_confirmations = 0

            # --------------------------------------------------
            # 1) Lobby
            # --------------------------------------------------
            lobby_allowed = (
                screen_phase
                in (
                    "auto",
                    "lobby",
                    "result",
                )
            )

            # 匹配等待超时后允许重新确认一次真正的大厅。
            if (
                screen_phase == "wait_horse"
                and time.monotonic() - phase_since
                >= WAIT_HORSE_RECOVERY_SECONDS
            ):
                lobby_allowed = True

            if (
                lobby_allowed
                and is_lobby(image)
            ):
                if screen_phase != "lobby":
                    log(
                        f"[状态机] {screen_phase} → lobby"
                    )

                screen_phase = "lobby"
                phase_since = time.monotonic()

                race_started_at = None
                current_horse = None
                current_limit = None

                VISION_ENGINE.set_current_limit(
                    None
                )

                limit = int(
                    CFG.get(
                        "matches_to_play",
                        1,
                    )
                )

                if (
                    limit > 0
                    and matches_completed >= limit
                ):
                    log(
                        f"已完成 {matches_completed} 场，达到配置上限，程序结束"
                    )
                    break

                if int(
                    CFG.get(
                        "auto_start_match",
                        1,
                    )
                ):
                    start_button = (
                        confirm_android_start_match_button(
                            image
                        )
                    )

                    if start_button is None:
                        log(
                            "[V2.7匹配保护] 匹配页按钮未通过两帧确认，"
                            "本轮不点击"
                        )
                        time.sleep(
                            0.01
                        )
                        continue

                    log(
                        "[V2.7匹配页] "
                        f"布局={start_button['layout']}；"
                        f"按钮=({start_button['x']},{start_button['y']},"
                        f"{start_button['w']},{start_button['h']})；"
                        f"点击=({start_button['cx']:.0f},"
                        f"{start_button['cy']:.0f})"
                    )

                    current_round_index = 0
                    reward_click_attempts = 0
                    reward_pending_at = 0.0
                    reward_lobby_confirmations = 0
                    current_realtime_rank = None
                    last_rank_check_at = 0.0
                    reset_round_invalid_plays()
                    invalidate_hand_cache()
                    reset_match_memory()
                    reset_shared_deck()
                    _v32_reset_match_selection()

                    match_tap_ok = (
                        click_match_button_safe(
                            hwnd,
                            image,
                            start_button,
                        )
                    )

                    if not match_tap_ok:
                        log(
                            "[V2.8匹配保护] 开始匹配安全点击未发送；"
                            "保持lobby，不切换状态"
                        )
                        time.sleep(
                            0.08
                        )
                        continue

                    screen_phase = "wait_horse"
                    phase_since = time.monotonic()
                    matches_started += 1
                    start_race_report(matches_started)

                    log(
                        "[状态机V3.3] lobby → wait_horse；"
                        "已发送安全开始匹配点击；"
                        "等待大厅按钮真正消失后才允许选马"
                    )

                    continue

            # --------------------------------------------------
            # 2) Horse selection
            # --------------------------------------------------
            # V2.8：点击“开始匹配”后，必须先确认大厅按钮已经消失。
            # 防止页面尚未切换时把大厅的马匹背景误判成“选马页”，
            # 从而产生第二次错误点击。
            if screen_phase == "wait_horse":
                wait_elapsed = (
                    time.monotonic()
                    - phase_since
                )

                # 给小游戏一个最短转场时间；这期间绝不再点击任何东西。
                if wait_elapsed < 0.45:
                    time.sleep(
                        0.01
                    )
                    continue

                still_lobby_button = (
                    find_android_start_match_button(
                        image
                    )
                )

                if still_lobby_button is not None:
                    if wait_elapsed < 2.50:
                        # 按钮还在：说明仍停留在大厅，继续等，不允许选马。
                        time.sleep(
                            0.02
                        )
                        continue

                    # 2.5秒后按钮仍在，认为本次tap没有生效。
                    # 回到lobby，下一轮重新经过安全点击。
                    log(
                        "[V2.8转场保护] 2.5秒后“开始匹配”仍存在；"
                        "本次点击未生效，返回lobby安全重试"
                    )
                    screen_phase = "lobby"
                    phase_since = time.monotonic()
                    time.sleep(
                        0.08
                    )
                    continue

            horse_select_allowed = (
                screen_phase
                in (
                    "auto",
                    "wait_horse",
                    "race",
                )
            )

            if (
                horse_select_allowed
                and is_horse_select(image)
            ):
                if screen_phase != "wait_horse":
                    log(
                        f"[状态机] {screen_phase} → wait_horse"
                    )

                screen_phase = "wait_horse"
                phase_since = time.monotonic()

                race_started_at = None

                (
                    horse,
                    info,
                    all_info,
                    selection_values,
                    horse_metrics,
                ) = choose_horse(
                    image,
                    round_index=min(
                        3,
                        current_round_index + 1,
                    ),
                )

                if horse is None:
                    if horse_metrics.get(
                        "_waiting",
                        False,
                    ):
                        time.sleep(
                            0.015
                        )
                        continue

                    log(
                        "选马页已识别，但暂未得到可靠选择"
                    )

                    time.sleep(
                        0.05
                    )
                    continue

                label = {
                    "lower": "下等马",
                    "middle": "中等马",
                    "upper": "上等马",
                }[horse]

                current_horse = horse
                current_limit = {
                    "lower": 8,
                    "middle": 9,
                    "upper": 10,
                }[horse]

                VISION_ENGINE.set_current_limit(
                    current_limit
                )

                if selection_values:
                    log(
                        "选马页识别本轮手牌："
                        + " ".join(
                            str(v)
                            for v in selection_values
                        )
                    )

                    # 三局共享同一牌库：这里绝不重置。
                    # 只把新出现的牌从40张剩余牌库中扣除。
                    observe_shared_hand(
                        selection_values
                    )

                if horse_metrics:
                    for name in (
                        "lower",
                        "middle",
                        "upper",
                    ):
                        if name not in horse_metrics:
                            continue

                        metric = horse_metrics[
                            name
                        ]

                        log(
                            f"选马预估 {HORSE_LABEL[name]}："
                            f"上限={HORSE_CAPACITY[name]}，"
                            f"当前最佳={metric['current']:.2f}x，"
                            f"精确补牌期望={metric['expected']:.2f}x，"
                            f"容量增益={metric.get('capacity_gain',0):+.2f}，"
                            f">=3x={metric['strong_prob']:.0%}，"
                            f">=4.2x={metric['very_strong_prob']:.0%}，"
                            f"炸弹={metric.get('bomb_prob',0):.0%}，"
                            f"连三={metric.get('triple_run_prob',0):.0%}，"
                            f"保留成本={metric.get('reserve_penalty_effective',0):.3f}，"
                            f"规则分={metric.get('utility',0):.3f}，"
                            f"AI修正={metric.get('learned_adjustment',0):+.3f}，"
                            f"AI样本={metric.get('learned_samples',0)}，"
                            f"最终={metric.get('final_utility',metric.get('utility',0)):.3f}"
                        )

                log(
                    f"动态选马结果：{label} "
                    f"sat={info['sat']:.1f} "
                    f"ratio={info['ratio']:.2f} "
                    f"手牌上限={current_limit}"
                )

                round_for_choice = min(
                    3,
                    current_round_index + 1,
                )

                current_round_index = (
                    round_for_choice
                )

                current_realtime_rank = None
                last_rank_check_at = 0.0
                reset_round_invalid_plays()
                invalidate_hand_cache()

                horse_tap_ok = (
                    _v31_click_horse_safe(
                        horse,
                        current_round_index,
                    )
                )

                if not horse_tap_ok:
                    log(
                        "选马点击未发送，保持wait_horse等待重试"
                    )
                    screen_phase = "wait_horse"
                    time.sleep(
                        0.10
                    )
                    continue

                record_horse_decision(selection_values, all_info, horse,
                                      round_for_choice)
                race_report_horse(round_for_choice, horse, selection_values,
                                  horse_metrics, all_info)

                _V32_USED_HORSES.add(
                    horse
                )

                _v32_reset_selection_runtime()

                time.sleep(
                    0.08
                )

                # V1.5关键：选完马以后立即锁到 WAIT_RACE。
                # 旧版下一帧又把比赛画面误认成选马页，
                # 导致不断重复选马、current_round_index被连续增加，
                # 比赛出牌逻辑永远进不去。
                screen_phase = "wait_race"
                phase_since = time.monotonic()
                horse_selected_at = phase_since
                wait_race_last_diag = 0.0

                VISION_ENGINE.suppress_hand_for(
                    0.10
                )
                invalidate_hand_cache()

                log(
                    f"[状态机] 已选择{label} → wait_race；"
                    "从现在起禁止大厅/选马识别，只等待比赛手牌"
                )

                continue

            # --------------------------------------------------
            # 3) Race
            # --------------------------------------------------
            cards = (
                VISION_ENGINE.get_stable_hand()
                if int(
                    CFG.get(
                        "async_vision_enabled",
                        1,
                    )
                )
                else None
            )

            if cards is None:
                cards = read_hand_fast(
                    image
                )

            if (
                current_limit is not None
                and len(cards)
                > int(
                    current_limit
                )
            ):
                over = (
                    len(cards)
                    - int(
                        current_limit
                    )
                )

                allowed = int(
                    CFG.get(
                        "allowed_over_capacity",
                        1,
                    )
                )

                if over <= allowed:
                    log(
                        f"手牌已超上限：{len(cards)}/{current_limit}。"
                        "不再等待，立即进入紧急出牌腾位置"
                    )
                else:
                    log(
                        f"手牌识别暂不可信：{len(cards)}/{current_limit}，"
                        f"超过允许瞬时溢出 +{allowed}；"
                        "等待多线程下一次稳定识别"
                    )

                    time.sleep(
                        0.03
                    )

                    continue

            # V1.5：选马页偶尔只识别出1~2张卡片数字，
            # 这些不能当成“已经进入比赛”。
            if (
                screen_phase == "wait_race"
                and len(cards) < RACE_ENTER_MIN_CARDS
            ):
                now_wait = time.monotonic()

                if (
                    now_wait - phase_since
                    >= WAIT_RACE_DIAGNOSTIC_SECONDS
                    and now_wait - wait_race_last_diag
                    >= 4.0
                ):
                    log(
                        "[状态机] wait_race："
                        f"当前只识别到 {len(cards)} 张牌，"
                        "继续等待真实比赛手牌；"
                        "不会重复选马/点大厅"
                    )
                    wait_race_last_diag = now_wait

                if (
                    len(cards) == 0
                    and now_wait
                    - wait_race_last_debug_save
                    >= WAIT_RACE_DEBUG_SAVE_INTERVAL
                ):
                    save_debug(
                        image,
                        "wait_race_zero_cards",
                    )
                    wait_race_last_debug_save = now_wait

            if 1 <= len(cards) <= 10:
                if (
                    screen_phase == "wait_race"
                    and len(cards) >= RACE_ENTER_MIN_CARDS
                ):
                    screen_phase = "race"
                    phase_since = time.monotonic()

                    log(
                        f"[状态机] wait_race → race；"
                        f"确认比赛手牌 {len(cards)} 张，"
                        "启用自动出牌"
                    )

                # 启动中途进入比赛时也允许自动接管。
                elif (
                    screen_phase == "auto"
                    and len(cards) >= RACE_ENTER_MIN_CARDS
                ):
                    screen_phase = "race"
                    phase_since = time.monotonic()

                    log(
                        f"[状态机] auto → race；"
                        f"检测到 {len(cards)} 张比赛手牌"
                    )

                # 在 wait_race 下1~2张只是选马页残影，不运行出牌。
                if screen_phase != "race":
                    time.sleep(
                        0.03
                    )
                    continue

                if race_started_at is None:
                    race_started_at = time.monotonic()

                    mark_race_started()

                    if current_limit is None:
                        # Restarted mid-race: use a conservative inferred limit.
                        current_limit = max(
                            8,
                            len(cards),
                        )

                    log(
                        f"进入比赛阶段；当前手牌上限按 {current_limit} 处理"
                    )

                # Realtime ranking is recognized by a dedicated worker thread.
                detected_rank, rank_score = (
                    VISION_ENGINE.get_rank()
                )

                if (
                    detected_rank is not None
                    and detected_rank
                    != current_realtime_rank
                ):
                    old_rank = (
                        current_realtime_rank
                    )

                    current_realtime_rank = (
                        detected_rank
                    )
                    race_report_rank(current_round_index, detected_rank,
                                     rank_score, time.monotonic() - race_started_at)

                    log(
                        f"实时排名：{detected_rank}/4 "
                        f"score={rank_score:.3f}"
                    )

                    if (
                        old_rank is not None
                    ):
                        record_realtime_rank_change(
                            old_rank,
                            detected_rank,
                            time.monotonic()
                            - race_started_at,
                        )

                hand_values = [
                    card["value"]
                    for card in cards
                ]

                observe_shared_hand(
                    hand_values
                )

                signature = tuple(
                    hand_values
                )

                if signature != last_hand_log:
                    hand_scores = [
                        float(card.get("score", 0.0))
                        for card in cards
                    ]
                    log(
                        "识别手牌："
                        + " ".join(
                            str(v)
                            for v in hand_values
                        )
                        + f" ({len(cards)}/{current_limit}) "
                        + (
                            f"conf(avg/min)="
                            f"{np.mean(hand_scores):.3f}/"
                            f"{min(hand_scores):.3f}"
                            if hand_scores
                            else ""
                        )
                    )
                    event_log(
                        "hand_used_for_decision",
                        phase=screen_phase,
                        values=list(hand_values),
                        count=len(cards),
                        limit=current_limit,
                        avg_score=(
                            round(float(np.mean(hand_scores)), 4)
                            if hand_scores else None
                        ),
                        min_score=(
                            round(float(min(hand_scores)), 4)
                            if hand_scores else None
                        ),
                        xs=[round(float(card.get("click_x", 0.0)), 1) for card in cards],
                    )

                    last_hand_log = signature

                    if (
                        int(
                            CFG.get(
                                "probability_log",
                                1,
                            )
                        )
                        and DECK_STATE.get(
                            "initialized",
                            False,
                        )
                    ):
                        bomb_probs = (
                            current_bomb_probabilities(
                                hand_values,
                                max_draw=min(
                                    3,
                                    max(
                                        0,
                                        int(
                                            current_limit
                                            or len(
                                                cards
                                            )
                                        )
                                        - len(
                                            cards
                                        ),
                                    ),
                                ),
                            )
                        )

                        interesting = [
                            (
                                value,
                                probability,
                            )
                            for value, probability in bomb_probs.items()
                            if probability > 0.005
                        ]

                        interesting.sort(
                            key=lambda item: -item[1]
                        )

                        if interesting:
                            log(
                                "炸弹机会："
                                + "；".join(
                                    f"{value}号={probability:.1%}"
                                    for value, probability in interesting[:4]
                                )
                            )

                now = time.monotonic()

                urgent_target = (
                    max(
                        0,
                        int(
                            current_limit
                            or len(
                                cards
                            )
                        )
                        - int(
                            CFG.get(
                                "reserve_slots_before_refill",
                                3,
                            )
                        ),
                    )
                    if future_refills_left() > 0
                    else 0
                )

                urgent_release = (
                    len(
                        cards
                    )
                    > urgent_target
                )

                play_interval = (
                    float(
                        CFG.get(
                            "urgent_play_interval",
                            0.30,
                        )
                    )
                    if urgent_release
                    else float(
                        CFG[
                            "min_seconds_between_plays"
                        ]
                    )
                )

                if (
                    now - last_play_at
                    >= play_interval
                ):
                    elapsed = (
                        now
                        - race_started_at
                    )

                    choose_started = time.perf_counter()
                    play = choose_play(
                        cards,
                        current_limit,
                        elapsed,
                        round_index=max(
                            1,
                            current_round_index,
                        ),
                        horse=current_horse,
                        realtime_rank=current_realtime_rank,
                    )
                    choose_ms = (
                        time.perf_counter()
                        - choose_started
                    ) * 1000.0

                    event_log(
                        "decision",
                        choose_ms=round(choose_ms, 3),
                        hand=list(hand_values),
                        play=(play.get("name") if play else None),
                        values=(list(play.get("values", [])) if play else []),
                        urgent=bool(urgent_release),
                        rank=current_realtime_rank,
                        round_index=current_round_index,
                    )

                    if choose_ms >= 30.0:
                        log(
                            f"[性能] choose_play耗时={choose_ms:.1f}ms"
                        )

                    if (
                        play is None
                        and now - last_no_play_log >= 3.0
                    ):
                        log(
                            "[出牌] 已进入比赛并识别到手牌，"
                            "但当前没有满足策略的合法组合，继续等待/重新识别"
                        )
                        last_no_play_log = now

                    if play is not None:
                        log(
                            f"[出牌] 策略选择：{play.get('name')} "
                            f"values={play.get('values')} "
                            f"倍率={play.get('multiplier',1.0):.2f}"
                        )

                        execute_started = time.perf_counter()
                        ok = execute_play(
                            hwnd,
                            cards,
                            play,
                        )
                        execute_ms = (
                            time.perf_counter()
                            - execute_started
                        ) * 1000.0

                        event_log(
                            "execute_play_result",
                            ok=bool(ok),
                            execute_ms=round(execute_ms, 3),
                            play=play.get("name"),
                            values=list(play.get("values", [])),
                        )

                        if not ok:
                            log(
                                f"[出牌] 本次执行未确认成功；耗时={execute_ms:.0f}ms"
                            )

                        if ok:
                            race_report_play(current_round_index, play,
                                             hand_values, current_realtime_rank,
                                             time.monotonic() - race_started_at)
                            last_play_at = (
                                time.monotonic()
                            )

                            record_play_decision(
                                play,
                                round_index=max(
                                    1,
                                    current_round_index,
                                ),
                            )

                            if MATCH_MEMORY.get(
                                "play_decisions"
                            ):
                                MATCH_MEMORY[
                                    "play_decisions"
                                ][-1][
                                    "rank_before"
                                ] = current_realtime_rank

                        time.sleep(
                            float(
                                CFG.get(
                                    "urgent_loop_sleep",
                                    0.006,
                                )
                            )
                            if urgent_release
                            else float(
                                CFG.get(
                                    "normal_loop_sleep",
                                    0.018,
                                )
                            )
                        )
                        continue

                time.sleep(
                    float(
                        CFG.get(
                            "normal_loop_sleep",
                            0.018,
                        )
                    )
                    if int(
                        CFG.get(
                            "async_vision_enabled",
                            1,
                        )
                    )
                    else float(
                        CFG.get(
                            "scan_interval",
                            0.010,
                        )
                    )
                )

                continue

            # --------------------------------------------------
            # 4) Round / match result
            # --------------------------------------------------
            if (
                screen_phase in (
                    "race",
                    "result",
                    "auto",
                )
                and is_race_end(image)
            ):
                screen_phase = "result"
                phase_since = time.monotonic()
                result_rank, result_score = detect_user_rank(image)
                race_report_round_result(current_round_index, result_rank,
                                         result_score, current_realtime_rank)

                # Learn from FINAL placement before this screen disappears.
                if learning_enabled() and current_round_index >= 3:
                    apply_match_learning(
                        image
                    )

                log("识别到“比赛结束”，点击继续")

                click_rel(
                    hwnd,
                    0.50,
                    0.925,
                    wait=2.0,
                )

                # Reward animation overlay often asks for another click.
                click_rel(
                    hwnd,
                    0.50,
                    0.915,
                    wait=1.35,
                )

                race_started_at = None
                current_horse = None
                current_limit = None
                last_hand_log = None
                finish_shared_deck_round()

                screen_phase = "wait_horse"
                phase_since = time.monotonic()

                log(
                    "[状态机] 本轮比赛结束 → wait_horse；"
                    "等待下一轮选马或最终奖励"
                )

                continue

            reward = find_reward_button(
                image
            )

            if (
                reward is not None
                and current_round_index >= 3
                and int(
                    CFG.get(
                        "auto_receive_reward",
                        1,
                    )
                )
            ):
                if reward_click_attempts >= 3:
                    save_debug(image, "reward_not_confirmed")
                    log("[STOP] 领奖点击3次后仍未确认返回赛马大厅")
                    break
                log(
                    f"识别到“领取奖励”，score={reward['score']:.3f}，点击"
                )

                reward_click_attempts += 1
                clicked = click_client(
                    hwnd,
                    reward["center"][0],
                    reward["center"][1],
                    wait=1.2,
                )

                if not clicked:
                    save_debug(image, "reward_click_failed")
                    continue

                reward_pending_at = time.monotonic()
                reward_lobby_confirmations = 0
                log("[领奖] 已点击，等待连续两帧确认赛马大厅")
                continue

            # 没匹配到当前状态时只是等待下一帧，不再因缺配置崩溃。
            time.sleep(
                float(
                    CFG.get(
                        "scan_interval",
                        0.010,
                    )
                )
            )

        except pyautogui.FailSafeException:
            print()
            log("收到ADB安全停止")
            break

        except KeyboardInterrupt:
            print()
            log("程序退出")
            break

        except Exception as exc:
            tb_lines = (
                traceback.format_exc()
                .strip()
                .splitlines()
            )

            tb_tail = " | ".join(
                tb_lines[
                    -5:
                ]
            )

            log(
                f"主循环异常："
                f"{type(exc).__name__}: {exc}；"
                f"定位={tb_tail}"
            )
            event_log(
                "main_exception",
                error_type=type(exc).__name__,
                error=str(exc),
                traceback=traceback.format_exc(),
                phase=screen_phase,
            )

            if "DXGI" in str(exc) or "scrcpy" in str(exc):
                capture_recovery_failures += 1
                if capture_recovery_failures > 3:
                    log("[STOP] 镜像窗口连续3次恢复失败；停止自动点击并保留诊断日志")
                    break
                try:
                    item = SCRCPY_MANAGER.ensure_window()
                    if item is None:
                        raise RuntimeError("没有可用的scrcpy窗口")
                    new_hwnd = int(item["hwnd"])
                    if new_hwnd != VISION_ENGINE.hwnd:
                        VISION_ENGINE.start(new_hwnd)
                    else:
                        VISION_ENGINE.invalidate_after_action()
                    hwnd = new_hwnd
                    invalidate_hand_cache()
                    SCRCPY_CAPTURE.reset_geometry()
                    log(f"[scrcpy恢复] 第{capture_recovery_failures}次已绑定窗口 {hwnd}；等待新帧")
                    time.sleep(0.35)
                    continue
                except Exception as recovery_exc:
                    log(f"[scrcpy恢复] 第{capture_recovery_failures}次失败：{recovery_exc}")

            try:
                debug, _ = capture(hwnd)
                save_debug(
                    debug,
                    "main_exception",
                )
            except Exception:
                pass

            time.sleep(0.12)

if __name__ == "__main__":
    main()
