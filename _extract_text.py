import os, glob
import pymupdf as fitz

DIR = os.path.join(os.environ["USERPROFILE"], "AppData", "Local", "hermes", "profiles", "telegram-bots", "cache", "documents")
OUT = os.path.join(os.environ["USERPROFILE"], "quiz_bot", "extracted_text")
os.makedirs(OUT, exist_ok=True)

# dedup by basename
seen = {}
pdfs = sorted(glob.glob(os.path.join(DIR, "*.pdf")))
for p in pdfs:
    base = os.path.basename(p)
    key = base.split("_", 1)[-1]  # strip doc_xxxx prefix
    if key in seen:
        continue
    seen[key] = p
    doc = fitz.open(p)
    txt = ""
    for pg in doc:
        txt += pg.get_text()
    doc.close()
    name = os.path.splitext(key)[0].replace(" ", "_") + ".txt"
    with open(os.path.join(OUT, name), "w", encoding="utf-8") as f:
        f.write(txt)
    print(f"{name}: {len(txt)} chars")
print("DONE")
