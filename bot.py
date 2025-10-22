# bot.py
import os
import asyncio
from datetime import datetime
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from db import ensure_user, create_job, increment_used, init_db
from utils import allowed_file, in_cooldown, set_cooldown_seconds

TEMP_PATH = "uploads"
TELEGRAM_TOKEN = os.getenv("8291206067:AAFffXWUa7u5FBCqoUnOySIDre9KwpNXP3g")

# Initialize database
init_db()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user.id)
    await update.message.reply_text("👋 Welcome to TurnitQ! Use /check to upload your document.")


async def check_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📄 Please upload your document (.docx or .pdf).")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user = ensure_user(uid)

    # Check cooldown
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

    # Set cooldown in DB
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
        f"👤 Your Account Info:\n"
        f"User ID: {uid}\n"
        f"Plan: {user['plan']}\n"
        f"Used today: {user['used_today']}/{user['daily_limit']}"
    )


async def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("check", check_cmd))
    app.add_handler(CommandHandler("id", id_cmd))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    print("Bot starting...")
    await app.initialize()
    await app.start()
    await app.updater.start_polling()


if __name__ == "__main__":
    asyncio.run(main())
