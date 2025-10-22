import os
import asyncio
from datetime import datetime
from fastapi import FastAPI, Request
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from db import ensure_user, create_job, increment_used, init_db
from utils import allowed_file, in_cooldown, set_cooldown_seconds

# Load token from environment variable
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TEMP_PATH = "uploads"

# Initialize DB
init_db()

# Initialize Telegram bot
bot_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

# ---------------- Command Handlers ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user.id)
    await update.message.reply_text("👋 Welcome to TurnitQ! Use /check to upload your document.")

async def check_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📄 Please upload your document (.docx or .pdf).")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user = ensure_user(uid)

    if user.get("cooldown_until"):
        in_cd, rem = in_cooldown(user["cooldown_until"])
        if in_cd:
            await update.message.reply_text(f"⏳ Please wait {rem} seconds before submitting another document.")
            return

    doc = update.message.document
    fname = doc.file_name
    if not allowed_file(fname):
        await update.message.reply_text("❌ Unsupported file type. Send .pdf or .docx")
        return

    os.makedirs(TEMP_PATH, exist_ok=True)
    local_fname = f"{uid}_{int(datetime.utcnow().timestamp())}_{fname}"
    local_path = os.path.join(TEMP_PATH, local_fname)

    file = await doc.get_file()
    await file.download_to_drive(local_path)

    job_id = create_job(uid, local_path)
    increment_used(uid)

    import sqlite3
    conn = sqlite3.connect("turnitq.db")
    c = conn.cursor()
    c.execute("UPDATE users SET cooldown_until=? WHERE telegram_id=?", (set_cooldown_seconds(60), uid))
    conn.commit()
    conn.close()

    await update.message.reply_text("✅ File received. It is queued for processing. You will get a report when ready.")

async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user = ensure_user(uid)
    await update.message.reply_text(
        f"👤 Your Account Info:\nUser ID: {uid}\nPlan: {user['plan']}\nUsed today: {user['used_today']}/{user['daily_limit']}"
    )

# ---------------- FastAPI Setup ----------------
app = FastAPI()

@app.post("/webhook")
async def webhook(req: Request):
    data = await req.json()
    update = Update.de_json(data, bot_app.bot)
    await bot_app.update_queue.put(update)
    return {"ok": True}

@app.get("/")
async def root():
    return {"message": "TurnitQ backend is running!"}

# ---------------- Start bot ----------------
async def start_bot():
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("check", check_cmd))
    bot_app.add_handler(CommandHandler("id", id_cmd))
    bot_app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    await bot_app.initialize()
    await bot_app.start()
    # Set webhook
    await bot_app.bot.set_webhook("https://telegramtask-1.onrender.com/webhook")
    print("Webhook set and bot started.")

# Start bot when FastAPI starts
@app.on_event("startup")
async def startup_event():
    asyncio.create_task(start_bot())
