#!/usr/bin/env python3
"""
Study Bot — a personal Telegram study assistant.

Features
--------
- Daily reminders that follow your exam-prep routine (Asia/Tashkent time).
- Pomodoro focus timer that pings you for work / break.
- Statistics: focus minutes and Pomodoro sessions, today and this week.
- One-tap buttons that open your Vocab Trainer and A-B Repeater
  *inside* Telegram as Mini Apps (works on phone and PC).

Everything is one file so it is easy to deploy. See README.md for setup.
"""

import os
import asyncio
import json
import sqlite3
import subprocess
import logging
import httpx
from datetime import time as dtime, datetime, date, timedelta
from zoneinfo import ZoneInfo

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    WebAppInfo,
    BotCommand,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# --------------------------------------------------------------------------- #
# Configuration (read from environment — see .env.example)
# --------------------------------------------------------------------------- #
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
TZ = ZoneInfo(os.environ.get("TZ", "Asia/Tashkent"))
DB_PATH = os.environ.get("DB_PATH", "study_bot.db")

# URLs to your two apps once you host them (HTTPS, e.g. GitHub Pages).
# Leave empty until you have them — the bot will explain what to do.
VOCAB_URL = os.environ.get("VOCAB_URL", "")
REPEATER_URL = os.environ.get("REPEATER_URL", "")

WORK_DEFAULT = 25   # Pomodoro work minutes
BREAK_DEFAULT = 5   # Pomodoro break minutes

# Exam date for the countdown (YYYY-MM-DD)
EXAM_DATE = os.environ.get("EXAM_DATE", "2026-08-28")


def days_to_exam():
    try:
        y, m, d = map(int, EXAM_DATE.split("-"))
        return (date(y, m, d) - datetime.now(TZ).date()).days
    except Exception:
        return None

# --- AI assistant (Groq, free tier, OpenAI-compatible) ---
AI_API_KEY = os.environ.get("AI_API_KEY", "")
AI_MODEL = os.environ.get("AI_MODEL", "llama-3.3-70b-versatile")
AI_BASE_URL = os.environ.get(
    "AI_BASE_URL", "https://api.groq.com/openai/v1/chat/completions"
)
AI_SYSTEM_PROMPT = (
    "Ты — дружелюбный помощник по учёбе для студента из Узбекистана, "
    "который готовится к экзамену по математике и физике (28 августа). "
    "Помогай понятно объяснять темы по математике и физике, проверять и "
    "улучшать английский и русский, давать подсказки к задачам (веди к "
    "решению, а не просто выдавай ответ) и поддерживай мотивацию. Отвечай "
    "кратко и ясно, на том языке, на котором пишет пользователь (по "
    "умолчанию по-русски)."
)
# Per-chat conversation history for the AI, in Gemini format.
AI_HISTORY: dict[int, list] = {}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("study-bot")

# In-memory Pomodoro state, keyed by chat_id.
POMO: dict[int, dict] = {}

# --------------------------------------------------------------------------- #
# Reminder schedule.  weekday(): Mon=0 ... Sun=6
# The plan lives in a plain text file (schedule.txt) you can edit in Notepad.
# Each entry: (hour, minute, {allowed weekdays}, message)
# --------------------------------------------------------------------------- #
SCHEDULE_FILE = os.environ.get("SCHEDULE_FILE", "schedule.txt")

LESSON_DAYS = {0, 1, 2, 3, 4, 5}   # Mon–Sat
SUNDAY = {6}

DAY_TOKENS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

# Built-in default plan, used the first time (also written to schedule.txt so
# you can edit it).
DEFAULT_SCHEDULE = [
    # Mon–Sat routine
    (6, 0, LESSON_DAYS, "☀️ Wake up. Wash, breakfast, pack your bag. Leave by 6:30."),
    (6, 30, LESSON_DAYS, "🚌 Commute — open English or Russian practice. Tap /apps for the Vocab Trainer."),
    (12, 30, LESSON_DAYS, "🚌 Heading home — start homework NOW while the lesson is fresh. Redo the class examples."),
    (15, 0, LESSON_DAYS, "🐔 Poultry care, then real rest."),
    (16, 0, LESSON_DAYS, "🏃 Fitness — move your body, then shower. It sharpens your evening focus."),
    (17, 30, LESSON_DAYS, "📚 Homework block 1 — finish the problem set. Try /pomodoro."),
    (19, 30, LESSON_DAYS, "🔥 Your peak hours. Hardest work now: finish homework, then drill weak fundamentals. /pomodoro"),
    (21, 30, LESSON_DAYS, "🔁 Light review — flashcards or a short dose of the other language. /apps"),
    (22, 0, LESSON_DAYS, "🌙 Wind down. Pack tomorrow's bag. No hard screens."),
    (22, 50, LESSON_DAYS, "😴 Sleep soon — you need 7 hours. Lights out by 23:00."),
    # Sunday (half rest, half study)
    (7, 30, SUNDAY, "🌤️ Slow Sunday. Hygiene, poultry, a good breakfast, rest."),
    (16, 0, SUNDAY, "📖 Catch-up study (2–3h): weak spots from the week + one timed practice test. /pomodoro"),
    (21, 30, SUNDAY, "✅ Wrap up and recharge for Monday. Sleep on time."),
]

# Filled in by load_schedule() at startup.
REMINDERS: list = []


def days_to_text(days: set) -> str:
    if days == set(range(7)):
        return "all"
    if days == LESSON_DAYS:
        return "mon-sat"
    if days == SUNDAY:
        return "sun"
    names = [n for n, i in sorted(DAY_TOKENS.items(), key=lambda x: x[1]) if i in days]
    return ",".join(names)


