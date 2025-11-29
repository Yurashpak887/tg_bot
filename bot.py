# bot.py
import logging
import os
import re
import sqlite3
from contextlib import closing
from datetime import time as dtime

import pytz
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ===================== НАЛАШТУВАННЯ =====================
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise ValueError("Встанови змінну середовища TOKEN")

KYIV_TZ = pytz.timezone("Europe/Kyiv")
DB_PATH = "/tmp/reminders.db"  # Koyeb-friendly

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
                  text=excluded.text,
                  days=excluded.days,
                  time_str=excluded.time_str
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
            out = []
            for row in rows:
                days = tuple(map(int, row["days"].split(","))) if row["days"] else ()
                out.append({
                    "chat_id": row["chat_id"],
                    "text": row["text"],
                    "days": days,
                    "time": row["time_str"]
                })
            return out
    except Exception as e:
        logger.error("DB error: %s", e)
        return []

init_db()

# ===================== ДНІ ТИЖНЯ ТА ПАРСЕР =====================

DAYS_MAP = {
    # понеділок
    "пн": 0, "пн.": 0, "пон": 0, "понеділок": 0, "понедельник": 0,
    # вівторок
    "вт": 1, "вт.": 1, "вівт": 1, "вівторок": 1, "вторник": 1,
    # середа
    "ср": 2, "ср.": 2, "сер": 2, "середа": 2, "среда": 2,
    # четвер
    "чт": 3, "чт.": 3, "чтв": 3, "четвер": 3, "четв": 3,
    # п'ятниця
    "пт": 4, "пт.": 4, "п'т": 4, "п'ятниця": 4, "пятниця": 4, "пят": 4,
    # субота
    "сб": 5, "сб.": 5, "суб": 5, "субота": 5, "суббота": 5,
    # неділя
    "нд": 6, "нд.": 6, "нед": 6, "неділя": 6, "воскресення": 6, "воскресенье": 6,
    # універсальні
    "щодня": (0,1,2,3,4,5,6), "щоденно": (0,1,2,3,4,5,6),
    "кожен день": (0,1,2,3,4,5,6), "кожного дня": (0,1,2,3,4,5,6),
    "каждый день": (0,1,2,3,4,5,6)
}

def normalize_token(tok: str) -> str:
    if not tok:
        return tok
    tok = tok.replace("\u200b", "").replace("\u2060", "").strip()
    tok = tok.replace("’", "'").replace("`", "'").replace("ʼ", "'")
    tok = tok.strip(".,;:()[]\"'").lower()
    tok = re.sub(r"\s+", " ", tok)
    return tok

def parse_days(days_raw: str):
    if not days_raw:
        return ()
    s = days_raw.lower()
    # роздільники: кома, крапка з комою, слеш, пробіли
    s = s.replace(",", " ").replace(";", " ").replace("/", " ")
    parts = [normalize_token(p) for p in s.split() if normalize_token(p)]
    days = []
    for p in parts:
        if p in DAYS_MAP:
            val = DAYS_MAP[p]
            if isinstance(val, tuple):
                return tuple(val)
            if val not in days:
                days.append(val)
        else:
            # спробуємо знаходження по префіксу (наприклад "четв" -> "четвер")
            for key, val in DAYS_MAP.items():
                if isinstance(val, int) and p.startswith(key):
                    if val not in days:
                        days.append(val)
                    break
    return tuple(sorted(days))

WEEKDAY_NAMES = ["пн","вт","ср","чт","пт","сб","нд"]

# ===================== ХЕНДЛЕРИ =====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привіт! Я бот-нагадувач.\n\n"
        "Формат:\n"
        "`/set Текст | дні | HH:MM`\n\n"
        "Приклади:\n"
        "• `/set Урок | пн, ср | 17:45`\n"
        "• `/set ТЕСТ | субота,п'ятниця,четвер | 15:30`\n"
        "• `/set Пити воду | щодня | 10:00`\n"
        "• `/list` — список поточних нагадувань\n"
        "• `/stop` — вимкнути нагадування",
        parse_mode="Markdown"
    )

