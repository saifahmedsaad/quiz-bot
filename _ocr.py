import os, glob
P314 = "C:/Users/saif.ahmed/AppData/Local/Python/pythoncore-3.14-64/python.exe"
IMG = os.path.join(os.environ["USERPROFILE"], "quiz_bot", "ocr_images")
OUT = os.path.join(os.environ["USERPROFILE"], "quiz_bot", "extracted_text")
os.makedirs(OUT, exist_ok=True)

import easyocr
reader = easyocr.Reader(["ar", "en"], gpu=False)

imgs = sorted(glob.glob(os.path.join(IMG, "*.png")))
# group by prefix
from collections import defaultdict
groups = defaultdict(list)
for im in imgs:
    name = os.path.basename(im)
    key = name.rsplit("_p", 1)[0]
    groups[key].append(im)

for key, files in groups.items():
    files = sorted(files)
    text = ""
    for f in files:
        res = reader.readtext(f, detail=0, paragraph=True)
        text += f"\n=== {os.path.basename(f)} ===\n" + "\n".join(res) + "\n"
    outname = os.path.join(OUT, f"ocr_{key}.txt")
    with open(outname, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(f"{key}: {len(files)} pages, {len(text)} chars -> {os.path.basename(outname)}")

print("OCR_DONE")