def parse_days(token: str) -> set:
    token = token.strip().lower()
    if token in ("all", "everyday", "every day"):
        return set(range(7))
    if token == "mon-sat":
        return LESSON_DAYS.copy()
    result: set = set()
    for part in token.split(","):
        part = part.strip()
        if "-" in part and part not in DAY_TOKENS:
            a, _, b = part.partition("-")
            a, b = a.strip(), b.strip()
            if a in DAY_TOKENS and b in DAY_TOKENS:
                result.update(range(DAY_TOKENS[a], DAY_TOKENS[b] + 1))
        elif part in DAY_TOKENS:
            result.add(DAY_TOKENS[part])
    return result or set(range(7))


def _parse_schedule_line(line: str):
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    parts = line.split("|")
    if len(parts) < 3:
        return None
    t = parts[0].strip()
    days = parse_days(parts[1])
    msg = "|".join(parts[2:]).strip()
    try:
        hh, mm = t.split(":")
        return (int(hh), int(mm), days, msg)
    except ValueError:
        return None


def parse_schedule_text(text: str) -> list:
    out = []
    for raw in text.splitlines():
        e = _parse_schedule_line(raw)
        if e:
            out.append(e)
    return out


def _serialize_schedule(entries: list) -> str:
    header = [
        "# ТВОЙ ПЛАН ДНЯ — меняй через /edit в Telegram или на GitHub + /reload.",
        "# Формат:   ЧЧ:ММ | дни | сообщение",
        "# дни: all, mon-sat, sun, или список вроде mon,wed,fri",
        "",
    ]
    lines = header + [
        f"{h:02d}:{m:02d} | {days_to_text(d)} | {msg}" for h, m, d, msg in entries
    ]
    return "\n".join(lines) + "\n"


def write_default_schedule(path: str):
    header = [
        "# YOUR DAILY PLAN — edit this file on GitHub, then send /reload in Telegram.",
        "# Format:   HH:MM | days | message",
        "# days can be:  all   mon-sat   sun   or a list like  mon,wed,fri",
        "# Lines starting with # are notes and are ignored.",
        "",
    ]
    lines = header + [
        f"{h:02d}:{m:02d} | {days_to_text(d)} | {msg}" for h, m, d, msg in DEFAULT_SCHEDULE
    ]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def load_schedule(path: str) -> list:
    if not os.path.exists(path):
        write_default_schedule(path)
        log.info("Wrote a starter plan to %s — edit it any time.", path)
        return list(DEFAULT_SCHEDULE)
    with open(path, encoding="utf-8") as f:
        out = [e for e in (_parse_schedule_line(l) for l in f) if e]
    return out or list(DEFAULT_SCHEDULE)

# --------------------------------------------------------------------------- #
# Database helpers
# --------------------------------------------------------------------------- #
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS users ("
            "chat_id INTEGER PRIMARY KEY, reminders_enabled INTEGER DEFAULT 1)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS focus_log ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, "
            "day TEXT, ts TEXT, minutes INTEGER)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS vocab ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, "
            "front TEXT, back TEXT, box INTEGER DEFAULT 0, due TEXT)"
        )


# Spaced-repetition intervals per box (days).
VOCAB_INTERVALS = [0, 1, 3, 7, 16, 30]


def vocab_add(chat_id: int, front: str, back: str, box=None, due=None) -> bool:
    front, back = front.strip(), back.strip()
    if not front or not back:
        return False
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    b = max(0, min(int(box), len(VOCAB_INTERVALS) - 1)) if box is not None else 0
    d = due or today
    with db() as conn:
        exists = conn.execute(
            "SELECT id FROM vocab WHERE chat_id=? AND front=? AND back=?",
            (chat_id, front, back),
        ).fetchone()
        if exists:
            if box is not None or due is not None:
                conn.execute(
                    "UPDATE vocab SET box=?, due=? WHERE id=?", (b, d, exists["id"])
                )
                return True
            return False
        conn.execute(
            "INSERT INTO vocab (chat_id, front, back, box, due) VALUES (?,?,?,?,?)",
            (chat_id, front, back, b, d),
        )
    return True


def vocab_import(chat_id: int, text: str) -> int:
    """Parse 'word <sep> translation' lines and add them. Returns count added."""
    seps = ["\t", " — ", " - ", " – ", ";", "|", ",", "="]
    added = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        pair = None
        for sep in seps:
            if sep in line:
                a, _, b = line.partition(sep)
                pair = (a, b)
                break
        if not pair:
            continue
        if vocab_add(chat_id, pair[0], pair[1]):
            added += 1
    return added


def _parse_date_like(v):
    """Best-effort parse of a date/timestamp from an old app's export into YYYY-MM-DD."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        try:
            ts = v / 1000 if v > 1e12 else v  # ms vs s epoch
            return datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d")
        except Exception:
            return None
    if isinstance(v, str):
        v = v.strip()
        for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(v[:19], fmt).strftime("%Y-%m-%d")
            except Exception:
                continue
    return None


def _extract_stats(obj):
    """Pull a (box, due) hint from an old app's per-word stats, if present."""
    if not isinstance(obj, dict):
        return None, None
    low = {str(k).lower(): v for k, v in obj.items()}
    box = None
    for k in ("box", "level", "stage", "srsbox", "interval_index", "rank"):
        if k in low and isinstance(low[k], (int, float)):
            box = int(low[k])
            break
    if box is None:
        correct = low.get("correct", low.get("correctcount", low.get("known")))
        wrong = low.get("wrong", low.get("incorrectcount", low.get("mistakes")))
        if isinstance(correct, (int, float)) and isinstance(wrong, (int, float)):
            box = int(correct) - int(wrong)
    if box is not None:
        box = max(0, min(box, len(VOCAB_INTERVALS) - 1))
    due = None
    for k in ("due", "nextreview", "next_due", "nextdue", "next_review_date", "reviewdate"):
        if k in low:
            due = _parse_date_like(low[k])
            if due:
                break
    if due is None and box is not None:
        for k in ("lastreview", "last_seen", "lastseen", "reviewed_at"):
            if k in low:
                last = _parse_date_like(low[k])
                if last:
                    y, m, d = map(int, last.split("-"))
                    due = (date(y, m, d) + timedelta(days=VOCAB_INTERVALS[box])).strftime("%Y-%m-%d")
                    break
    return box, due


