# bot.py — остаточна робоча версія (v21.5+, Koyeb, 2025)
import logging
import os
import sqlite3
from contextlib import closing
from datetime import time as dtime

import pytz
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# ===================== НАЛАШТУВАННЯ =====================
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise ValueError("Встанови змінну середовища TOKEN")

KYIV_TZ = pytz.timezone("Europe/Kyiv")
DB_PATH = "/tmp/reminders.db"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ===================== БАЗА ДАНИХ =====================
def init_db():
    with closing(sqlite3.connect(DB_PATH)) as conn:
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS reminders (
                    chat_id INTEGER PRIMARY KEY,
                    text TEXT NOT NULL,
                    days TEXT NOT NULL,
                    time_str TEXT NOT NULL
                )
            """)

def save_reminder(chat_id: int, text: str, days: tuple, time_str: str):
    days_str = ",".join(map(str, sorted(days)))
    with closing(sqlite3.connect(DB_PATH)) as conn:
        with conn:
            conn.execute("""
                INSERT INTO reminders (chat_id, text, days, time_str)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                  text=excluded.text, days=excluded.days, time_str=excluded.time_str
            """, (chat_id, text, days_str, time_str))

def delete_reminder(chat_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        with conn:
            conn.execute("DELETE FROM reminders WHERE chat_id = ?", (chat_id,))

def get_all_reminders():
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM reminders").fetchall()
            return [{
                "chat_id": r["chat_id"],
                "text": r["text"],
                "days": tuple(map(int, r["days"].split(","))) if r["days"] else (),
                "time": r["time_str"]
            } for r in rows]
    except Exception as e:
        logger.error("DB error: %s", e)
        return []

init_db()

# ===================== ДНІ ТИЖНЯ =====================
DAYS_MAP = {
    "пн": 0, "пон": 0, "понеділок": 0,
    "вт": 1, "вів": 1, "вівторок": 1,
    "ср": 2, "сер": 2, "середа": 2,
    "чт": 3, "чет": 3, "четвер": 3,
    "пт": 4, "п'ят": 4, "п'ятниця": 4,
    "сб": 5, "суб": 5, "субота": 5,
    "нд": 6, "нед": 6, "неділя": 6,
    "щодня": (0,1,2,3,4,5,6), "кожен день": (0,1,2,3,4,5,6),
}
WEEKDAY_NAMES = ["пн", "вт", "ср", "чт", "пт", "сб", "нд"]

def parse_days(text: str):
    if not text: return ()
    words = text.lower().replace(",", " ").replace(";", " ").split()
    days = set()
    for w in words:
        w = w.strip(".,:!?")
        if w in DAYS_MAP:
            val = DAYS_MAP[w]
            if isinstance(val, tuple): return val
            days.add(val)
    return tuple(sorted(days)) if days else ()

# ===================== КОМАНДИ =====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привіт! Я бот-нагадувач\n\n"
        "`/set Текст | дні | HH:MM`\n\n"
        "Приклади:\n"
        "• `/set Урок | пн ср пт | 17:45`\n"
        "• `/set Заняття | субота | 15:30`\n"
        "• `/set Пити воду | щодня | 10:00`\n"
        "• `/list` — що налаштовано\n"
        "• `/stop` — вимкнути",
        parse_mode="Markdown"
    )

async def list_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    r = next((x for x in get_all_reminders() if x["chat_id"] == chat_id), None)
    if not r:
        await update.message.reply_text("Нагадувань немає")
        return
    days_str = "щодня" if len(r["days"]) == 7 else " ".join(WEEKDAY_NAMES[d] for d in r["days"])
    await update.message.reply_text(f"Текст: {r['text']}\nДні: {days_str}\nЧас: {r['time']} (Київ)")

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    delete_reminder(chat_id)
    if context.job_queue:
        for job in context.job_queue.jobs():
            if getattr(job, "data", {}).get("chat_id") == chat_id:
                job.schedule_removal()
    await update.message.reply_text("Нагадування вимкнено")

async def set_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    raw = update.message.text[len("/set"):].strip()
    if raw.count("|") != 2:
        await update.message.reply_text("Формат: `/set текст | дні | HH:MM`", parse_mode="Markdown")
        return

    text_part, days_part, time_part = [p.strip() for p in raw.split("|", 2)]

    try:
        hh, mm = map(int, time_part.split(":"))
        if not (0 <= hh <= 23 and 0 <= mm <= 59): raise ValueError
    except:
        await update.message.reply_text("Час у форматі HH:MM")
        return

    days_tuple = parse_days(days_part)
    if not days_tuple:
        await update.message.reply_text("Не зрозумів дні (пн, ср, сб, щодня тощо)")
        return

    save_reminder(chat_id, text_part, days_tuple, f"{hh:02d}:{mm:02d}")

    if context.job_queue:
        for job in context.job_queue.jobs():
            if getattr(job, "data", {}).get("chat_id") == chat_id:
                job.schedule_removal()
        context.job_queue.run_daily(
            callback=send_scheduled,
            time=dtime(hour=hh, minute=mm, tzinfo=KYIV_TZ),
            days=days_tuple,
            data={"chat_id": chat_id},
            name=f"reminder_{chat_id}"
        )

    days_str = "щодня" if len(days_tuple) == 7 else " ".join(WEEKDAY_NAMES[d] for d in days_tuple)
    await update.message.reply_text(
        f"Нагадування встановлено!\n\nТекст: {text_part}\nДні: {days_str}\nЧас: {hh:02d}:{mm:02d} (Київ)"
    )

async def send_scheduled(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data["chat_id"]
    r = next((x for x in get_all_reminders() if x["chat_id"] == chat_id), None)
    if r:
        await context.bot.send_message(chat_id=chat_id, text=r["text"])

async def post_init(application: Application):
    await application.bot.delete_webhook(drop_pending_updates=True)
    logger.info("Відновлюю нагадування...")
    for r in get_all_reminders():
        try:
            hh, mm = map(int, r["time"].split(":"))
            application.job_queue.run_daily(
                send_scheduled,
                time=dtime(hour=hh, minute=mm, tzinfo=KYIV_TZ),
                days=r["days"],
                data={"chat_id": r["chat_id"]},
                name=f"reminder_{r['chat_id']}"
            )
        except Exception as e:
            logger.warning("Не вдалося відновити %s: %s", r["chat_id"], e)
    logger.info("Відновлено %d нагадувань", len(get_all_reminders()))

# ===================== ЗАПУСК =====================
def main():
    app = Application.builder().token(TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("set", set_reminder))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CommandHandler("list", list_reminders))

    logger.info("Бот запущено – працює в усіх чатах")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
