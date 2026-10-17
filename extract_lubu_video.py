"""Rebuild Lv Bu templates and fixtures from the supplied recording, offline."""
import json
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parent
SPECS = {
    # seconds, original video crop, canonical search region
    "lubu_war": (18, (185,127,262,148), (230,130,480,240)),
    "lubu_card": (18, (322,232,407,260), (490,250,700,1280)),
    "lubu_join": (18, (271,270,304,291), (395,300,520,1260)),
    "lubu_march_target": (36, (165,141,279,174), (200,140,520,290)),
    "lubu_march_controls": (36, (301,791,428,818), (440,1200,715,1340)),
    "lubu_depart": (36, (196,964,251,984), (210,1500,510,1600)),
    "lubu_ratio": (12, (184,325,265,347), (230,440,490,580)),
    "lubu_ratio_confirm": (12, (199,687,249,709), (250,1020,480,1190)),
    "lubu_map_detail": (3, (171,738,291,777), (60,350,680,1370)),
    "lubu_map_rally": (3, (267,817,311,839), (300,400,700,1450)),
    "lubu_start_dialog": (5, (188,400,261,422), (200,540,520,750)),
    "lubu_start_three": (5, (50,540,132,565), (40,800,270,960)),
    "lubu_start": (5, (222,624,264,646), (180,920,530,1110)),
    "lubu_header": (75, (42,309,78,326), (50,300,150,650)),
    "lubu_activity_header": (75, (42,576,77,594), (45,390,170,1110)),
    "lubu_event_label": (1050, (217,633,308,646), (0,250,726,1400)),
    "lubu_cooldown": (1076, (203,602,250,618), (0,250,726,1400)),
    "lubu_buff": (6, (45,145,280,179), (60,180,520,300)),
}


def frame_at(cap, seconds):
    cap.set(cv2.CAP_PROP_POS_MSEC, seconds*1000)
    ok, frame = cap.read()
    if not ok or frame.shape[:2] != (1000,444):
        raise ValueError(f"Unexpected video frame at {seconds}")
    return cv2.resize(frame[20:], (726,1600))


def main():
    cap = cv2.VideoCapture(str(ROOT / "Video_2026-10-12_202021.mp4"))
    meta_path = ROOT / "template_meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    out = ROOT / "runtime/debug/lubu_video/fixtures"
    out.mkdir(parents=True, exist_ok=True)
    try:
        for name, (seconds, crop, roi) in SPECS.items():
            frame = frame_at(cap, seconds)
            x1,y1,x2,y2 = crop
            rect = [round(x1*726/444), round((y1-20)*1600/980),
                    round(x2*726/444), round((y2-20)*1600/980)]
            a,b,c,d = rect
            file = f"templates/{name}.png"
            if not cv2.imwrite(str(ROOT/file), frame[b:d,a:c]):
                raise OSError(file)
            threshold = .77 if name == "lubu_march_controls" else .68 if name == "lubu_buff" else .86
            meta[name] = dict(file=file, threshold=threshold, search_roi_raw=list(roi),
                              expected_rect_raw=rect, source=f"Video_2026-10-12_202021.mp4 {seconds}s", crop_screenshot=list(crop))
        for seconds in (0,3,5,6,10,12,14,16,18,24,30,33,36,39,69,75,150,510,1050,1076):
            cv2.imwrite(str(out/f"{seconds:04}.png"),frame_at(cap,seconds))
        meta_path.write_text(json.dumps(meta,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    finally:
        cap.release()


if __name__ == "__main__":
    main()