def _pair_from_obj(obj):
    """Extract (front, back) from a dict or 2-element list, trying common keys."""
    if isinstance(obj, (list, tuple)) and len(obj) >= 2:
        return str(obj[0]), str(obj[1])
    if isinstance(obj, dict):
        low = {str(k).lower(): v for k, v in obj.items()}
        for a, b in [
            ("front", "back"), ("word", "translation"), ("word", "meaning"),
            ("en", "ru"), ("eng", "rus"), ("english", "russian"), ("english", "uzbek"),
            ("term", "definition"), ("question", "answer"), ("q", "a"),
            ("key", "value"), ("source", "target"),
        ]:
            if a in low and b in low:
                return str(low[a]), str(low[b])
        vals = [v for v in obj.values() if isinstance(v, (str, int, float))]
        if len(vals) >= 2:
            return str(vals[0]), str(vals[1])
    return None


def vocab_import_ascend(chat_id: int, data: dict) -> int:
    """Import from the 'Ascend' app export: vocabList/learnedWords with stage (0-11)
    and nextReview (epoch ms). Scales their 12-stage SRS to our 6-box scale."""
    max_box = len(VOCAB_INTERVALS) - 1  # 5
    items = list(data.get("vocabList") or []) + list(data.get("learnedWords") or [])
    added = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        eng = it.get("english")
        translation = it.get("russian") or it.get("uzbek") or it.get("meaning")
        if not eng or not translation:
            continue
        stage = it.get("stage")
        box = None
        if isinstance(stage, (int, float)):
            box = max(0, min(round(stage / 11 * max_box), max_box))
        due = _parse_date_like(it.get("nextReview"))
        if vocab_add(chat_id, str(eng), str(translation), box=box, due=due):
            added += 1
    return added


def vocab_import_json(chat_id: int, text: str):
    """Import words from JSON. Returns count added, or None if text isn't valid JSON."""
    try:
        data = json.loads(text)
    except Exception:
        return None
    if isinstance(data, dict) and ("vocabList" in data or "learnedWords" in data):
        return vocab_import_ascend(chat_id, data)
    added = 0
    if isinstance(data, dict) and data and all(
        isinstance(v, (str, int, float)) for v in data.values()
    ):
        # {"hello": "привет", ...}
        for k, v in data.items():
            if vocab_add(chat_id, str(k), str(v)):
                added += 1
        return added
    # otherwise look for a list of items (top-level, or nested under a key)
    items = data if isinstance(data, list) else None
    if items is None and isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list):
                items = v
                break
    for item in items or []:
        pair = _pair_from_obj(item)
        if pair:
            box, due = _extract_stats(item)
            if vocab_add(chat_id, pair[0], pair[1], box=box, due=due):
                added += 1
    return added


def vocab_next(chat_id: int):
    """Return the most-due card (due today or overdue), else the earliest upcoming."""
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM vocab WHERE chat_id=? AND due<=? ORDER BY due ASC, id ASC LIMIT 1",
            (chat_id, today),
        ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT * FROM vocab WHERE chat_id=? ORDER BY due ASC, id ASC LIMIT 1",
                (chat_id,),
            ).fetchone()
    return row


def vocab_get(card_id: int):
    with db() as conn:
        return conn.execute("SELECT * FROM vocab WHERE id=?", (card_id,)).fetchone()


def vocab_grade(card_id: int, known: bool):
    with db() as conn:
        row = conn.execute("SELECT box FROM vocab WHERE id=?", (card_id,)).fetchone()
        if not row:
            return
        box = row["box"]
        box = min(box + 1, len(VOCAB_INTERVALS) - 1) if known else 0
        due = (datetime.now(TZ) + timedelta(days=VOCAB_INTERVALS[box])).strftime("%Y-%m-%d")
        conn.execute("UPDATE vocab SET box=?, due=? WHERE id=?", (box, due, card_id))


def vocab_counts(chat_id: int):
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    with db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) c FROM vocab WHERE chat_id=?", (chat_id,)
        ).fetchone()["c"]
        due = conn.execute(
            "SELECT COUNT(*) c FROM vocab WHERE chat_id=? AND due<=?", (chat_id, today)
        ).fetchone()["c"]
    return total, due


def register_user(chat_id: int):
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (chat_id, reminders_enabled) VALUES (?, 1)",
            (chat_id,),
        )


def set_reminders(chat_id: int, enabled: bool):
    with db() as conn:
        conn.execute(
            "UPDATE users SET reminders_enabled=? WHERE chat_id=?",
            (1 if enabled else 0, chat_id),
        )


def enabled_users() -> list[int]:
    with db() as conn:
        rows = conn.execute(
            "SELECT chat_id FROM users WHERE reminders_enabled=1"
        ).fetchall()
    return [r["chat_id"] for r in rows]


def log_focus(chat_id: int, minutes: int):
    now = datetime.now(TZ)
    with db() as conn:
        conn.execute(
            "INSERT INTO focus_log (chat_id, day, ts, minutes) VALUES (?,?,?,?)",
            (chat_id, now.strftime("%Y-%m-%d"), now.isoformat(), minutes),
        )


