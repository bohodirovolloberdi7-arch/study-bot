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
import sqlite3
import subprocess
import logging
import httpx
from datetime import time as dtime, datetime
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
    out = []
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("|")
            if len(parts) < 3:
                continue
            t = parts[0].strip()
            days = parse_days(parts[1])
            msg = "|".join(parts[2:]).strip()
            try:
                hh, mm = t.split(":")
                h, m = int(hh), int(mm)
            except ValueError:
                continue
            out.append((h, m, days, msg))
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
        "/now — what should I do right now\n"
        "💬 Просто напиши сообщение — ИИ-помощник ответит (объяснит тему, проверит английский, поможет с задачей).\n"
        "/clear — очистить разговор с ИИ\n"
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


async def ask_ai(chat_id: int, user_text: str) -> str:
    """Send the user's message (with recent history) to the AI and return the reply."""
    hist = AI_HISTORY.setdefault(chat_id, [])
    hist.append({"role": "user", "content": user_text})
    messages = [{"role": "system", "content": AI_SYSTEM_PROMPT}] + hist[-16:]
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
        BotCommand("now", "What should I do now?"),
        BotCommand("clear", "Clear the AI chat"),
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
    app.add_handler(CommandHandler("reload", cmd_reload))
    app.add_handler(CommandHandler("pomodoro", cmd_pomodoro))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("apps", cmd_apps))
    app.add_handler(CommandHandler("reminders_on", cmd_rem_on))
    app.add_handler(CommandHandler("reminders_off", cmd_rem_off))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_ai_message))

    log.info("Study bot starting…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
