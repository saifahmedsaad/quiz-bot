import os, glob, sys
import pymupdf as fitz  # same as fitz

DIR = os.path.join(os.environ["USERPROFILE"], "AppData", "Local", "hermes", "profiles", "telegram-bots", "cache", "documents")

pdfs = sorted(glob.glob(os.path.join(DIR, "*.pdf")))
for p in pdfs:
    try:
        doc = fitz.open(p)
        n = doc.page_count
        txt = ""
        for pg in doc:
            txt += pg.get_text()
        print("="*70)
        print("FILE:", os.path.basename(p))
        print("PAGES:", n, "| TEXT_CHARS:", len(txt))
        # print first 1200 chars as a sample
        print("-"*70)
        print(txt[:1200])
        doc.close()
    except Exception as e:
        print("ERR", p, e)