async def list_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    allr = get_all_reminders()
    chat_id = update.effective_chat.id
    r = next((x for x in allr if x["chat_id"] == chat_id), None)
    if not r:
        await update.message.reply_text("Немає встановлених нагадувань для цього чату.")
        return
    days_names = " ".join(WEEKDAY_NAMES[d] for d in r["days"]) if len(r["days"]) < 7 else "щодня"
    await update.message.reply_text(f"Текст: {r['text']}\nДні: {days_names}\nЧас: {r['time']} (Київ)")

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    delete_reminder(chat_id)

    if context.job_queue:
        for job in context.job_queue.jobs():
            # збережемо, що data може бути словником або простим chat_id
            data = getattr(job, "data", None)
            if data == chat_id or (isinstance(data, dict) and data.get("chat_id") == chat_id):
                job.schedule_removal()

    await update.message.reply_text("Нагадування вимкнено для цього чату.")

async def set_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    raw = update.message.text[len("/set"):].strip()

    if "|" not in raw or raw.count("|") != 2:
        await update.message.reply_text("Формат: `/set текст | дні | HH:MM`", parse_mode="Markdown")
        return

    text_part, days_part, time_part = [p.strip() for p in raw.split("|", 2)]

    # час
    try:
        hh, mm = map(int, time_part.split(":"))
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError()
    except Exception:
        await update.message.reply_text("Час у форматі HH:MM, наприклад 17:45")
        return

    days_tuple = parse_days(days_part)
    if not days_tuple:
        await update.message.reply_text("Не розпізнаю дні. Використай: `пн, вт, ср, чт, пт, сб, нд` або `щодня`.", parse_mode="Markdown")
        return

    # зберігаємо
    save_reminder(chat_id, text_part, days_tuple, f"{hh:02d}:{mm:02d}")

    # видаляємо старі job-и для цього чату
    if context.job_queue:
        for job in context.job_queue.jobs():
            data = getattr(job, "data", None)
            if data == chat_id or (isinstance(data, dict) and data.get("chat_id") == chat_id):
                job.schedule_removal()

        # створюємо нове щоденне завдання з data як словник
        job_time = dtime(hour=hh, minute=mm, tzinfo=KYIV_TZ)
        context.job_queue.run_daily(
            callback=send_scheduled,
            time=job_time,
            days=days_tuple,
            data={"chat_id": chat_id},
            name=f"reminder_{chat_id}"
        )
    else:
        logger.warning("JobQueue не доступний — заплановано лише в БД.")

    days_names = " ".join(WEEKDAY_NAMES[d] for d in days_tuple) if len(days_tuple) < 7 else "щодня"
    await update.message.reply_text(
        f"Нагадування встановлено!\n\nТекст: {text_part}\nДні: {days_names}\nЧас: {hh:02d}:{mm:02d} (Київ)"
    )

async def send_scheduled(context: ContextTypes.DEFAULT_TYPE):
    # підтримка двох форматів data: або простий chat_id, або dict {"chat_id": ...}
    data = getattr(context.job, "data", None)
    if data is None:
        logger.error("Job без data, неможливо визначити chat_id")
        return

    chat_id = data if isinstance(data, int) else data.get("chat_id")
    if not chat_id:
        logger.error("Не знайдено chat_id у job.data: %s", data)
        return

    # знаходимо текст у базі
    reminders = [r for r in get_all_reminders() if r["chat_id"] == chat_id]
    if not reminders:
        logger.info("Нагадування для %s не знайдено у БД — пропускаю", chat_id)
        return

    text = reminders[0]["text"]
    try:
        await context.bot.send_message(chat_id=chat_id, text=text)
        logger.info("Надіслано нагадування до %s", chat_id)
    except Exception as e:
        logger.error("Помилка відправки в %s: %s", chat_id, e)

# ===================== ВІДНОВЛЕННЯ ПІСЛЯ РЕСТАРТУ =====================

async def post_init(application: Application):
    # видаляємо webhook на старті, щоб уникнути конфліктів (409)
    try:
        await application.bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass

    logger.info("Відновлюю нагадування з БД...")
    all_rem = get_all_reminders()
    for r in all_rem:
        try:
            hh, mm = map(int, r["time"].split(":"))
        except Exception:
            logger.warning("Невірний час у записі %s — пропускаю", r)
            continue

        if application.job_queue:
            application.job_queue.run_daily(
                callback=send_scheduled,
                time=dtime(hour=hh, minute=mm, tzinfo=KYIV_TZ),
                days=r["days"],
                data={"chat_id": r["chat_id"]},
                name=f"reminder_{r['chat_id']}"
            )
        else:
            logger.warning("JobQueue відсутній при post_init!")

    logger.info("Відновлено %d нагадувань", len(all_rem))

# ===================== СТАРТ =====================

def main():
    app = Application.builder().token(TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("set", set_reminder))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CommandHandler("list", list_reminders))

    logger.info("Бот запущено")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