def get_stats(chat_id: int) -> dict:
    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")
    # Monday of the current week
    week_start = (now - _days(now.weekday())).strftime("%Y-%m-%d")
    with db() as conn:
        t = conn.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(minutes),0) m "
            "FROM focus_log WHERE chat_id=? AND day=?",
            (chat_id, today),
        ).fetchone()
        w = conn.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(minutes),0) m "
            "FROM focus_log WHERE chat_id=? AND day>=?",
            (chat_id, week_start),
        ).fetchone()
    return {
        "today_sessions": t["c"], "today_min": t["m"],
        "week_sessions": w["c"], "week_min": w["m"],
    }


def _days(n: int):
    from datetime import timedelta
    return timedelta(days=n)


def get_streak(chat_id: int) -> int:
    """Consecutive days (ending today or yesterday) with at least one focus session."""
    with db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT day FROM focus_log WHERE chat_id=?", (chat_id,)
        ).fetchall()
    dayset = {r["day"] for r in rows}
    if not dayset:
        return 0
    today = datetime.now(TZ).date()
    if today.isoformat() in dayset:
        cur = today
    elif (today - timedelta(days=1)).isoformat() in dayset:
        cur = today - timedelta(days=1)
    else:
        return 0
    streak = 0
    while cur.isoformat() in dayset:
        streak += 1
        cur = cur - timedelta(days=1)
    return streak


# --------------------------------------------------------------------------- #
# UI helpers
# --------------------------------------------------------------------------- #
def main_menu() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🍅 Start Pomodoro", callback_data="pomo25")],
        [InlineKeyboardButton("📊 My stats", callback_data="stats")],
        [InlineKeyboardButton("📚 English apps", callback_data="apps")],
        [InlineKeyboardButton("🔔 Reminders on", callback_data="rem_on"),
         InlineKeyboardButton("🔕 off", callback_data="rem_off")],
    ]
    return InlineKeyboardMarkup(rows)


def apps_keyboard() -> InlineKeyboardMarkup | None:
    buttons = []
    if VOCAB_URL:
        buttons.append([InlineKeyboardButton(
            "📚 Vocab Trainer", web_app=WebAppInfo(url=VOCAB_URL))])
    if REPEATER_URL:
        buttons.append([InlineKeyboardButton(
            "🔁 A–B Repeater", web_app=WebAppInfo(url=REPEATER_URL))])
    return InlineKeyboardMarkup(buttons) if buttons else None


# --------------------------------------------------------------------------- #
# Command handlers
# --------------------------------------------------------------------------- #
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    register_user(chat_id)
    text = (
        "👋 Salom! I'm your study assistant.\n\n"
        "I'll remind you to follow your exam routine, run Pomodoro focus "
        "sessions, keep your stats, and open your English apps right here.\n\n"
        "Commands:\n"
        "/plan — see your whole daily plan\n"
        "/edit — изменить план словами (напр.: /edit добавь чтение в 21:00)\n"
        "/vocab — тренажёр слов (флэш-карточки прямо в боте)\n"
        "/addword слово - перевод — добавить слово (или пришли файл .txt)\n"
        "/now — what should I do right now\n"
        "💬 Просто напиши сообщение — ИИ-помощник ответит (объяснит тему, проверит английский, поможет с задачей).\n"
        "/clear — очистить разговор с ИИ\n"
        "/countdown — сколько дней до экзамена\n"
        "/report — итоги дня (фокус, серия, до экзамена)\n"
        "/reload — apply plan changes you made on GitHub\n"
        "/pomodoro — start a focus timer (default 25/5)\n"
        "/stop — stop the current focus timer\n"
        "/stats — see your focus stats\n"
        "/apps — open your Vocab Trainer & A–B Repeater\n"
        "/reminders_on  /reminders_off — toggle daily reminders\n"
        "/help — show this again"
    )
    await update.message.reply_text(text, reply_markup=main_menu())


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


def _format_plan() -> str:
    """Build a readable view of the current schedule, grouped by day scope."""
    buckets = {"all": [], "mon-sat": [], "sun": [], "other": []}
    for h, m, days, msg in REMINDERS:
        key = days_to_text(days)
        if key in ("all", "mon-sat", "sun"):
            buckets[key].append((h, m, msg, None))
        else:
            buckets["other"].append((h, m, msg, key))

    def block(title, items, show_days=False):
        if not items:
            return ""
        items.sort(key=lambda x: (x[0], x[1]))
        lines = [f"*{title}*"]
        for h, m, msg, key in items:
            tag = f" ({key})" if show_days and key else ""
            lines.append(f"`{h:02d}:{m:02d}`{tag}  {msg}")
        return "\n".join(lines)

    sections = [
        block("🗓️ Every day", buckets["all"]),
        block("📚 Mon–Sat", buckets["mon-sat"]),
        block("🌤️ Sunday", buckets["sun"]),
        block("📌 Other days", buckets["other"], show_days=True),
    ]
    body = "\n\n".join(s for s in sections if s)
    footer = (
        "\n\n_To change it: open *schedule.txt* in Notepad, edit, save, "
        "then restart the bot._"
    )
    return (body or "No plan set yet.") + footer


async def cmd_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(_format_plan(), parse_mode="Markdown")


