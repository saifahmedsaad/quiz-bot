"""Telegram handlers for the Quiz Bot (الصف الأول الثانوي — مادة البرمجة).

Roles
-----
- Teacher (ADMIN_IDS from .env, or group creator/admin): full control menu.
- Student (anyone else): sees the weeks that are available, enters a quiz,
  or checks their own score.

Student flow
------------
  /start            -> lists available weeks (those with a scheduled open time
                      in the future OR already open). Tapping a week shows:
                        🧪 ادخل على الكويز  OR  📊 اشوف درجتي
  📊 اشوف درجتي      -> "جبت X من Y، صح Z، غلط W"
  🧪 ادخل على الكويز -> if scheduled & locked: "مش متاح دلوقتي — معاده ...".
                      if open: question with A/B/C/D buttons.

Teacher flow
------------
  /startnow         -> pick a week -> week panel:
                        📖 اشوف الأسئلة والإجابات
                        📊 اشوف درجات الطلاب (كل طالب وجاب كام)
                        📅 حدد المعاد للطلاب
  ▶️ ابدأ فوري       -> opens the quiz in TEST MODE: the teacher themself sees
                      the questions WITH option buttons (A/B/C/D icons) and can
                      answer them, so the quiz can be tested end to end.
  /schedule         -> pick day -> time -> week, then it opens for students at
                      that time (and students see the 12h lock message).

State persistence
-----------------
Per-week quiz data (open time, set, per-student score/correct/wrong, taken flag)
is stored in data/quiz_state.json so a bot restart does not wipe it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Dict, Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from quiz_data import load_sets, get_set, Question, QuizSet
import game

# Load .env BEFORE reading ADMIN_IDS.
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# ---------------------------------------------------------------------------
# Config / paths
# ---------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "data", "quiz_state.json")

_ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = {int(x) for x in _ADMIN_IDS_RAW.split(",") if x.strip().isdigit()}

logger = logging.getLogger("quiz_bot")

LETTERS = ["A", "B", "C", "D", "E", "F"]

# How long a quiz stays open after it starts (seconds).
QUIZ_OPEN_SECONDS = 30 * 60

# Egypt timezone (UTC+3) — used so /schedule HH:MM shows the correct local time
# when the server (Railway) is running UTC.
import datetime as _dt
EGYPT_TZ = _dt.timezone(_dt.timedelta(hours=3))

# Arabic date helpers --------------------------------------------------------
AR_DAYS = {
    "Saturday": "السبت", "Sunday": "الأحد", "Monday": "الإثنين",
    "Tuesday": "الثلاثاء", "Wednesday": "الأربعاء", "Thursday": "الخميس",
    "Friday": "الجمعة",
}
AR_MONTHS = {
    "January": "يناير", "February": "فبراير", "March": "مارس", "April": "أبريل",
    "May": "مايو", "June": "يونيو", "July": "يوليو", "August": "أغسطس",
    "September": "سبتمبر", "October": "أكتوبر", "November": "نوفمبر",
    "December": "ديسمبر",
}

# Mission-time helper for the 12-hour format with صباحاً/مساءً.
def fmt_12h(dt) -> str:
    h24 = dt.hour
    period = "صباحاً" if h24 < 12 else "مساءً"
    h12 = h24 % 12
    if h12 == 0:
        h12 = 12
    return f"{h12}:{dt.minute:02d} {period}"

def fmt_date_ar(dt) -> str:
    return f"{dt.day} {AR_MONTHS.get(dt.strftime('%B'), dt.strftime('%B'))}"

# ---------------------------------------------------------------------------
# Persistent state
# ---------------------------------------------------------------------------
# state[week_id] = {
#   "set_id": str,
#   "open_at": float|None,      # epoch seconds when it opens for students
#   "active": bool,             # a live session is running right now
#   "scores": {user_id(str): {"name": str, "score": int,
#                             "correct": int, "wrong": int, "taken": bool}},
# }
_state: Dict[str, dict] = {}

def _load_state() -> None:
    global _state
    _state = {}
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                _state = json.load(f)
        except Exception:
            _state = {}
    # Never restore an "active" flag across a restart — sessions live in memory.
    for w in _state.values():
        w["active"] = False

def _save_state() -> None:
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(_state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("could not save quiz state: %s", e)

def _week_state(week_id: str) -> dict:
    if week_id not in _state:
        _state[week_id] = {
            "set_id": week_id,
            "open_at": None,
            "active": False,
            "scores": {},
        }
    return _state[week_id]

def _ensure_weeks() -> None:
    for s in load_sets():
        _week_state(s.id)
    _save_state()

def _available_weeks() -> list:
    """Weeks the students can see: any week that has a scheduled open time
    (future or passed-but-not-yet-opened) or is currently live."""
    out = []
    for s in load_sets():
        w = _week_state(s.id)
        if w.get("open_at") or w.get("active"):
            out.append(s)
    return out

def _week_open_info(week_id: str):
    """Return (open_at_epoch, status) for a week.

    status:
      "none"   -> no schedule set yet
      "locked" -> scheduled for the future (or scheduled time passed but the
                  quiz hasn't actually opened yet) -> students see the date
      "open"   -> a live session is currently running
    """
    w = _week_state(week_id)
    open_at = w.get("open_at")
    if open_at is None:
        return None, "none"
    if w.get("active"):
        return open_at, "open"
    # scheduled but not yet opened (even if the time passed, the teacher
    # hasn't opened it) -> show the locked message with the scheduled date
    return open_at, "locked"

def _record_score(week_id: str, user_id: int, name: str, correct: int, wrong: int,
                  points: int, taken: bool) -> None:
    w = _week_state(week_id)
    uid = str(user_id)
    prev = w["scores"].get(uid, {"name": name, "score": 0, "correct": 0, "wrong": 0, "taken": False})
    prev["name"] = name
    prev["score"] = prev.get("score", 0) + points
    prev["correct"] = prev.get("correct", 0) + correct
    prev["wrong"] = prev.get("wrong", 0) + wrong
    prev["taken"] = prev.get("taken", False) or taken
    w["scores"][uid] = prev
    _save_state()

# ---------------------------------------------------------------------------
# Active quiz sessions (in-memory, per week)
# ---------------------------------------------------------------------------
SESSIONS: Dict[str, "game.QuizSession"] = {}  # week_id -> session

def _session_for(week_id: str) -> Optional["game.QuizSession"]:
    s = SESSIONS.get(week_id)
    if s and not s.finished:
        return s
    return None

# ---------------------------------------------------------------------------
# Admin helper
# ---------------------------------------------------------------------------
def _is_admin(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    if user_id in ADMIN_IDS:
        return True
    try:
        member = context.bot.get_chat_member(chat_id, user_id)
        return member.status in ("creator", "administrator")
    except Exception:
        return False

def _sets() -> list:
    return load_sets()

# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------
def _week_buttons(weeks: list, prefix: str) -> list:
    return [InlineKeyboardButton(f"📅 {s.title}", callback_data=f"{prefix}{s.id}") for s in weeks]

def _student_start_keyboard() -> InlineKeyboardMarkup:
    weeks = _available_weeks()
    if not weeks:
        return InlineKeyboardMarkup([[]])
    grid = [[b] for b in _week_buttons(weeks, "stwk:")]
    return InlineKeyboardMarkup(grid)

def _teacher_start_keyboard() -> InlineKeyboardMarkup:
    grid = [
        [InlineKeyboardButton("📚 افتح الكويز", callback_data="teacher_open")],
        [InlineKeyboardButton("📅 حدد موعد", callback_data="teacher_schedule")],
        [InlineKeyboardButton("▶️ ابدأ فوري (اختبار)", callback_data="teacher_startnow")],
    ]
    return InlineKeyboardMarkup(grid)

def _week_panel_keyboard(week_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📖 اشوف الأسئلة والإجابات", callback_data=f"qview:{week_id}")],
        [InlineKeyboardButton("📊 اشوف درجات الطلاب", callback_data=f"scores:{week_id}")],
        [InlineKeyboardButton("📅 حدد المعاد للطلاب", callback_data=f"schwk:{week_id}")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="teacher_open")],
    ])

def _student_week_keyboard(week_id: str) -> InlineKeyboardMarkup:
    open_at, status = _week_open_info(week_id)
    # Always show the "ادخل على الكويز" button; if the quiz is locked the
    # student gets the scheduled date when they tap it.
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧪 ادخل على الكويز", callback_data=f"stenter:{week_id}")],
        [InlineKeyboardButton("📊 اشوف درجتي", callback_data=f"stscore:{week_id}")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="stback")],
    ])

def _keyboard(q: Question, extra: Optional[list] = None) -> InlineKeyboardMarkup:
    """One button per option (A/B/C/D icons) + a SEND confirm.

    `extra` lets a caller append a full-width button row (e.g. an
    "إنهاء الاختبار" button used in test mode).
    """
    pick_buttons = [
        InlineKeyboardButton(f"{LETTERS[i]}. {opt}", callback_data=f"pick:{i}")
        for i, opt in enumerate(q.options)
    ]
    grid = [pick_buttons[i:i + 2] for i in range(0, len(pick_buttons), 2)]
    grid.append([InlineKeyboardButton("✅ إرسال إجابتي", callback_data="send")])
    if extra:
        grid.extend(extra)
    return InlineKeyboardMarkup(grid)

# ---------------------------------------------------------------------------
# Locked / schedule message helpers
# ---------------------------------------------------------------------------
def _lock_message(week_id: str) -> str:
    open_at, status = _week_open_info(week_id)
    if open_at is None:
        return "🔒 *الكويز ده لسه متحددش له موعد.* لما المدرّس يحدد المعاد هيظهر ليك."
    dt = datetime_from_ts(open_at)
    dn = AR_DAYS.get(dt.strftime("%A"), dt.strftime("%A"))
    return (
        f"🔒 *الكويز ده متاحش دلوقتي.*\n"
        f"📅 معاده: *{dn} {fmt_date_ar(dt)}* الساعة *{fmt_12h(dt)}*.\n"
        f"استنى لحد ما يفتح عشان تدخل تمتحن 💪"
    )

def datetime_from_ts(ts: float):
    """Convert epoch seconds to a timezone-aware Egypt-time datetime."""
    return _dt.datetime.fromtimestamp(ts, tz=EGYPT_TZ)

# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user = update.effective_user
    is_teacher = _is_admin(context, chat_id, user.id)

    if is_teacher:
        text = (
            "👋 *أهلاً يا أستاذ!*\n\n"
            "ده بوت كويز الصف الأول الثانوي (مادة البرمجة).\n"
            "تقدر تفتح الكويز، تحدد موعده، أو تبدأ اختبار فوري.\n\n"
            "يلا نبدأ! 👇"
        )
        kb = _teacher_start_keyboard()
    else:
        weeks = _available_weeks()
        if not weeks:
            text = (
                "👋 *أهلاً بيك في بوت الكويز!*\n\n"
                "📭 مفيش كويز متاح دلوقتي.\n"
                "استنى المدرّس يفتح أو يحدد موعد كويز وهتلاقيه هنا."
            )
            kb = InlineKeyboardMarkup([[]])
        else:
            text = (
                "👋 *أهلاً بيك في بوت الكويز!*\n\n"
                "📚 الكويزات المتاحة دلوقتي: اختار أسبوع عشان تدخل تمتحن "
                "أو تشوف درجتك.\n\nيلا! 👇"
            )
            kb = _student_start_keyboard()

    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)

# ---------------------------------------------------------------------------
# /sets  (teacher quick menu)
# ---------------------------------------------------------------------------
async def sets_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📚 *اختر الأسبوع اللي عايز تديره:*",
        parse_mode="Markdown",
        reply_markup=_week_grid_keyboard("wkpanel:"),
    )

def _week_grid_keyboard(prefix: str) -> InlineKeyboardMarkup:
    weeks = _sets()
    if not weeks:
        return InlineKeyboardMarkup([[]])
    grid = [[b] for b in _week_buttons(weeks, prefix)]
    return InlineKeyboardMarkup(grid)

# ---------------------------------------------------------------------------
# /schedule  (teacher) -> day -> time -> week
# ---------------------------------------------------------------------------
async def schedule_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user = update.effective_user
    if not _is_admin(context, chat_id, user.id):
        await update.message.reply_text("❌ ده للمدرّس بس.", parse_mode="Markdown")
        return
    args = context.args or []
    if args:
        # Parse /schedule 28/8 19:00 or /schedule 19:00
        time_str = None
        date_str = None
        for a in args[:2]:
            if re.match(r"^\d{1,2}:\d{2}$", a):
                time_str = a
            else:
                date_str = a
        if time_str:
            m = re.match(r"^(\d{1,2}):(\d{2})$", time_str)
            hh, mm = int(m.group(1)), int(m.group(2))
            if hh > 23 or mm > 59:
                await update.message.reply_text("❌ ساعة أو دقيقة غير صحيحة.", parse_mode="Markdown")
                return
            now_egypt = _dt.datetime.now(EGYPT_TZ)
            if date_str:
                dm = re.match(r"^(\d{1,2})[/-](\d{1,2})(?:[/-](\d{4}|\d{2}))?$", date_str)
                if not dm:
                    await update.message.reply_text(
                        "❌ مفهمتش التاريخ. اكتب مثلاً: `/schedule 28/8 19:00`",
                        parse_mode="Markdown")
                    return
                day, month, yr = int(dm.group(1)), int(dm.group(2)), now_egypt.year
                if dm.group(3):
                    yr = int(dm.group(3))
                    if yr < 100:
                        yr += 2000
                try:
                    then = _dt.datetime(yr, month, day, hh, mm, 0, tzinfo=EGYPT_TZ).astimezone(_dt.timezone.utc)
                except ValueError:
                    await update.message.reply_text("❌ تاريخ غير صحيح.", parse_mode="Markdown")
                    return
            else:
                then_egypt = now_egypt.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if then_egypt <= now_egypt:
                    then_egypt += _dt.timedelta(days=1)
                then = then_egypt.astimezone(_dt.timezone.utc)
            if then <= _dt.datetime.now(_dt.timezone.utc):
                await update.message.reply_text("❌ التاريخ ده عدّى.", parse_mode="Markdown")
                return
            # Need a week too — pick the first available or ask.
            weeks = _sets()
            if not weeks:
                await update.message.reply_text("❌ مفيش أسابيع متاحة.", parse_mode="Markdown")
                return
            # If more than one week, ask which one; otherwise schedule directly.
            if len(weeks) == 1:
                await _confirm_schedule(context, weeks[0].id, then, update.effective_chat.id)
            else:
                kb = InlineKeyboardMarkup(
                    [[InlineKeyboardButton(f"📅 {w.title}",
                                           callback_data=f"schwkarg:{w.id}:{int(then.timestamp())}")]
                     for w in weeks])
                await update.message.reply_text(
                    "📚 اختر الأسبوع اللي تحطّله الموعد:",
                    parse_mode="Markdown", reply_markup=kb)
            return
    # No args -> day picker
    await _show_day_picker(update, context)

async def _show_day_picker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    now_egypt = _dt.datetime.now(EGYPT_TZ)
    buttons = []
    for d in range(0, 7):
        day = now_egypt + _dt.timedelta(days=d)
        label = f"{AR_DAYS.get(day.strftime('%A'), day.strftime('%A'))} {day.day} {AR_MONTHS.get(day.strftime('%B'), day.strftime('%B'))}"
        buttons.append(InlineKeyboardButton(label, callback_data=f"schday:{day.year}-{day.month:02d}-{day.day:02d}"))
    grid = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    kb = InlineKeyboardMarkup(grid)
    text = "📅 *اختر يوم الكويز:*"
    if update.message:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
    else:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)

async def _show_time_picker(update: Update, context: ContextTypes.DEFAULT_TYPE, day_iso: str) -> None:
    hours = [15, 16, 17, 18, 19, 20, 21, 22]
    buttons = [InlineKeyboardButton(f"{h:02d}:00", callback_data=f"schtime:{day_iso}:{h:02d}:00") for h in hours]
    grid = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    grid.append([InlineKeyboardButton("🔙 رجوع للأيام", callback_data="schback")])
    kb = InlineKeyboardMarkup(grid)
    await update.callback_query.edit_message_text("🕐 *اختر ساعة الكويز:*", parse_mode="Markdown", reply_markup=kb)

# Schedule draft state, keyed by the teacher's chat id (safe — no reliance on
# arbitrary attributes on CallbackContext).
SCHEDULE_DRAFT: Dict[int, dict] = {}

async def _confirm_schedule(context: ContextTypes.DEFAULT_TYPE, week_id: str, then, chat_id: int) -> None:
    epoch = then.timestamp()
    w = _week_state(week_id)
    w["open_at"] = epoch
    w["set_id"] = week_id
    w["active"] = False
    _save_state()
    SCHEDULE_DRAFT.pop(chat_id, None)

    # Display times in Egypt timezone for the teacher
    then_egypt = then.astimezone(EGYPT_TZ)
    dn = AR_DAYS.get(then_egypt.strftime("%A"), then_egypt.strftime("%A"))
    wait = epoch - time.time()

    # Tell the teacher
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"✅ *تم تحديد موعد الكويز* ('{get_set(week_id).title if get_set(week_id) else week_id}')\n"
            f"📅 يوم *{dn} {fmt_date_ar(then_egypt)}* الساعة *{fmt_12h(then_egypt)}*\n"
            f"⏳ يفضل بعد {int(wait // 60)} دقيقة.\n"
            f"الطلاب هيلاقوه في /start لما يفتح."
        ),
        parse_mode="Markdown",
    )

    # Schedule the open job
    context.job_queue.run_once(
        _open_scheduled,
        wait,
        data={"week_id": week_id},
        name=f"open:{week_id}",
    )

# ---------------------------------------------------------------------------
# Open a scheduled quiz for students
# ---------------------------------------------------------------------------
async def _open_scheduled(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    week_id = job.data["week_id"]
    w = _week_state(week_id)
    qset = get_set(week_id) or (load_sets()[0] if load_sets() else None)
    if qset is None:
        return
    for j in context.job_queue.get_jobs_by_name(f"reveal:{week_id}"):
        j.schedule_removal()
    session = game.QuizSession(qset)
    session.week_id = week_id
    session.q_nonce = 0
    SESSIONS[week_id] = session
    w["active"] = True
    w["set_id"] = week_id
    _save_state()
    await _post_question(context, week_id)
    _schedule_close(context, week_id)

# ---------------------------------------------------------------------------
# /startnow  (teacher) -> week picker -> opens in TEST MODE for the teacher
# ---------------------------------------------------------------------------
async def startnow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user = update.effective_user
    if not _is_admin(context, chat_id, user.id):
        await update.message.reply_text("❌ ده للمدرّس بس.", parse_mode="Markdown")
        return
    weeks = _sets()
    if not weeks:
        await update.message.reply_text("❌ مفيش كويز متاح.", parse_mode="Markdown")
        return
    grid = [[b] for b in _week_buttons(weeks, "nowopen:")]
    await update.message.reply_text(
        "▶️ *ابدأ فوري (اختبار)* — اختر الأسبوع:",
        parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(grid))

async def _open_test_mode(context: ContextTypes.DEFAULT_TYPE, week_id: str, chat_id: int) -> None:
    """Open a quiz in TEST MODE: the teacher chat itself receives the questions
    with option buttons so the quiz can be tested end-to-end."""
    qset = get_set(week_id)
    if qset is None:
        await context.bot.send_message(chat_id=chat_id, text="❌ الأسبوع ده مش موجود.")
        return
    for j in context.job_queue.get_jobs_by_name(f"reveal:{week_id}"):
        j.schedule_removal()
    session = game.QuizSession(qset)
    session.week_id = week_id
    session.q_nonce = 0
    session.test_mode = True
    session.test_chat = chat_id
    SESSIONS[week_id] = session

    w = _week_state(week_id)
    w["active"] = True
    w["set_id"] = week_id
    _save_state()

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"▶️ *بدأ الكويز فوراً (اختبار): {qset.title}*\n"
            f"عدد الأسئلة: {len(qset.questions)}\n"
            f"الوقت لكل سؤال: {session.timer} ثانية.\n\n"
            f"الأسئلة هتظهرلك بأزرار الخيارات (A/B/C/D) — اختار إجابتك واضغط '✅ إرسال إجابتي'.\n"
            f"⏳ الكويز مفتوح لمدة 30 دقيقة وبعدها يقفل أوتوماتيك."
        ),
        parse_mode="Markdown",
    )
    await _post_question(context, week_id)
    _schedule_close(context, week_id)

# ---------------------------------------------------------------------------
# Question posting
# ---------------------------------------------------------------------------
async def _post_question(context: ContextTypes.DEFAULT_TYPE, week_id: str) -> None:
    session = SESSIONS.get(week_id)
    if not session or session.finished:
        return
    q = session.current
    if q is None:
        await _finish(context, week_id)
        return

    session.q_nonce = getattr(session, "q_nonce", 0) + 1
    session.answered = set()
    session.pending = {}
    session.question_start = time.time()

    # In test mode, deliver the question to the teacher's chat with option buttons.
    if getattr(session, "test_mode", False) and session.test_chat:
        # Add an "إنهاء الاختبار" button so the admin can stop it early.
        end_btn = [[InlineKeyboardButton("🔴 إنهاء الاختبار", callback_data="endtest")]]
        await context.bot.send_message(
            chat_id=session.test_chat,
            text=_question_text(q, session.index, len(session.set.questions)),
            parse_mode="Markdown",
            reply_markup=_keyboard(q, extra=end_btn),
        )

    # Also notify any student who joined (waiting) so they can answer.
    for uid in list(getattr(session, "waiting", set())):
        try:
            await context.bot.send_message(
                chat_id=uid,
                text=_question_text(q, session.index, len(session.set.questions)) +
                     "\n\n" + _options_block(q) +
                     "\n\nاختار إجابتك واضغط '✅ إرسال إجابتي'.",
                parse_mode="Markdown",
            )
        except Exception:
            pass

    context.job_queue.run_once(
        _reveal_and_next,
        session.timer,
        data={"week_id": week_id, "nonce": session.q_nonce},
        name=f"reveal:{week_id}",
    )

def _question_text(q: Question, idx: int, total: int) -> str:
    return f"❓ *سؤال {idx + 1} من {total}*\n\n{q.q}"

def _options_block(q: Question) -> str:
    return "\n".join(f"{LETTERS[i]}. {opt}" for i, opt in enumerate(q.options))

async def _reveal_and_next(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    week_id = job.data["week_id"]
    nonce = job.data["nonce"]
    session = SESSIONS.get(week_id)
    if not session or session.finished or getattr(session, "q_nonce", -1) != nonce:
        return

    q = session.current
    correct_letter = LETTERS[q.correct]
    reveal = (
        f"⏰ *انتهى وقت السؤال {session.index + 1}!*\n"
        f"✅ الإجابة الصحيحة: *{correct_letter}. {q.correct_text()}*\n"
    )
    if q.explanation:
        reveal += f"💡 {q.explanation}\n"

    # In test mode, reveal goes to the teacher chat.
    if getattr(session, "test_mode", False) and session.test_chat:
        await context.bot.send_message(chat_id=session.test_chat, text=reveal, parse_mode="Markdown")

    for uid in list(session.pending.keys()):
        try:
            await context.bot.send_message(chat_id=uid, text=reveal, parse_mode="Markdown")
        except Exception:
            pass

    if session.is_last:
        await _finish(context, week_id)
        return

    session.next()
    await asyncio.sleep(1.2)
    await _post_question(context, week_id)

# ---------------------------------------------------------------------------
# Student enter quiz
# ---------------------------------------------------------------------------
async def _student_enter(context: ContextTypes.DEFAULT_TYPE, week_id: str, chat_id: int) -> None:
    open_at, status = _week_open_info(week_id)
    if status == "locked":
        await context.bot.send_message(chat_id=chat_id, text=_lock_message(week_id), parse_mode="Markdown")
        return
    if status == "none":
        await context.bot.send_message(chat_id=chat_id,
                                       text="🔒 الكويز ده لسه متحددش له موعد.", parse_mode="Markdown")
        return

    # Open -> ensure a session exists; if not, create one now (teacher didn't open yet but time passed)
    session = _session_for(week_id)
    if session is None:
        qset = get_set(week_id)
        if qset is None:
            await context.bot.send_message(chat_id=chat_id, text="❌ الأسبوع ده مش موجود.", parse_mode="Markdown")
            return
        for j in context.job_queue.get_jobs_by_name(f"reveal:{week_id}"):
            j.schedule_removal()
        session = game.QuizSession(qset)
        session.week_id = week_id
        session.q_nonce = 0
        SESSIONS[week_id] = session
        w = _week_state(week_id)
        w["active"] = True
        _save_state()
        await _post_question(context, week_id)
        _schedule_close(context, week_id)

    q = session.current
    if q is None:
        await context.bot.send_message(chat_id=chat_id, text="❌ الكويز انتهى.", parse_mode="Markdown")
        return

    # --- NEW: warn students who joined late that earlier questions were skipped ---
    total_questions = len(session.set.questions)
    questions_skipped = session.index  # how many questions the session already passed
    if questions_skipped > 0:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"⚠️ *لقيت الكويز شغال — فاتك {questions_skipped} أسئلة!*\n"
                f"السؤال الحالي: *{session.index + 1} من {total_questions}*\n\n"
                f"❓ *سؤال {session.index + 1} من {total_questions}*\n\n{q.q}"
            ),
            parse_mode="Markdown",
            reply_markup=_keyboard(q),
        )
        return

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"{_question_text(q, session.index, len(session.set.questions))}\n\n"
             f"{_options_block(q)}\n\nاختار إجابتك واضغط '✅ إرسال إجابتي'.",
        parse_mode="Markdown",
        reply_markup=_keyboard(q),
    )
    session.pending[chat_id] = {"pick": None, "msg_id": None}

# ---------------------------------------------------------------------------
# Student score
# ---------------------------------------------------------------------------
async def _student_score(context: ContextTypes.DEFAULT_TYPE, week_id: str, chat_id: int) -> None:
    w = _week_state(week_id)
    rec = w["scores"].get(str(chat_id))
    qset = get_set(week_id)
    total = len(qset.questions) if qset else 0
    if not rec:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"📊 *درجتك في {qset.title if qset else week_id}*\n\n❌ لسه ما دخلتش الكويز ده.",
            parse_mode="Markdown",
        )
        return
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"📊 *درجتك في {qset.title if qset else week_id}*\n\n"
            f"⭐ جبت: *{rec['score']}* من {total} سؤال\n"
            f"✅ صح: *{rec['correct']}*   ❌ غلط: *{rec['wrong']}*"
        ),
        parse_mode="Markdown",
    )

# ---------------------------------------------------------------------------
# Teacher views
# ---------------------------------------------------------------------------
async def _teacher_qview(context: ContextTypes.DEFAULT_TYPE, week_id: str, chat_id: int) -> None:
    qset = get_set(week_id)
    if qset is None:
        await context.bot.send_message(chat_id=chat_id, text="❌ الأسبوع ده مش موجود.")
        return
    lines = [f"📖 *أسئلة وإجابات — {qset.title}*\n"]
    for i, q in enumerate(qset.questions, 1):
        lines.append(f"*{i}. {q.q}*")
        for j, opt in enumerate(q.options):
            mark = " ✅" if j == q.correct else ""
            lines.append(f"   {LETTERS[j]}. {opt}{mark}")
        if q.explanation:
            lines.append(f"   💡 {q.explanation}")
        lines.append("")
    text = "\n".join(lines)
    # Telegram message size limit ~4096; split if needed.
    for chunk in _split(text):
        await context.bot.send_message(chat_id=chat_id, text=chunk, parse_mode="Markdown")

def _split(text: str, limit: int = 3900):
    if len(text) <= limit:
        return [text]
    parts = []
    cur = ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit:
            parts.append(cur)
            cur = line
        else:
            cur = (cur + "\n" + line).strip()
    if cur:
        parts.append(cur)
    return parts

async def _teacher_scores(context: ContextTypes.DEFAULT_TYPE, week_id: str, chat_id: int) -> None:
    w = _week_state(week_id)
    if not w["scores"]:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"📊 *درجات الطلاب — {get_set(week_id).title if get_set(week_id) else week_id}*\n\n❌ لسه مفيش طالب دخل الكويز ده.",
            parse_mode="Markdown",
        )
        return
    ranked = sorted(w["scores"].items(), key=lambda kv: kv[1]["score"], reverse=True)
    total = len(get_set(week_id).questions) if get_set(week_id) else 0
    lines = [f"📊 *درجات الطلاب — {get_set(week_id).title}* (من {total} سؤال)\n"]
    for i, (uid, rec) in enumerate(ranked, 1):
        lines.append(f"{i}. {rec['name']} — ⭐ {rec['score']} (✅{rec['correct']}/❌{rec['wrong']})")
    await context.bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode="Markdown")

# ---------------------------------------------------------------------------
# Answer handling (pick / send) — used by both test mode (teacher) and students
# ---------------------------------------------------------------------------
async def _handle_pick(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str) -> None:
    query = update.callback_query
    user = update.effective_user
    chat_id = update.effective_chat.id
    # Find which week this chat is answering:
    # - test mode: the teacher's chat maps to session.test_chat
    # - student: their own chat id keys the pending dict
    week_id = _week_for_chat(chat_id)
    if week_id is None:
        await query.answer("❌ مفيش كويز شغال ليك.", show_alert=True)
        return
    session = SESSIONS.get(week_id)
    if not session or session.finished:
        await query.answer("❌ الكويز انتهى.", show_alert=True)
        return
    try:
        chosen = int(data.split(":", 1)[1])
    except (ValueError, IndexError):
        return
    pend = session.pending.get(chat_id)
    if pend is None:
        session.pending[chat_id] = {"pick": chosen, "msg_id": None}
    else:
        pend["pick"] = chosen
    q = session.current
    # keep the "إنهاء الاختبار" button in test mode
    extra = [[InlineKeyboardButton("🔴 إنهاء الاختبار", callback_data="endtest")]] \
        if getattr(session, "test_mode", False) else None
    await query.edit_message_text(
        f"{_question_text(q, session.index, len(session.set.questions))}\n\n"
        f"{_options_block(q)}\n\n"
        f"🟢 اختارتي: *{LETTERS[chosen]}. {q.options[chosen]}*\n"
        f"اضغط '✅ إرسال إجابتي' للتأكيد.",
        parse_mode="Markdown",
        reply_markup=_keyboard(q, extra=extra),
    )

def _week_for_chat(chat_id: int) -> Optional[str]:
    """Find the week whose active session this chat is participating in."""
    for wid, s in SESSIONS.items():
        if s.finished:
            continue
        if getattr(s, "test_mode", False) and s.test_chat == chat_id:
            return wid
        if chat_id in s.pending or chat_id in getattr(s, "answered", set()) or chat_id in getattr(s, "waiting", set()):
            return wid
    return None

async def _handle_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat_id = update.effective_chat.id
    week_id = _week_for_chat(chat_id)
    if week_id is None:
        await query.answer("❌ مفيش كويز شغال ليك.", show_alert=True)
        return
    session = SESSIONS.get(week_id)
    if not session or session.finished:
        await query.answer("❌ الكويز انتهى.", show_alert=True)
        return
    pend = session.pending.get(chat_id)
    if not pend or pend["pick"] is None:
        await query.answer("❗ اختار إجابة أولاً", show_alert=True)
        return
    if chat_id in getattr(session, "answered", set()):
        await query.answer("✅ أنت أجبت على السؤال ده", show_alert=True)
        return

    chosen = pend["pick"]
    is_correct = (chosen == session.current.correct)
    q = session.current
    name = update.effective_user.full_name or update.effective_user.first_name or "طالب"
    uid = update.effective_user.id

    # Record: 1 point if correct, track correct/wrong counts
    _record_score(
        week_id, uid, name,
        correct=1 if is_correct else 0,
        wrong=0 if is_correct else 1,
        points=1 if is_correct else 0,
        taken=True,
    )
    session.answered.add(chat_id)

    if is_correct:
        result = f"✅ *صح!* +1 نقطة\nالإجابة: {LETTERS[q.correct]}. {q.correct_text()}"
    else:
        result = (f"❌ *غلط*\nإجابتك: {LETTERS[chosen]}. {q.options[chosen]}\n"
                  f"الصحيحة: {LETTERS[q.correct]}. {q.correct_text()}")
    if q.explanation:
        result += f"\n💡 {q.explanation}"

    session.pending.pop(chat_id, None)
    await query.edit_message_text(
        f"{result}\n\n📊 درجتك الحالية في الأسبوع: "
        f"*{_week_state(week_id)['scores'].get(str(uid), {}).get('score', 0)}* نقطة",
        parse_mode="Markdown",
    )

# ---------------------------------------------------------------------------
# Auto-close
# ---------------------------------------------------------------------------
def _schedule_close(context: ContextTypes.DEFAULT_TYPE, week_id: str) -> None:
    for j in context.job_queue.get_jobs_by_name(f"close:{week_id}"):
        j.schedule_removal()
    context.job_queue.run_once(_auto_close, QUIZ_OPEN_SECONDS,
                               data={"week_id": week_id}, name=f"close:{week_id}")

async def _auto_close(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    week_id = job.data["week_id"]
    session = SESSIONS.get(week_id)
    if session and not session.finished:
        await _finish(context, week_id)
    w = _week_state(week_id)
    w["active"] = False
    _save_state()

async def _finish(context: ContextTypes.DEFAULT_TYPE, week_id: str) -> None:
    session = SESSIONS.get(week_id)
    if not session:
        return
    session.finished = True
    w = _week_state(week_id)
    w["active"] = False
    _save_state()

    # In test mode, just tell the teacher it's done (with their own tally).
    if getattr(session, "test_mode", False) and session.test_chat:
        rec = _week_state(week_id)["scores"].get(str(session.test_chat), {})
        await context.bot.send_message(
            chat_id=session.test_chat,
            text=f"🎉 *انتهى الكويز (اختبار): {session.set.title}*\n\n"
                 f"⭐ درجتك في الاختبار: *{rec.get('score', 0)}* نقطة "
                 f"(✅ {rec.get('correct', 0)} / ❌ {rec.get('wrong', 0)})",
            parse_mode="Markdown",
        )
    SESSIONS.pop(week_id, None)

# ---------------------------------------------------------------------------
# /help, /myscore, /myid
# ---------------------------------------------------------------------------
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "*طريقة اللعب:*\n"
        "1) المدرّس يحدد موعد الكويز أو يبدأ فوري.\n"
        "2) لما يفتح، ادخل على الأسبوع من /start.\n"
        "3) تختار إجابتك (A/B/C/D) وتضغط '✅ إرسال إجابتي'.\n"
        "4) تظهرلك النتيجة فوراً: صح = نقطة.\n"
        "5) في أي وقت تقدر تشوف درجتك من زرار '📊 اشوف درجتي'."
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def myscore_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat_id = update.effective_chat.id
    # Show the most recent week the user has a score in (or the open one)
    for s in _sets():
        rec = _week_state(s.id)["scores"].get(str(user.id))
        if rec:
            await _student_score(context, s.id, chat_id)
            return
    await update.message.reply_text(
        "❌ مفيش درجة مسجلة ليك لسه. لما تدخل كويز وترد، اكتب /myscore.",
        parse_mode="Markdown")

async def myid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await update.message.reply_text(
        f"🆔 رقمك على تليجرام: `{user.id}`\nالاسم: {user.first_name}\n\n"
        "حط الرقم ده في ملف .env قدام ADMIN_IDS= عشان تبقى مدرّس البوت.",
        parse_mode="Markdown")

# ---------------------------------------------------------------------------
# /join (kept for compatibility; opens the week picker for the student)
# ---------------------------------------------------------------------------
async def join_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user = update.effective_user
    if _is_admin(context, chat_id, user.id):
        await update.message.reply_text(
            "📚 اختر الأسبوع:", reply_markup=_week_grid_keyboard("wkpanel:"),
            parse_mode="Markdown")
        return
    weeks = _available_weeks()
    if not weeks:
        await update.message.reply_text("❌ مفيش كويز متاح دلوقتي.", parse_mode="Markdown")
        return
    await update.message.reply_text(
        "📚 اختر الأسبوع:", reply_markup=_student_start_keyboard(),
        parse_mode="Markdown")

# ---------------------------------------------------------------------------
# Main callback router
# ---------------------------------------------------------------------------
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    chat_id = update.effective_chat.id

    # ----- Teacher top menu -----
    if data == "teacher_open":
        await query.edit_message_text(
            "📚 *اختر الأسبوع اللي عايز تديره:*",
            parse_mode="Markdown", reply_markup=_week_grid_keyboard("wkpanel:"))
        return

    if data == "teacher_schedule":
        # jump straight to the day picker (will ask week after time)
        await _show_day_picker(update, context)
        return

    if data == "teacher_startnow":
        weeks = _sets()
        if not weeks:
            await query.edit_message_text("❌ مفيش كويز متاح.")
            return
        grid = [[b] for b in _week_buttons(weeks, "nowopen:")]
        await query.edit_message_text(
            "▶️ *ابدأ فوري (اختبار)* — اختر الأسبوع:",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(grid))
        return

    # ----- Week panel (teacher) -----
    if data.startswith("wkpanel:"):
        week_id = data.split(":", 1)[1]
        await query.edit_message_text(
            f"🛠️ *إدارة: {get_set(week_id).title if get_set(week_id) else week_id}*",
            parse_mode="Markdown", reply_markup=_week_panel_keyboard(week_id))
        return

    if data.startswith("qview:"):
        week_id = data.split(":", 1)[1]
        await query.edit_message_text("📖 بقرأ أسئلة الأسبوع…", parse_mode="Markdown")
        await _teacher_qview(context, week_id, chat_id)
        return

    if data.startswith("scores:"):
        week_id = data.split(":", 1)[1]
        await query.edit_message_text("📊 بقرأ درجات الطلاب…", parse_mode="Markdown")
        await _teacher_scores(context, week_id, chat_id)
        return

    if data.startswith("schwk:"):
        week_id = data.split(":", 1)[1]
        # remember the week + chat for the upcoming day/time pickers
        SCHEDULE_DRAFT[chat_id] = {"week_id": week_id}
        await _show_day_picker(update, context)
        return

    if data.startswith("nowopen:"):
        week_id = data.split(":", 1)[1]
        await _open_test_mode(context, week_id, chat_id)
        return

    # ----- Schedule flow (day -> time -> week) -----
    if data == "schback":
        await _show_day_picker(update, context)
        return

    if data.startswith("schday:"):
        day_iso = data.split(":", 1)[1]
        await _show_time_picker(update, context, day_iso)
        return

    if data.startswith("schtime:"):
        _, day_iso, clock = data.split(":", 2)
        yr, mo, da = map(int, day_iso.split("-"))
        hh, mm = map(int, clock.split(":"))
        then = _dt.datetime(yr, mo, da, hh, mm, 0, tzinfo=EGYPT_TZ).astimezone(_dt.timezone.utc)
        if then <= _dt.datetime.now(_dt.timezone.utc):
            await query.answer("❌ التاريخ ده عدّى", show_alert=True)
            return
        draft = SCHEDULE_DRAFT.get(chat_id, {})
        week_id = draft.get("week_id")
        if week_id:
            await _confirm_schedule(context, week_id, then, chat_id)
        else:
            # fall back: ask which week
            kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton(f"📅 {w.title}",
                                       callback_data=f"schwkarg:{w.id}:{int(then.timestamp())}")]
                 for w in _sets()])
            await query.edit_message_text("📚 اختر الأسبوع:", parse_mode="Markdown", reply_markup=kb)
        return

    if data.startswith("schwkarg:"):
        # schwkarg:week_id:epoch
        _, week_id, epoch = data.split(":", 2)
        then = _dt.datetime.fromtimestamp(int(epoch), tz=_dt.timezone.utc)
        await _confirm_schedule(context, week_id, then, chat_id)
        return

    # ----- Student flow -----
    if data == "stback":
        weeks = _available_weeks()
        if not weeks:
            await query.edit_message_text("📭 مفيش كويز متاح دلوقتي.")
            return
        await query.edit_message_text(
            "📚 الكويزات المتاحة:", parse_mode="Markdown",
            reply_markup=_student_start_keyboard())
        return

    if data.startswith("stwk:"):
        week_id = data.split(":", 1)[1]
        title = get_set(week_id).title if get_set(week_id) else week_id
        open_at, status = _week_open_info(week_id)
        if status == "locked":
            dt = datetime_from_ts(open_at)
            dn = AR_DAYS.get(dt.strftime("%A"), dt.strftime("%A"))
            sched = f"\n🔒 *مقفول دلوقتي* — معاده: *{dn} {fmt_date_ar(dt)}* الساعة *{fmt_12h(dt)}*"
        elif status == "open":
            sched = "\n🟢 *مفتوح دلوقتي* — تقدر تدخل تمتحن!"
        else:
            sched = "\n⏳ *لسه متحددش له موعد.*"
        await query.edit_message_text(
            f"📅 *{title}*{sched}",
            parse_mode="Markdown", reply_markup=_student_week_keyboard(week_id))
        return

    if data.startswith("stenter:"):
        week_id = data.split(":", 1)[1]
        await query.edit_message_text("🧪 بفتح الكويز…", parse_mode="Markdown")
        await _student_enter(context, week_id, chat_id)
        return

    if data.startswith("stscore:"):
        week_id = data.split(":", 1)[1]
        await query.edit_message_text("📊 بقرأ درجتك…", parse_mode="Markdown")
        await _student_score(context, week_id, chat_id)
        return

    # ----- Answer buttons (test mode + students) -----
    if data == "endtest":
        # Admin ends the test-mode quiz immediately, even mid-question.
        chat_id = update.effective_chat.id
        week_id = _week_for_chat(chat_id)
        if week_id is None:
            await query.answer("❌ مفيش اختبار شغال.", show_alert=True)
            return
        session = SESSIONS.get(week_id)
        if session is None or not getattr(session, "test_mode", False):
            await query.answer("❌ ده مش اختبار.", show_alert=True)
            return
        if not _is_admin(context, chat_id, update.effective_user.id):
            await query.answer("❌ ده للمدرّس بس.", show_alert=True)
            return
        # remove pending timers/jobs for this week
        for j in context.job_queue.get_jobs_by_name(f"reveal:{week_id}"):
            j.schedule_removal()
        for j in context.job_queue.get_jobs_by_name(f"close:{week_id}"):
            j.schedule_removal()
        await query.edit_message_text(
            f"🔴 *اتإنهى الاختبار* ({session.set.title})\n\n"
            f"📊 درجتك في الاختبار: *{_week_state(week_id)['scores'].get(str(chat_id), {}).get('score', 0)}* نقطة "
            f"(✅ {_week_state(week_id)['scores'].get(str(chat_id), {}).get('correct', 0)} / "
            f"❌ {_week_state(week_id)['scores'].get(str(chat_id), {}).get('wrong', 0)})",
            parse_mode="Markdown",
        )
        await _finish(context, week_id)
        return

    if data.startswith("pick:"):
        await _handle_pick(update, context, data)
        return

    if data == "send":
        await _handle_send(update, context)
        return

# ---------------------------------------------------------------------------
# Text fallback (student types A/B/C/D)
# ---------------------------------------------------------------------------
async def message_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != "private":
        return
    text = (update.message.text or "").strip().upper()
    mapping = {L: i for i, L in enumerate(LETTERS)}
    if text and text[0] in mapping:
        chat_id = update.effective_chat.id
        week_id = _week_for_chat(chat_id)
        if week_id is None:
            return
        # simulate a pick then send
        session = SESSIONS.get(week_id)
        if session and not session.finished:
            session.pending.setdefault(chat_id, {"pick": None, "msg_id": None})
            session.pending[chat_id]["pick"] = mapping[text[0]]
            # fake a callback-like update is hard; instead directly score
            await _handle_send_text(update, context, week_id, mapping[text[0]])

async def _handle_send_text(update: Update, context: ContextTypes.DEFAULT_TYPE, week_id: str, chosen: int) -> None:
    chat_id = update.effective_chat.id
    session = SESSIONS.get(week_id)
    if not session or session.finished:
        return
    if chat_id in getattr(session, "answered", set()):
        return
    is_correct = (chosen == session.current.correct)
    q = session.current
    name = update.effective_user.full_name or update.effective_user.first_name or "طالب"
    uid = update.effective_user.id
    _record_score(week_id, uid, name, correct=1 if is_correct else 0,
                  wrong=0 if is_correct else 1, points=1 if is_correct else 0, taken=True)
    session.answered.add(chat_id)
    if is_correct:
        result = f"✅ *صح!* +1 نقطة\nالإجابة: {LETTERS[q.correct]}. {q.correct_text()}"
    else:
        result = (f"❌ *غلط*\nالصحيحة: {LETTERS[q.correct]}. {q.correct_text()}")
    await update.message.reply_text(
        f"{result}\n\n📊 درجتك الحالية: "
        f"*{_week_state(week_id)['scores'].get(str(uid), {}).get('score', 0)}* نقطة",
        parse_mode="Markdown")

# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def start_handler():
    return CommandHandler("start", start)

def sets_handler():
    return CommandHandler("sets", sets_cmd)

def help_handler():
    return CommandHandler("help", help_cmd)

def myscore_handler():
    return CommandHandler("myscore", myscore_cmd)

def myid_handler():
    return CommandHandler("myid", myid_cmd)

def startnow_handler():
    return CommandHandler("startnow", startnow_cmd)

def schedule_handler():
    return CommandHandler("schedule", schedule_cmd)

def join_handler():
    return CommandHandler("join", join_cmd)

def answer_handler():
    return CommandHandler("answer", join_cmd)

def callback_handler_reg():
    return CallbackQueryHandler(callback_handler)

def fallback_handler():
    return MessageHandler(filters.TEXT & ~filters.COMMAND, message_fallback)

# Initialize state on import.
_load_state()
