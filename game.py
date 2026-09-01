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
        self.week_id: str = qset.id
        self.test_mode: bool = False
        self.test_chat: Optional[int] = None
        self.finished: bool = False
        # الطلاب国家安全: chat_id -> state
        self.student_states: Dict[int, dict] = {}

    def get_student(self, chat_id: int) -> dict:
        """حالة الطالب، 如果不存在则初始化."""
        if chat_id not in self.student_states:
            self.student_states[chat_id] = {
                "index": 0,
                "pending": None,
                "score": 0,
                "correct": 0,
                "wrong": 0,
                "timer_end": 0.0,
                "finished": False,
                "started_at": 0.0,
            }
        return self.student_states[chat_id]

    def current_question(self, chat_id: int) -> Optional[Question]:
        state = self.get_student(chat_id)
        if 0 <= state["index"] < len(self.set.questions):
            return self.set.questions[state["index"]]
        return None

    def is_last(self, chat_id: int) -> bool:
        state = self.get_student(chat_id)
        return state["index"] >= len(self.set.questions) - 1

    def advance(self, chat_id: int) -> Optional[Question]:
        """الانتقال للسؤال التالي للطالب特定."""
        state = self.get_student(chat_id)
        state["index"] += 1
        return self.current_question(chat_id)

    def seconds_left(self, chat_id: int) -> int:
        state = self.get_student(chat_id)
        if state["timer_end"] == 0:
            return self.timer
        elapsed = time.time() - state["timer_end"] + self.timer
        return max(0, int(self.timer - elapsed))

    def record(self, chat_id: int, points: int, is_correct: bool) -> None:
        state = self.get_student(chat_id)
        state["score"] += points
        if is_correct:
            state["correct"] += 1
        else:
            state["wrong"] += 1
