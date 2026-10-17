"""从用户 fht.mp4 提取已演示的烽火台模板；不连接设备。

录像是 444x1000，前 20 行是桌面标题栏；保留手机画面内的黑边。
所有模板统一到 726x1600，并在元数据中记录时间和原始裁剪范围。
"""
import json
from pathlib import Path

import cv2


ROOT = Path(__file__).resolve().parent
SPECS = {
    # name: (video seconds, crop in video pixels, search ROI in normalized frame)
    "beacon_excavation_event": (0, (159, 549, 191, 577), (55, 340, 680, 1160)),
    "beacon_resource_detail": (1.6, (15, 762, 76, 782), (5, 1160, 300, 1320)),
    "beacon_excavation_detail": (22, (92, 790, 188, 815), (130, 1200, 450, 1380)),
    "beacon_excavation_popup": (23.5, (213, 205, 292, 229), (130, 210, 605, 710)),
    "beacon_excavation_button": (23.5, (197, 362, 250, 386), (130, 430, 590, 1050)),
    "beacon_depart_text": (10, (191, 961, 254, 982), (210, 1500, 510, 1600)),
    "beacon_march_controls": (10, (17, 790, 145, 819), (5, 1220, 300, 1380)),
}


def read_frame(cap, seconds):
    cap.set(cv2.CAP_PROP_POS_MSEC, seconds * 1000)
    ok, frame = cap.read()
    if not ok or frame.shape[:2] != (1000, 444):
        raise ValueError(f"fht.mp4 {seconds}s: unexpected frame")
    return cv2.resize(frame[20:1000, :444], (726, 1600))


def main():
    cap = cv2.VideoCapture(str(ROOT / "fht.mp4"))
    meta_path = ROOT / "template_meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    try:
        for name, (seconds, crop, search) in SPECS.items():
            frame = read_frame(cap, seconds)
            left, top, right, bottom = crop
            rect = [round(left * 726 / 444), round((top - 20) * 1600 / 980),
                    round(right * 726 / 444), round((bottom - 20) * 1600 / 980)]
            x1, y1, x2, y2 = rect
            relative = f"templates/{name}.png"
            if not cv2.imwrite(str(ROOT / relative), frame[y1:y2, x1:x2]):
                raise OSError(relative)
            meta[name] = {
                "file": relative,
                "source": f"fht.mp4 at {seconds}s; game viewport (0,20,444,1000)",
                "crop_screenshot": list(crop),
                "expected_rect_raw": rect,
                "search_roi_raw": list(search),
                "threshold": .86,
            }
        # 右边缘的事件也会在原位置显示“前往”。
        meta["beacon_go_button"]["search_roi_raw"] = [40, 330, 710, 1200]
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
    finally:
        cap.release()


if __name__ == "__main__":
    main()
