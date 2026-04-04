import os
import uuid
import sqlite3
import asyncio
import time
import logging
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler,
    MessageHandler, CallbackQueryHandler,
    filters, ContextTypes
)

import yt_dlp

# ---------------- LOGGING ----------------
logging.basicConfig(level=logging.INFO)

# ---------------- CONFIG ----------------
ADMIN_ID = int(os.getenv('ADMIN_ID', 0))
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise Exception("BOT_TOKEN غير موجود في البيئة")

BOT_TOKEN = BOT_TOKEN.strip()

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
        return [] if fetch else None

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
        except Exception as e:
            print("SUB CHECK ERROR:", e)
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

    force_sub_row = db_execute("SELECT value FROM settings WHERE key='force_sub'", fetch=True)
    force_sub = force_sub_row[0][0] if force_sub_row else "0"

    if force_sub == '1':
        if not await is_subscribed(context.bot, user_id):
            channels = db_execute("SELECT channel FROM channels", fetch=True)

            buttons = []
            text = "❌ يجب الاشتراك:\n\n"

            for (ch,) in channels:
                text += f"- {ch}\n"
                buttons.append([InlineKeyboardButton(ch, url=f"https://t.me/{ch.replace('@','')}")])

            buttons.append([InlineKeyboardButton("✅ تحقق", callback_data="check_sub")])

            await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))
            return

    row = db_execute("SELECT approved,banned FROM users WHERE id=?", (user_id,), fetch=True)

    if not row:
        db_execute("""INSERT INTO users (id, username, first_name, last_name, approved, banned, join_date, downloads)
        VALUES (?, ?, ?, ?, 0, 0, ?, 0)""",
        (
            user_id,
            user.username or "",
            user.first_name or "",
            user.last_name or "",
            datetime.now().strftime("%Y-%m-%d")
        ))

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

    ydl_opts = {
        'outtmpl': f"{file_id}.%(ext)s",
        'format': 'best',
        'quiet': True
    }

    loop = asyncio.get_event_loop()

    def run():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            return ydl.prepare_filename(info)

    filename = None

    try:
        filename = await loop.run_in_executor(None, run)

        if os.path.getsize(filename) > 49 * 1024 * 1024:
            await update.message.reply_text("❗ الملف كبير جداً")
            return

        with open(filename, 'rb') as v:
            await update.message.reply_video(v)

        db_execute("UPDATE users SET downloads=downloads+1 WHERE id=?", (user_id,))

    except Exception as e:
        print("DOWNLOAD ERROR:", e)
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

    if user_id in last_request:
        if time.time() - last_request[user_id] < 5:
            await update.message.reply_text("⏳ انتظر قليلاً")
            return

    last_request[user_id] = time.time()

    # broadcast
    if user_id in user_state:
        state = user_state[user_id]

        if state == "broadcast":
            users = db_execute("SELECT id FROM users", fetch=True)
            for (uid,) in users:
                try:
                    await context.bot.send_message(chat_id=uid, text=text)
                except:
                    pass
            await update.message.reply_text("📢 تم الإرسال")

        del user_state[user_id]
        return

    if not text.startswith(("http://", "https://")):
        return

    row = db_execute("SELECT approved,banned FROM users WHERE id=?", (user_id,), fetch=True)

    if not row or row[0][1] or not row[0][0]:
        return

    await update.message.reply_text("⏳ جاري التحميل...")
    await download_video(text, update, context)

# ---------------- CALLBACK ----------------
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    uid = query.from_user.id
    data = query.data

    if uid != ADMIN_ID:
        await query.answer("❌ ليس لديك صلاحية", show_alert=True)
        return

    if data == "broadcast":
        user_state[uid] = "broadcast"
        await query.message.reply_text("✉️ أرسل الرسالة الآن")
        return

    if data == "users":
        users = db_execute("SELECT COUNT(*) FROM users", fetch=True)
        count = users[0][0] if users else 0
        await query.message.reply_text(f"👥 عدد المستخدمين: {count}")
        return

    if data == "enable_force":
        db_execute("UPDATE settings SET value='1' WHERE key='force_sub'")
        await query.message.reply_text("✅ تم تفعيل الاشتراك الإجباري")
        return

    if data == "disable_force":
        db_execute("UPDATE settings SET value='0' WHERE key='force_sub'")
        await query.message.reply_text("❎ تم تعطيل الاشتراك")
        return

# ---------------- RUN ----------------
def main():
    print("🚀 Bot is running...")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # 🔥 التعديل المهم لـ Railway
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
