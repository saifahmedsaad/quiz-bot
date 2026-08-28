"""Question bank loader.

Loads quiz sets from data/questions.json AND from data/weekly/*.json
(the weekly folder makes it easy to swap the quiz every week).

Each weekly file is a single quiz set, e.g.:
  {"id": "week1", "title": "الأسبوع الأول - المعلومات والوسائط", "questions": [...]}
"""
from __future__ import annotations

import glob
import json
import os
import random
from dataclasses import dataclass, field
from typing import List

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(HERE, "data", "questions.json")
WEEKLY_DIR = os.path.join(HERE, "data", "weekly")


@dataclass
class Question:
    q: str
    options: List[str]
    correct: int
    explanation: str = ""

    def correct_text(self) -> str:
        return self.options[self.correct]

    def shuffle_options(self) -> "Question":
        """Return a copy with options shuffled and correct index fixed."""
        import random
        pairs = list(enumerate(self.options))
        random.shuffle(pairs)
        new_options = [t for _, t in pairs]
        new_correct = next(i for i, (orig_i, _) in enumerate(pairs) if orig_i == self.correct)
        return Question(self.q, new_options, new_correct, self.explanation)


@dataclass
class QuizSet:
    id: str
    title: str
    questions: List[Question] = field(default_factory=list)


def load_weekly(path: str = WEEKLY_DIR) -> List[QuizSet]:
    """Load every weekly quiz file as its own set.

    Files are sorted by name so week1, week2, ... appear in order.
    A weekly file must contain: {"id", "title", "questions": [...]}
    """
    sets: List[QuizSet] = []
    if not os.path.isdir(path):
        return sets
    for fp in sorted(glob.glob(os.path.join(path, "*.json"))):
        try:
            with open(fp, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as e:
            print(f"[warn] could not load weekly file {fp}: {e}")
            continue
        qs = [Question(**q) for q in raw.get("questions", [])]
        sets.append(QuizSet(raw.get("id", os.path.basename(fp)), raw.get("title", "كويز أسبوعي"), qs))
    return sets


def load_sets(path: str = DATA_PATH, include_weekly: bool = True) -> List[QuizSet]:
    """Load quiz sets.

    By default ONLY the weekly folder (data/weekly/*.json) is used, so the
    bot's quiz list is driven entirely by weekly files — drop a week1.json,
    week2.json, ... and they appear in /sets automatically. The main
    questions.json bank is ignored unless include_weekly is toggled off AND
    you re-enable the core bank.
    """
    sets: List[QuizSet] = []
    if include_weekly:
        sets.extend(load_weekly())
    return sets


def load_sets_core(path: str = DATA_PATH) -> List[QuizSet]:
    """Load only the main question bank (data/questions.json)."""
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    sets: List[QuizSet] = []
    for s in raw.get("sets", []):
        qs = [Question(**q) for q in s["questions"]]
        sets.append(QuizSet(s["id"], s["title"], qs))
    return sets


def get_set(set_id: str, path: str = DATA_PATH) -> QuizSet | None:
    for s in load_sets(path):
        if s.id == set_id:
            return s
    return None


if __name__ == "__main__":
    sets = load_sets()
    for s in sets:
        print(f"[{s.id}] {s.title} — {len(s.questions)} أسئلة")
