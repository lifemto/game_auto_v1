# -*- coding: utf-8 -*-
from pathlib import Path
import json
import cv2

root = Path(__file__).resolve().parent
meta = json.loads((root / "template_meta.json").read_text(encoding="utf-8"))
missing=[]
bad=[]
count=0
for name,item in meta.items():
    if not isinstance(item,dict) or not item.get("file"):
        continue
    count += 1
    p=root/item["file"]
    if not p.exists():
        missing.append((name,str(p)))
        continue
    if cv2.imread(str(p),cv2.IMREAD_COLOR) is None:
        bad.append((name,str(p)))
print(f"模板总数: {count}")
print(f"缺失: {len(missing)}")
print(f"损坏/不可读: {len(bad)}")
if missing:
    print("\n缺失文件:")
    for x in missing: print(x)
if bad:
    print("\n不可读文件:")
    for x in bad: print(x)
if not missing and not bad:
    print("[OK] 所有模板完整可读")
else:
    raise SystemExit(1)
