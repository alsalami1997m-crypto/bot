import os
import uuid
import sqlite3
import asyncio
import time
from datetime import datetime

from flask import Flask, request

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler,
    MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

import yt_dlp

# ---------------- CONFIG ----------------
ADMIN_ID = int(os.getenv('ADMIN_ID', 0))
BOT_TOKEN = os.getenv("BOT_TOKEN")
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL")

if not BOT_TOKEN:
    raise Exception("BOT_TOKEN غير موجود")

# ---------------- DATABASE ----------------
conn = sqlite3.connect("bot_data.db", check_same_thread=False)

def db_execute(query, params=(), fetch=False):
    try:
        cursor = conn.cursor()
        cursor.execute(query, params)
        conn.commit()
        if fetch:
            return cursor.fetchall()
    except Exception as e:
        print("DB ERROR:", e)
        return []

# ---------------- TABLES ----------------
db_execute("""CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    last_name TEXT,
    approved INTEGER DEFAULT 0,
    banned INTEGER DEFAULT 0,
    join_date TEXT,
    downloads INTEGER DEFAULT 0
)""")

db_execute("""CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT
)""")

db_execute("""CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
)""")

db_execute("INSERT OR IGNORE INTO settings (key,value) VALUES ('force_sub','0')")

# ---------------- STATE ----------------
user_state = {}
last_request = {}

# ---------------- FORCE SUB ----------------
async def is_subscribed(bot, user_id):
    channels = db_execute("SELECT channel FROM channels", fetch=True)
    if not channels:
        return True

    for (channel,) in channels:
        try:
            member = await bot.get_chat_member(chat_id=channel, user_id=user_id)
            if member.status in ["left", "kicked"]:
                return False
        except:
            return False
    return True

# ---------------- ADMIN KEYBOARD ----------------
def admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 المستخدمين", callback_data="users"),
         InlineKeyboardButton("📊 الإحصائيات", callback_data="stats")],
        [InlineKeyboardButton("📢 رسالة جماعية", callback_data="broadcast")],
        [InlineKeyboardButton("📣 القنوات", callback_data="channels"),
         InlineKeyboardButton("➕ إضافة قناة", callback_data="add_channel")],
        [InlineKeyboardButton("⚙️ تفعيل الاشتراك", callback_data="enable_force"),
         InlineKeyboardButton("❎ تعطيل الاشتراك", callback_data="disable_force")]
    ])

# ---------------- START ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    user = update.message.from_user
    user_id = user.id

    if user_id == ADMIN_ID:
        await update.message.reply_text("لوحة التحكم 👇", reply_markup=admin_keyboard())
        return

    row = db_execute("SELECT approved,banned FROM users WHERE id=?", (user_id,), fetch=True)

    if not row:
        db_execute("""INSERT INTO users VALUES (?, ?, ?, ?, 0, 0, ?, 0)""",
        (user_id, user.username, user.first_name, user.last_name, datetime.now().strftime("%Y-%m-%d")))

        await context.bot.send_message(ADMIN_ID, f"طلب جديد: {user_id}")
        await update.message.reply_text("⏳ بانتظار الموافقة")
        return

    approved, banned = row[0]

    if banned:
        await update.message.reply_text("🚫 محظور")
    elif approved:
        await update.message.reply_text("📎 أرسل الرابط")
    else:
        await update.message.reply_text("⏳ بانتظار الموافقة")

# ---------------- DOWNLOAD ----------------
async def download_video(url, update, context):
    user_id = update.message.from_user.id
    file_id = str(uuid.uuid4())

    ydl_opts = {'outtmpl': f"{file_id}.%(ext)s", 'format': 'best', 'quiet': True}

    loop = asyncio.get_event_loop()

    def run():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            return ydl.prepare_filename(info)

    filename = None

    try:
        filename = await loop.run_in_executor(None, run)

        if os.path.getsize(filename) > 49 * 1024 * 1024:
            await update.message.reply_text("❗ الملف كبير")
            return

        with open(filename, 'rb') as v:
            await update.message.reply_video(v)

        db_execute("UPDATE users SET downloads=downloads+1 WHERE id=?", (user_id,))

    except:
        await update.message.reply_text("❌ فشل التحميل")

    finally:
        if filename and os.path.exists(filename):
            os.remove(filename)

# ---------------- TEXT ----------------
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    user_id = update.message.from_user.id
    text = update.message.text

    if user_id in last_request and time.time() - last_request[user_id] < 5:
        await update.message.reply_text("⏳ انتظر")
        return

    last_request[user_id] = time.time()

    if not text.startswith(("http://", "https://")):
        return

    await update.message.reply_text("⏳ جاري التحميل...")
    await download_video(text, update, context)

# ---------------- CALLBACK ----------------
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        return

    if query.data == "broadcast":
        user_state[query.from_user.id] = "broadcast"
        await query.message.reply_text("✉️ أرسل الرسالة")

# ---------------- APP ----------------
app = Flask(__name__)

telegram_app = ApplicationBuilder().token(BOT_TOKEN).build()

telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CallbackQueryHandler(callback_handler))
telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

@app.route(f"/{BOT_TOKEN}", methods=["POST"])
async def webhook():
    data = request.get_json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return "ok"

@app.route("/")
def home():
    return "Bot is running"

# ---------------- START SERVER ----------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))

    loop = asyncio.get_event_loop()
    loop.run_until_complete(
        telegram_app.bot.set_webhook(f"{RENDER_URL}/{BOT_TOKEN}")
    )

    app.run(host="0.0.0.0", port=port)