async def cmd_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tell the user what they should be doing right now, and what's next."""
    now = datetime.now(TZ)
    weekday = now.weekday()
    today = sorted(
        [(h, m, msg) for h, m, days, msg in REMINDERS if weekday in days],
        key=lambda x: (x[0], x[1]),
    )
    if not today:
        await update.message.reply_text(
            f"🕒 Сейчас {now.strftime('%H:%M')}\n\nНа сегодня в плане ничего нет. Отдыхай 🙂"
        )
        return

    now_min = now.hour * 60 + now.minute
    current = None
    nxt = None
    for h, m, msg in today:
        if h * 60 + m <= now_min:
            current = (h, m, msg)
        elif nxt is None:
            nxt = (h, m, msg)

    lines = [f"🕒 Сейчас {now.strftime('%H:%M')}"]
    if current:
        lines.append(f"\n👉 Сейчас ({current[0]:02d}:{current[1]:02d}): {current[2]}")
    else:
        first = today[0]
        lines.append(f"\n🌙 День ещё не начался. Первое дело в {first[0]:02d}:{first[1]:02d}.")
    if nxt:
        lines.append(f"\n⏭️ Дальше в {nxt[0]:02d}:{nxt[1]:02d}: {nxt[2]}")
    elif current:
        lines.append("\n✅ На сегодня всё. Отдыхай и ложись вовремя.")
    await update.message.reply_text("\n".join(lines))


def _plan_text_for_ai() -> str:
    """Render the current plan as compact plain text for the AI's context."""
    labels = {
        "all": "Каждый день", "mon-sat": "Пн–Сб", "sun": "Вс",
        "mon,wed,fri": "Пн/Ср/Пт (физика дома)", "tue,thu,sat": "Вт/Чт/Сб (математика)",
    }
    rows = sorted(REMINDERS, key=lambda x: (days_to_text(x[2]), x[0], x[1]))
    out = []
    for h, m, days, msg in rows:
        key = days_to_text(days)
        out.append(f"{labels.get(key, key)} {h:02d}:{m:02d} — {msg}")
    return "\n".join(out)


async def ask_ai(chat_id: int, user_text: str) -> str:
    """Send the user's message (with recent history) to the AI and return the reply."""
    hist = AI_HISTORY.setdefault(chat_id, [])
    hist.append({"role": "user", "content": user_text})
    system = AI_SYSTEM_PROMPT
    plan = _plan_text_for_ai()
    if plan:
        system += (
            "\n\nВот текущий распорядок дня ученика (его расписание). Используй "
            "его, когда он спрашивает про учёбу, эффективность или сам план — "
            "давай советы под этот конкретный распорядок:\n" + plan
        )
    messages = [{"role": "system", "content": system}] + hist[-16:]
    payload = {
        "model": AI_MODEL,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 900,
    }
    headers = {"Authorization": f"Bearer {AI_API_KEY}"}
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(AI_BASE_URL, json=payload, headers=headers)
        r.raise_for_status()
        data = r.json()
    reply = data["choices"][0]["message"]["content"].strip()
    hist.append({"role": "assistant", "content": reply})
    # Keep history bounded.
    if len(hist) > 24:
        del hist[: len(hist) - 24]
    return reply


async def ai_complete(system: str, user: str) -> str:
    """One-shot AI call (no history) — used for structured tasks like plan edits."""
    payload = {
        "model": AI_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
        "max_tokens": 1500,
    }
    headers = {"Authorization": f"Bearer {AI_API_KEY}"}
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(AI_BASE_URL, json=payload, headers=headers)
        r.raise_for_status()
        data = r.json()
    return data["choices"][0]["message"]["content"].strip()


def _format_entries(entries: list) -> str:
    """Plain-text grouped view of a plan (safe to send without markdown)."""
    labels = {
        "all": "Каждый день", "mon,wed,fri": "Пн/Ср/Пт", "mon-sat": "Пн–Сб",
        "tue,thu,sat": "Вт/Чт/Сб", "sun": "Вс",
    }
    groups: dict[str, list] = {}
    for h, m, d, msg in entries:
        groups.setdefault(days_to_text(d), []).append((h, m, msg))
    order = ["all", "mon,wed,fri", "mon-sat", "tue,thu,sat", "sun"]
    keys = order + [k for k in groups if k not in order]
    out = []
    for k in keys:
        if k not in groups:
            continue
        out.append(f"— {labels.get(k, k)} —")
        for h, m, msg in sorted(groups[k]):
            out.append(f"{h:02d}:{m:02d}  {msg}")
        out.append("")
    return "\n".join(out).strip()


# chat_id -> serialized new schedule text awaiting confirmation
PENDING_EDITS: dict[int, str] = {}


