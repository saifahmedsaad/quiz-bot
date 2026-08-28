"""Export the question bank to a Blooket-importable CSV.

Blooket import format (simplified): columns
  question, answer, option1, option2, option3, option4, ...

Run:  python scripts/make_blooket_csv.py
Output: data/blooket_questions.csv
"""
from __future__ import annotations

import csv
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)
from quiz_data import load_sets

OUT = os.path.join(ROOT, "data", "blooket_questions.csv")


def main() -> None:
    sets = load_sets()
    rows = []
    for s in sets:
        for q in s.questions:
            opts = list(q.options)
            # Ensure at least 4 columns for Blooket
            while len(opts) < 4:
                opts.append("")
            row = [q.q, q.correct_text()] + opts
            rows.append(row)

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["question", "answer", "option1", "option2", "option3", "option4"])
        writer.writerows(rows)

    print(f"✅ تم إنشاء {os.path.abspath(OUT)}")
    print(f"   عدد الأسئلة: {len(rows)}")


if __name__ == "__main__":
    main()
