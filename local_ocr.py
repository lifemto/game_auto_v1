"""Read changing game text with the locally installed Windows OCR engine."""
import json
import subprocess
import tempfile
from pathlib import Path

import cv2


ROOT = Path(__file__).resolve().parent


def text_box(data, label):
    """Locate an exact label across OCR words; coordinates are in the frame."""
    for line in data.get("lines", []):
        words = line.get("words", [])
        parts = ["".join(word.get("text", "").split()) for word in words]
        joined = "".join(parts)
        start = joined.find(label)
        if start < 0:
            continue
        offset, selected = 0, []
        for word, part in zip(words, parts):
            if offset < start + len(label) and offset + len(part) > start:
                selected.append(word)
            offset += len(part)
        if selected:
            return (min(w["x"] for w in selected), min(w["y"] for w in selected),
                    max(w["x"]+w["width"] for w in selected),
                    max(w["y"]+w["height"] for w in selected))
    return None


def read_text(frame, roi, scale=2, context=False, mode=None):
    """OCR a 726x1600 canonical rectangle; return text and frame-coordinate words."""
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = [round(v*s) for v, s in zip(roi, (w/726, h/1600, w/726, h/1600))]
    patch = frame[max(0,y1):min(h,y2), max(0,x1):min(w,x2)]
    if patch.size == 0:
        raise ValueError("Empty OCR region")
    if mode in ("white", "green", "yellow"):
        # Isolate the actual lettering from progress bars and textured panels.
        # Keep geometry unchanged so OCR word positions still map to the frame.
        hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
        bounds = (((0, 0, 175), (180, 95, 255)) if mode == "white" else
                  ((30, 75, 110), (95, 255, 255)) if mode == "green" else
                  ((15, 80, 120), (45, 255, 255)))
        mask = cv2.inRange(hsv, *bounds)
        patch = cv2.cvtColor(255-mask, cv2.COLOR_GRAY2BGR)
    padding = 0
    if context:
        # Windows OCR often ignores a short standalone word or a single digit.
        # Supply surrounding Latin text, then discard those added words.
        import numpy as np
        ph, pw = patch.shape[:2]
        padding = 90
        canvas = np.full((ph + 40, pw + 2*padding, 3), 255, np.uint8)
        canvas[20:20+ph, padding:padding+pw] = patch
        baseline = 20 + ph//2 + 8
        cv2.putText(canvas, "TEXT", (5, baseline), cv2.FONT_HERSHEY_SIMPLEX, .7, (0,0,0), 2)
        cv2.putText(canvas, "END", (padding+pw+10, baseline), cv2.FONT_HERSHEY_SIMPLEX, .7, (0,0,0), 2)
        patch = canvas
    patch = cv2.resize(patch, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    out = ROOT / "runtime" / "ocr"
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".png", dir=out, delete=False) as f:
        path = Path(f.name)
    try:
        if not cv2.imwrite(str(path), patch):
            raise RuntimeError("Cannot write OCR image")
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-File",
             str(ROOT / "windows_ocr.ps1"), str(path)],
            capture_output=True, encoding="utf-8", errors="replace", timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode:
            raise RuntimeError(f"Windows OCR failed: {result.stderr.strip()[:350]}")
        data = json.loads(result.stdout.lstrip("\ufeff"))
        if context:
            for line in data["lines"]:
                line["words"] = [word for word in line["words"]
                                 if padding*scale <= word["x"] < (padding+pw)*scale]
                line["text"] = " ".join(word["text"] for word in line["words"])
            data["text"] = " ".join(line["text"] for line in data["lines"])
        for line in data["lines"]:
            for word in line["words"]:
                word["x"] = x1 + word["x"] / scale - padding
                word["y"] = y1 + word["y"] / scale - (20 if context else 0)
                word["width"] /= scale
                word["height"] /= scale
        return data
    finally:
        path.unlink(missing_ok=True)