async def cmd_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Change the plan in plain language; AI applies it, user confirms."""
    chat_id = update.effective_chat.id
    instr = " ".join(context.args).strip() if context.args else ""
    if not instr:
        await update.message.reply_text(
            "✏️ Напиши, что изменить в плане. Примеры:\n"
            "/edit добавь чтение в 21:00 каждый день\n"
            "/edit перенеси ужин на 19:00\n"
            "/edit убери утреннюю пробежку в среду"
        )
        return
    if not AI_API_KEY:
        await update.message.reply_text("Для /edit нужен ИИ-ключ (Groq).")
        return
    await context.bot.send_chat_action(chat_id, "typing")
    current = _serialize_schedule(REMINDERS)
    system = (
        "Ты редактируешь расписание дня. Тебе дают текущее расписание (строки "
        "формата 'ЧЧ:ММ | дни | сообщение'; дни: all, mon-sat, sun или список "
        "вроде mon,wed,fri) и инструкцию. Верни ПОЛНОЕ новое расписание в том же "
        "формате — только строки расписания, без пояснений, без markdown, без "
        "тройных кавычек. Строки, которых инструкция не касается, оставь без "
        "изменений."
    )
    try:
        out = await ai_complete(system, f"Текущее расписание:\n{current}\n\nИнструкция: {instr}")
    except Exception as e:
        log.warning("edit ai error: %s", e)
        await update.message.reply_text("Не получилось. Попробуй ещё раз через минуту.")
        return
    entries = parse_schedule_text(out)
    if len(entries) < 3:
        await update.message.reply_text(
            "Не понял правку. Попробуй сформулировать иначе, например: "
            "«добавь чтение в 21:00 каждый день»."
        )
        return
    PENDING_EDITS[chat_id] = _serialize_schedule(entries)
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Применить", callback_data="edit_apply"),
        InlineKeyboardButton("❌ Отмена", callback_data="edit_cancel"),
    ]])
    preview = _format_entries(entries)
    if len(preview) > 3500:
        preview = preview[:3500] + "\n…"
    await update.message.reply_text("Вот новый план:\n\n" + preview + "\n\nПрименить?", reply_markup=kb)


async def _apply_pending_edit(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    global REMINDERS
    text = PENDING_EDITS.pop(chat_id, None)
    if not text:
        await context.bot.send_message(chat_id, "Нечего применять.")
        return
    with open(SCHEDULE_FILE, "w", encoding="utf-8") as f:
        f.write(text)
    REMINDERS = load_schedule(SCHEDULE_FILE)
    reschedule_reminders(context.application)
    await context.bot.send_message(
        chat_id, "✅ План обновлён!\n\n" + _format_entries(REMINDERS)
    )


def _vocab_card_kb_front(card_id: int):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("👁 Показать ответ", callback_data=f"voc_show:{card_id}")
    ]])


def _vocab_card_kb_back(card_id: int):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Знаю", callback_data=f"voc_know:{card_id}"),
        InlineKeyboardButton("❌ Не знаю", callback_data=f"voc_no:{card_id}"),
    ]])


async def _send_next_card(context, chat_id):
    row = vocab_next(chat_id)
    if row is None:
        total, _ = vocab_counts(chat_id)
        if total == 0:
            await context.bot.send_message(
                chat_id,
                "📭 Словарь пуст. Добавь слова:\n"
                "• одним: /addword hello — привет\n"
                "• списком: пришли файл .txt, где каждая строка «слово - перевод».",
            )
        else:
            await context.bot.send_message(chat_id, "🎉 На сегодня все слова повторены! Отдыхай.")
        return
    await context.bot.send_message(
        chat_id, f"🇬🇧 <b>{row['front']}</b>\n\nВспомни перевод…",
        parse_mode="HTML", reply_markup=_vocab_card_kb_front(row["id"]),
    )


async def cmd_vocab(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _send_next_card(context, update.effective_chat.id)


async def cmd_addword(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args) if context.args else ""
    added = vocab_import(update.effective_chat.id, text) if text else 0
    if added:
        total, due = vocab_counts(update.effective_chat.id)
        await update.message.reply_text(f"➕ Добавлено! Всего слов: {total}. Напиши /vocab для тренировки.")
    else:
        await update.message.reply_text(
            "Формат: /addword слово - перевод\nНапример: /addword improve — улучшать"
        )


async def cmd_vocabstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total, due = vocab_counts(update.effective_chat.id)
    await update.message.reply_text(
        f"📚 Словарь: {total} слов, к повторению сегодня: {due}."
    )


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Import vocabulary from an uploaded text/CSV/JSON file."""
    doc = update.message.document
    name = (doc.file_name or "").lower()
    ok_ext = name.endswith((".txt", ".csv", ".json"))
    ok_mime = doc.mime_type in ("text/plain", "text/csv", "application/json")
    if not (ok_ext or ok_mime):
        await update.message.reply_text(
            "Пришли файл .txt, .csv или .json.\n"
            "• .txt/.csv: каждая строка «слово - перевод»\n"
            "• .json: {\"hello\": \"привет\"} или [{\"word\":\"hello\",\"translation\":\"привет\"}]"
        )
        return
    try:
        f = await context.bot.get_file(doc.file_id)
        raw = await f.download_as_bytearray()
        text = bytes(raw).decode("utf-8", errors="replace")
    except Exception as e:
        log.warning("doc import error: %s", e)
        await update.message.reply_text("Не смог прочитать файл. Попробуй кодировку UTF-8.")
        return
    # JSON first if it looks like JSON; otherwise line-based text.
    added = None
    if name.endswith(".json") or text.lstrip()[:1] in ("{", "["):
        added = vocab_import_json(update.effective_chat.id, text)
    if not added:  # None (not JSON) or 0 — fall back to line parsing
        added = vocab_import(update.effective_chat.id, text)
    total, _ = vocab_counts(update.effective_chat.id)
    if added:
        await update.message.reply_text(
            f"✅ Импортировал {added} слов. Всего: {total}. Напиши /vocab чтобы начать!"
        )
    else:
        await update.message.reply_text(
            "Не нашёл слов. Проверь формат: «hello - привет» построчно, "
            "или JSON вида {\"hello\": \"привет\"}."
        )


async def on_ai_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Any plain (non-command) text message goes to the AI assistant."""
    if not update.message or not update.message.text:
        return
    if not AI_API_KEY:
        await update.message.reply_text(
            "🤖 ИИ-помощник ещё не подключён. Нужен бесплатный ИИ-ключ "
            "(Groq) — попроси помощи с настройкой."
        )
        return
    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id, "typing")
    try:
        reply = await ask_ai(chat_id, update.message.text)
    except Exception as e:
        log.warning("AI error: %s", e)
        reply = "🤖 Не получилось получить ответ от ИИ. Попробуй ещё раз через минуту."
    await update.message.reply_text(reply)


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Forget the AI conversation so far."""
    AI_HISTORY.pop(update.effective_chat.id, None)
    await update.message.reply_text("🧹 Разговор с ИИ очищен. Начнём заново.")


