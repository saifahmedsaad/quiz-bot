import os, glob
import pymupdf as fitz

DIR = os.path.join(os.environ["USERPROFILE"], "AppData", "Local", "hermes", "profiles", "telegram-bots", "cache", "documents")
IMG = os.path.join(os.environ["USERPROFILE"], "quiz_bot", "ocr_images")
os.makedirs(IMG, exist_ok=True)

targets = {
    "kitab_khariji": "doc_85bcd39c5b0e_المعلومات_والوسائط1_الكتاب_الخارجي.pdf",
    "amali": "doc_f5f4ee4b39c3_الجزء_العملي_ص1-8.pdf",
    "malzama": "doc_f89a591951c5_المعلومات_والوسائط1_الملزمه.pdf",
}

zoom = 2.2
mat = fitz.Matrix(zoom, zoom)
for key, fname in targets.items():
    p = os.path.join(DIR, fname)
    doc = fitz.open(p)
    n = doc.page_count
    for i, pg in enumerate(doc, 1):
        pix = pg.get_pixmap(matrix=mat)
        out = os.path.join(IMG, f"{key}_p{i}.png")
        pix.save(out)
    doc.close()
    print(f"{key}: {n} pages -> PNG")

print("DONE_IMAGES")
