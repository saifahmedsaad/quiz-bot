"""Per-chat quiz session state.

Each Telegram chat (group) that starts a quiz gets its own QuizSession.
The session tracks the current set, question index, scores, and a timer.
This is intentionally decoupled from Telegram so it is easy to test/reuse.
"""
from __future__ import annotations

import time
from typing import Dict, Optional

from quiz_data import QuizSet, Question


class QuizSession:
    def __init__(self, qset: QuizSet, timer_seconds: int = 20):
        self.set = qset
        self.timer = timer_seconds
        self.index = 0
        self.scores: Dict[int, int] = {}          # user_id -> score
        self.names: Dict[int, str] = {}           # user_id -> display name
        self.question_start: float = 0.0
        self.finished = False
        self.chat_id: Optional[int] = None        # group chat id this session belongs to
        self.pending: Dict[int, dict] = {}        # user_id -> {"pick": int|None, "msg_id": int}
        self.q_nonce: int = 0
        self.answered: set = set()
        self.waiting: set = set()                 # students waiting for the quiz to open
        # --- new fields used by the Quiz Bot flow ---
        self.week_id: str = qset.id               # which weekly set this session is for
        self.test_mode: bool = False              # True => teacher tests the quiz (sees buttons)
        self.test_chat: Optional[int] = None      # chat id that receives test-mode questions

    @property
    def current(self) -> Optional[Question]:
        if 0 <= self.index < len(self.set.questions):
            return self.set.questions[self.index]
        return None

    @property
    def is_last(self) -> bool:
        return self.index >= len(self.set.questions) - 1

    def next(self) -> Optional[Question]:
        self.index += 1
        self.question_start = time.time()
        return self.current

    def seconds_left(self) -> int:
        elapsed = time.time() - self.question_start
        return max(0, int(self.timer - elapsed))

    def record(self, user_id: int, name: str, points: int) -> None:
        self.scores[user_id] = self.scores.get(user_id, 0) + points
        self.names[user_id] = name

    def leaderboard(self) -> str:
        if not self.scores:
            return "لا يوجد نقاط بعد."
        ranked = sorted(self.scores.items(), key=lambda x: x[1], reverse=True)
        lines = ["🏆 *الترتيب الحالي:*"]
        for i, (uid, sc) in enumerate(ranked, 1):
            lines.append(f"{i}. {self.names.get(uid, 'طالب')} — {sc} نقطة")
        return "\n".join(lines)