async def cmd_countdown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    n = days_to_exam()
    if n is None:
        await update.message.reply_text("Дата экзамена не задана.")
        return
    if n > 1:
        txt = f"📅 До экзамена ({EXAM_DATE[8:10]}.{EXAM_DATE[5:7]}) осталось {n} дн. Каждый день на счету — вперёд! 💪"
    elif n == 1:
        txt = "📅 До экзамена остался 1 день! Спокойно повтори главное и выспись. 🍀"
    elif n == 0:
        txt = "📅 Экзамен сегодня! Ты готовился — соберись и удачи! 🍀"
    else:
        txt = f"📅 Экзамен был {abs(n)} дн. назад. Надеюсь, всё прошло отлично!"
    await update.message.reply_text(txt)


def build_report(chat_id: int) -> str:
    s = get_stats(chat_id)
    streak = get_streak(chat_id)
    n = days_to_exam()
    lines = ["📊 Итоги дня", ""]
    lines.append(f"🎯 Сегодня: {s['today_sessions']} сессий, {s['today_min']} мин фокуса")
    lines.append(f"📈 За неделю: {s['week_sessions']} сессий, {s['week_min']} мин")
    lines.append(f"🔥 Серия: {streak} дн. подряд")
    if n is not None and n >= 0:
        lines.append(f"📅 До экзамена: {n} дн.")
    lines.append("")
    if s["today_min"] == 0:
        lines.append("Сегодня без фокуса — ничего, завтра наверстаем. Отдохни. 🌙")
    else:
        lines.append("Отличная работа сегодня! Не теряй серию. 💪")
    return "\n".join(lines)


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(build_report(update.effective_chat.id))


async def daily_report_cb(context: ContextTypes.DEFAULT_TYPE):
    for chat_id in enabled_users():
        try:
            await context.bot.send_message(chat_id, build_report(chat_id))
        except Exception as e:
            log.warning("daily report to %s failed: %s", chat_id, e)


async def cmd_reload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pull the latest schedule.txt from GitHub and apply it live."""
    await update.message.reply_text("🔄 Updating your plan…")
    # 1. Try to fetch the newest schedule.txt from GitHub.
    repo_dir = os.path.dirname(os.path.abspath(__file__))
    try:
        subprocess.run(
            ["git", "pull", "--no-rebase"],
            cwd=repo_dir, capture_output=True, text=True, timeout=45,
        )
    except Exception as e:
        log.warning("git pull failed: %s", e)
    # 2. Reload the plan from the file and reschedule the reminders.
    global REMINDERS
    REMINDERS = load_schedule(SCHEDULE_FILE)
    reschedule_reminders(context.application)
    await update.message.reply_text(
        f"✅ Plan updated — {len(REMINDERS)} reminders active.\n\n" + _format_plan(),
        parse_mode="Markdown",
    )


async def cmd_pomodoro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    work, brk = WORK_DEFAULT, BREAK_DEFAULT
    if context.args:
        try:
            work = int(context.args[0])
            if len(context.args) > 1:
                brk = int(context.args[1])
        except ValueError:
            pass
    work = max(1, min(work, 120))
    brk = max(1, min(brk, 60))
    await _start_pomodoro(context, chat_id, work, brk)


async def _start_pomodoro(context, chat_id, work, brk):
    # cancel any existing session
    old = POMO.get(chat_id)
    if old and old.get("job"):
        old["job"].schedule_removal()
    POMO[chat_id] = {"active": True, "work": work, "break": brk, "cycle": 1}
    job = context.job_queue.run_once(
        _pomo_end_work, work * 60, data={"chat_id": chat_id}, name=f"pomo-{chat_id}"
    )
    POMO[chat_id]["job"] = job
    await context.bot.send_message(
        chat_id,
        f"🍅 Focus session 1 — work for {work} min.\n"
        f"Phone face-down. I'll ping you when it's break time. /stop to end.",
    )


async def _pomo_end_work(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data["chat_id"]
    state = POMO.get(chat_id)
    if not state or not state["active"]:
        return
    log_focus(chat_id, state["work"])  # one completed work interval
    brk = state["break"]
    job = context.job_queue.run_once(
        _pomo_end_break, brk * 60, data={"chat_id": chat_id}, name=f"pomo-{chat_id}"
    )
    state["job"] = job
    await context.bot.send_message(
        chat_id,
        f"✅ Work done — nice! Break {brk} min. Stand up, water, look far away.",
    )


async def _pomo_end_break(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data["chat_id"]
    state = POMO.get(chat_id)
    if not state or not state["active"]:
        return
    state["cycle"] += 1
    work = state["work"]
    job = context.job_queue.run_once(
        _pomo_end_work, work * 60, data={"chat_id": chat_id}, name=f"pomo-{chat_id}"
    )
    state["job"] = job
    await context.bot.send_message(
        chat_id,
        f"🍅 Break over — Focus session {state['cycle']}, {work} min. /stop to end.",
    )


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    state = POMO.get(chat_id)
    if state and state["active"]:
        if state.get("job"):
            state["job"].schedule_removal()
        state["active"] = False
        s = get_stats(chat_id)
        await update.message.reply_text(
            f"⏹️ Pomodoro stopped. Today: {s['today_sessions']} sessions, "
            f"{s['today_min']} focus min. Great work!"
        )
    else:
        await update.message.reply_text("No focus session running. Start one with /pomodoro.")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _send_stats(context, update.effective_chat.id)


async def _send_stats(context, chat_id):
    s = get_stats(chat_id)
    text = (
        "📊 Your focus stats\n\n"
        f"Today: {s['today_sessions']} sessions · {s['today_min']} min\n"
        f"This week: {s['week_sessions']} sessions · {s['week_min']} min\n\n"
        "Every session counts. Keep the streak going 🔥"
    )
    await context.bot.send_message(chat_id, text)


async def cmd_apps(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _send_apps(context, update.effective_chat.id)


async def _send_apps(context, chat_id):
    kb = apps_keyboard()
    if kb:
        await context.bot.send_message(
            chat_id, "📚 Open an app — it runs right here in Telegram:", reply_markup=kb
        )
    else:
        await context.bot.send_message(
            chat_id,
            "Your apps aren't linked yet. Host the two HTML files (free, see README) "
            "and set VOCAB_URL and REPEATER_URL. Then /apps will open them here.",
        )


async def cmd_rem_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_reminders(update.effective_chat.id, True)
    await update.message.reply_text("🔔 Daily reminders ON.")


async def cmd_rem_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_reminders(update.effective_chat.id, False)
    await update.message.reply_text("🔕 Daily reminders OFF. Turn back on with /reminders_on.")


# --------------------------------------------------------------------------- #
# Inline button router
# --------------------------------------------------------------------------- #
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat.id
    data = q.data
    if data == "pomo25":
        await _start_pomodoro(context, chat_id, WORK_DEFAULT, BREAK_DEFAULT)
    elif data == "stats":
        await _send_stats(context, chat_id)
    elif data == "apps":
        await _send_apps(context, chat_id)
    elif data == "rem_on":
        set_reminders(chat_id, True)
        await context.bot.send_message(chat_id, "🔔 Daily reminders ON.")
    elif data == "rem_off":
        set_reminders(chat_id, False)
        await context.bot.send_message(chat_id, "🔕 Daily reminders OFF.")
    elif data == "edit_apply":
        await _apply_pending_edit(context, chat_id)
    elif data == "edit_cancel":
        PENDING_EDITS.pop(chat_id, None)
        await context.bot.send_message(chat_id, "❌ Отменено. План без изменений.")
    elif data.startswith("voc_show:"):
        row = vocab_get(int(data.split(":")[1]))
        if row:
            await q.edit_message_text(
                f"🇬🇧 <b>{row['front']}</b>\n🇷🇺 {row['back']}",
                parse_mode="HTML", reply_markup=_vocab_card_kb_back(row["id"]),
            )
    elif data.startswith("voc_know:") or data.startswith("voc_no:"):
        cid = int(data.split(":")[1])
        vocab_grade(cid, data.startswith("voc_know:"))
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await _send_next_card(context, chat_id)


# --------------------------------------------------------------------------- #
# Reminder broadcasting
# --------------------------------------------------------------------------- #
async def reminder_cb(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    weekday = datetime.now(TZ).weekday()
    if weekday not in data["days"]:
        return
    for chat_id in enabled_users():
        try:
            await context.bot.send_message(chat_id, data["text"])
        except Exception as e:  # user blocked bot, etc.
            log.warning("reminder to %s failed: %s", chat_id, e)


def schedule_reminders(app: Application):
    for i, (hour, minute, days, text) in enumerate(REMINDERS):
        app.job_queue.run_daily(
            reminder_cb,
            time=dtime(hour=hour, minute=minute, tzinfo=TZ),
            data={"days": days, "text": text},
            name=f"rem-{i}",
        )
    log.info("Scheduled %d daily reminders", len(REMINDERS))


def reschedule_reminders(app: Application):
    """Remove existing reminder jobs and re-add them from the current plan."""
    for job in app.job_queue.jobs():
        if job.name and job.name.startswith("rem-"):
            job.schedule_removal()
    schedule_reminders(app)


# --------------------------------------------------------------------------- #
# Startup
# --------------------------------------------------------------------------- #
async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start", "Open the menu"),
        BotCommand("plan", "See your daily plan"),
        BotCommand("edit", "Change your plan (just tell it)"),
        BotCommand("vocab", "Practice vocabulary flashcards"),
        BotCommand("addword", "Add a vocabulary word"),
        BotCommand("now", "What should I do now?"),
        BotCommand("clear", "Clear the AI chat"),
        BotCommand("countdown", "Days until the exam"),
        BotCommand("report", "Today's study report"),
        BotCommand("reload", "Load plan changes from GitHub"),
        BotCommand("pomodoro", "Start a focus timer"),
        BotCommand("stop", "Stop the focus timer"),
        BotCommand("stats", "See your focus stats"),
        BotCommand("apps", "Open your English apps"),
        BotCommand("reminders_on", "Turn daily reminders on"),
        BotCommand("reminders_off", "Turn daily reminders off"),
        BotCommand("help", "Show help"),
    ])
    schedule_reminders(app)


def main():
    if not BOT_TOKEN or BOT_TOKEN == "PASTE_YOUR_TOKEN_HERE":
        raise SystemExit(
            "BOT_TOKEN is not set. Get one from @BotFather and put your REAL "
            "token in the command / .env file (see README.md)."
        )
    init_db()

    global REMINDERS
    REMINDERS = load_schedule(SCHEDULE_FILE)
    log.info("Loaded %d reminders from %s", len(REMINDERS), SCHEDULE_FILE)

    # Python 3.13+ no longer auto-creates an event loop; make sure one exists
    # so python-telegram-bot's run_polling() has a current loop to use.
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("plan", cmd_plan))
    app.add_handler(CommandHandler("now", cmd_now))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("countdown", cmd_countdown))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("reload", cmd_reload))
    app.add_handler(CommandHandler("edit", cmd_edit))
    app.add_handler(CommandHandler("vocab", cmd_vocab))
    app.add_handler(CommandHandler("addword", cmd_addword))
    app.add_handler(CommandHandler("vocabstats", cmd_vocabstats))
    app.add_handler(CommandHandler("pomodoro", cmd_pomodoro))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("apps", cmd_apps))
    app.add_handler(CommandHandler("reminders_on", cmd_rem_on))
    app.add_handler(CommandHandler("reminders_off", cmd_rem_off))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_ai_message))

    log.info("Study bot starting…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
