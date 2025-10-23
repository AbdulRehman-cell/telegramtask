import os
import time
import json
import threading
import tempfile
import datetime
import sqlite3
from pathlib import Path
from functools import wraps
import asyncio

from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
import requests
from telegram import Bot, Update, InputFile, InlineKeyboardButton, InlineKeyboardMarkup

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
WEBHOOK_BASE_URL = os.getenv("WEBHOOK_BASE_URL", "").rstrip("/")
DATABASE = os.getenv("DATABASE_URL", "bot_db.sqlite")
SECRET_KEY = os.getenv("SECRET_KEY", "secret")

if not TELEGRAM_BOT_TOKEN:
    raise SystemExit("❌ Set TELEGRAM_BOT_TOKEN in env")

print(f"🤖 Bot token: {TELEGRAM_BOT_TOKEN[:10]}...")
print(f"🌐 Webhook base: {WEBHOOK_BASE_URL}")

TEMP_DIR = Path(os.getenv("TEMP_DIR", "/tmp/turnitq"))
TEMP_DIR.mkdir(parents=True, exist_ok=True)

# Initialize bot
bot = Bot(token=TELEGRAM_BOT_TOKEN)
app = Flask(__name__)
app.config['SECRET_KEY'] = SECRET_KEY

# ---------------------------
# Database setup
# ---------------------------
def get_db():
    conn = sqlite3.connect(DATABASE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

db = get_db()

def init_db():
    cur = db.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        plan TEXT DEFAULT 'free',
        daily_limit INTEGER DEFAULT 1,
        used_today INTEGER DEFAULT 0,
        expiry_date TEXT,
        last_submission INTEGER DEFAULT 0,
        free_checks_used INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS submissions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        filename TEXT,
        status TEXT,
        created_at INTEGER,
        report_path TEXT,
        options TEXT,
        is_free_check BOOLEAN DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS reservations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        plan TEXT,
        created_at INTEGER,
        expires_at INTEGER,
        reference TEXT
    );
    CREATE TABLE IF NOT EXISTS meta (
        k TEXT PRIMARY KEY,
        v TEXT
    );
    CREATE TABLE IF NOT EXISTS user_sessions (
        user_id INTEGER PRIMARY KEY,
        waiting_for_options BOOLEAN DEFAULT 0,
        current_file_path TEXT,
        current_filename TEXT
    );
    """)
    db.commit()

init_db()

# Initialize global daily allocation
if not db.execute("SELECT 1 FROM meta WHERE k='global_alloc'").fetchone():
    db.execute("INSERT INTO meta(k,v) VALUES('global_alloc','0')")
    db.execute("INSERT INTO meta(k,v) VALUES('global_max','50')")
    db.commit()

# ---------------------------
# Utilities
# ---------------------------
def now_ts():
    return int(time.time())

def user_get(user_id):
    cur = db.cursor()
    r = cur.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    if not r:
        cur.execute("INSERT INTO users(user_id) VALUES(?)", (user_id,))
        db.commit()
        r = cur.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    return r

def get_user_session(user_id):
    cur = db.cursor()
    r = cur.execute("SELECT * FROM user_sessions WHERE user_id=?", (user_id,)).fetchone()
    if not r:
        cur.execute("INSERT INTO user_sessions(user_id) VALUES(?)", (user_id,))
        db.commit()
        r = cur.execute("SELECT * FROM user_sessions WHERE user_id=?", (user_id,)).fetchone()
    return r

def update_user_session(user_id, **kwargs):
    cur = db.cursor()
    set_clause = ", ".join([f"{k}=?" for k in kwargs.keys()])
    values = list(kwargs.values()) + [user_id]
    cur.execute(f"UPDATE user_sessions SET {set_clause} WHERE user_id=?", values)
    db.commit()

def allowed_file(filename):
    return filename.lower().endswith((".pdf", ".docx"))

# ---------------------------
# Message sending - SIMPLE SYNC VERSION
# ---------------------------
def send_telegram_message(chat_id, text, reply_markup=None):
    """Send message using requests directly to Telegram API"""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML"
        }
        
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup.to_dict())
        
        response = requests.post(url, json=payload, timeout=10)
        result = response.json()
        
        if result.get("ok"):
            print(f"✅ Message sent to {chat_id}")
            return True
        else:
            print(f"❌ Telegram API error: {result}")
            return False
            
    except Exception as e:
        print(f"❌ Error sending message: {e}")
        return False

# ---------------------------
# Report Options and Processing
# ---------------------------
def ask_for_report_options(user_id):
    """Ask user for report customization options"""
    options_message = (
        "📊 Before generating your report, please choose what to include:\n\n"
        "1️⃣ Exclude bibliography — Yes/No\n"
        "2️⃣ Exclude quoted text — Yes/No\n"
        "3️⃣ Exclude cited text — Yes/No\n"
        "4️⃣ Exclude small matches — Yes/No\n\n"
        "Please reply with your choices (e.g.: Yes, No, Yes, Yes)"
    )
    
    send_telegram_message(user_id, options_message)
    update_user_session(user_id, waiting_for_options=1)

def parse_options_response(text):
    """Parse user's options response"""
    try:
        parts = [part.strip().lower() for part in text.split(',')]
        if len(parts) != 4:
            return None
        
        options = {
            "exclude_bibliography": parts[0] == 'yes',
            "exclude_quoted_text": parts[1] == 'yes', 
            "exclude_cited_text": parts[2] == 'yes',
            "exclude_small_matches": parts[3] == 'yes'
        }
        return options
    except:
        return None

def mock_process_file(submission_id, file_path, options):
    """Simulate file processing with options"""
    try:
        cur = db.cursor()
        cur.execute("UPDATE submissions SET status=? WHERE id=?", ("processing", submission_id))
        db.commit()

        # Get user info
        r = cur.execute("SELECT user_id, filename, is_free_check FROM submissions WHERE id=?", (submission_id,)).fetchone()
        if not r:
            return
            
        user_id = r["user_id"]
        filename = r["filename"]
        is_free_check = r["is_free_check"]

        # Simulate processing time
        send_telegram_message(user_id, "⏳ Generating your Turnitin report with your selected preferences...")
        time.sleep(8)

        # Create fake reports
        similarity_report_path = str(TEMP_DIR / f"similarity_report_{submission_id}.pdf")
        ai_report_path = str(TEMP_DIR / f"ai_report_{submission_id}.pdf")
        
        # Create fake PDFs
        from PyPDF2 import PdfWriter
        
        # Similarity report
        writer1 = PdfWriter()
        writer1.add_blank_page(width=600, height=800)
        with open(similarity_report_path, "wb") as f:
            writer1.write(f)
            
        # AI report  
        writer2 = PdfWriter()
        writer2.add_blank_page(width=600, height=800)
        with open(ai_report_path, "wb") as f:
            writer2.write(f)

        # Update submission
        cur.execute("UPDATE submissions SET status=?, report_path=? WHERE id=?", ("done", similarity_report_path, submission_id))
        db.commit()

        # Send reports to user
        similarity_score = 10 + (submission_id % 15)  # 10-25%
        ai_score = 5 + (submission_id % 10)  # 5-15%
        
        caption = (
            f"✅ Report ready for {filename}!\n\n"
            f"📊 Similarity Score: {similarity_score}%\n"
            f"🤖 AI Detection Score: {ai_score}%\n\n"
            f"Options used:\n"
            f"• Exclude bibliography: {'Yes' if options['exclude_bibliography'] else 'No'}\n"
            f"• Exclude quoted text: {'Yes' if options['exclude_quoted_text'] else 'No'}\n"
            f"• Exclude cited text: {'Yes' if options['exclude_cited_text'] else 'No'}\n"
            f"• Exclude small matches: {'Yes' if options['exclude_small_matches'] else 'No'}"
        )
        
        # Send similarity report
        with open(similarity_report_path, "rb") as f:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
            files = {'document': (f"similarity_report_{filename}.pdf", f)}
            data = {'chat_id': user_id, 'caption': caption}
            requests.post(url, files=files, data=data)
        
        # Send AI report
        with open(ai_report_path, "rb") as f:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
            files = {'document': (f"ai_report_{filename}.pdf", f)}
            data = {'chat_id': user_id, 'caption': "🤖 AI Writing Analysis Report"}
            requests.post(url, files=files, data=data)
        
        # Show upgrade message if it was a free check
        if is_free_check:
            upgrade_markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("💎 Upgrade Plan", callback_data="upgrade_after_free")]
            ])
            send_telegram_message(
                user_id,
                "🎁 Your first check was free!\n\n"
                "To unlock more checks and full reports for the next 28 days, upgrade below 👇",
                reply_markup=upgrade_markup
            )
            
    except Exception as e:
        print(f"❌ Processing error: {e}")
        import traceback
        traceback.print_exc()

def start_processing(submission_id, file_path, options):
    t = threading.Thread(target=mock_process_file, args=(submission_id, file_path, options), daemon=True)
    t.start()

# ---------------------------
# Flask routes
# ---------------------------
@app.route("/")
def home():
    webhook_url = f"{WEBHOOK_BASE_URL}/webhook/{TELEGRAM_BOT_TOKEN}"
    return f"""
    <h1>TurnitQ Bot</h1>
    <p>Status: 🟢 Running</p>
    <p>Webhook: <code>{webhook_url}</code></p>
    <p><a href="/debug">Debug Info</a></p>
    """

@app.route("/debug")
def debug():
    webhook_url = f"{WEBHOOK_BASE_URL}/webhook/{TELEGRAM_BOT_TOKEN}"
    webhook_status = "❓ Unknown"
    
    try:
        response = requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getWebhookInfo")
        webhook_info = response.json()
        webhook_status = "✅ Active" if webhook_info.get("result", {}).get("url") else "❌ Inactive"
    except:
        webhook_status = "❌ Error checking"
    
    return f"""
    <h1>Debug Information</h1>
    <p><strong>Webhook Status:</strong> {webhook_status}</p>
    <p><strong>Webhook URL:</strong> <code>{webhook_url}</code></p>
    <p><strong>Bot Token:</strong> <code>{TELEGRAM_BOT_TOKEN[:10]}...</code></p>
    <p><a href="https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getWebhookInfo" target="_blank">View Detailed Webhook Info</a></p>
    """

@app.route(f"/webhook/{TELEGRAM_BOT_TOKEN}", methods=["POST", "GET"])
def telegram_webhook():
    if request.method == "GET":
        return "Webhook is active! Send POST requests here."
    
    try:
        update_data = request.get_json(force=True)
        print(f"📥 Received update: {json.dumps(update_data, indent=2)}")
        
        update = Update.de_json(update_data, bot)
        
        if update.message:
            user_id = update.message.from_user.id
            text = update.message.text or ""
            
            print(f"👤 User {user_id} sent: {text}")
            
            # Check if user is waiting for options
            session = get_user_session(user_id)
            if session['waiting_for_options']:
                options = parse_options_response(text)
                if options:
                    # Process the file with options
                    update_user_session(user_id, waiting_for_options=0)
                    
                    # Create submission
                    created = now_ts()
                    cur = db.cursor()
                    
                    # Check if it's user's first free check
                    user_data = user_get(user_id)
                    is_free_check = user_data['free_checks_used'] == 0
                    
                    cur.execute(
                        "INSERT INTO submissions(user_id, filename, status, created_at, options, is_free_check) VALUES(?,?,?,?,?,?)",
                        (user_id, session['current_filename'], "queued", created, json.dumps(options), is_free_check)
                    )
                    sub_id = cur.lastrowid
                    
                    # Update user usage
                    cur.execute(
                        "UPDATE users SET last_submission=?, used_today=used_today+1, free_checks_used=free_checks_used+? WHERE user_id=?",
                        (created, 1 if is_free_check else 0, user_id)
                    )
                    db.commit()

                    send_telegram_message(user_id, "✅ File received. Checking with Turnitin — please wait a few seconds…")
                    
                    # Start processing with options
                    start_processing(sub_id, session['current_file_path'], options)
                    return "ok", 200
                else:
                    send_telegram_message(
                        user_id,
                        "❌ Invalid format. Please reply with 4 choices separated by commas.\n\n"
                        "Example: Yes, No, Yes, Yes\n\n"
                        "1. Exclude bibliography\n"
                        "2. Exclude quoted text\n" 
                        "3. Exclude cited text\n"
                        "4. Exclude small matches"
                    )
                    return "ok", 200
            
            # Handle commands
            if text.startswith("/start"):
                send_telegram_message(
                    user_id, 
                    "👋 Welcome to TurnitQ!\n\n"
                    "I can check your documents for originality and AI writing.\n\n"
                    "Available commands:\n"
                    "/check - Start a new document check\n"
                    "/id - Your account info\n"
                    "/upgrade - Upgrade your plan\n"
                    "/cancel - Cancel current check"
                )
                return "ok", 200
                
            elif text.startswith("/check"):
                send_telegram_message(
                    user_id,
                    "📄 Please upload your document (.docx or .pdf).\n"
                    "Only one file can be processed at a time."
                )
                return "ok", 200
                
            elif text.startswith("/id"):
                u = user_get(user_id)
                reply = (
                    f"👤 Your Account Info:\n"
                    f"User ID: {user_id}\n"
                    f"Plan: {u['plan']}\n"
                    f"Used today: {u['used_today']}/{u['daily_limit']}\n"
                    f"Free checks used: {u['free_checks_used']}\n"
                    f"Subscription: {u['expiry_date'] or 'Free tier'}"
                )
                send_telegram_message(user_id, reply)
                return "ok", 200
                
            elif text.startswith("/upgrade"):
                markup = InlineKeyboardMarkup([
                    [InlineKeyboardButton("💎 Premium - 5 checks/day", callback_data="premium")],
                    [InlineKeyboardButton("🚀 Pro - 30 checks/day", callback_data="pro")],
                    [InlineKeyboardButton("🏆 Elite - 100 checks/day", callback_data="elite")]
                ])
                send_telegram_message(
                    user_id,
                    "📊 Choose your plan:\n\n"
                    "💎 Premium: 5 checks per day\n"
                    "🚀 Pro: 30 checks per day\n"
                    "🏆 Elite: 100 checks per day\n\n"
                    "Click a button below to upgrade:",
                    reply_markup=markup
                )
                return "ok", 200
                
            elif text.startswith("/cancel"):
                send_telegram_message(user_id, "❌ No active checks to cancel.")
                return "ok", 200

            # Handle file uploads
            elif update.message.document:
                doc = update.message.document
                filename = doc.file_name or f"file_{now_ts()}"
                
                if not allowed_file(filename):
                    send_telegram_message(user_id, "⚠️ Only .pdf and .docx files are allowed.")
                    return "ok", 200

                u = user_get(user_id)
                
                # Daily limit check
                if u["used_today"] >= u["daily_limit"]:
                    send_telegram_message(
                        user_id,
                        "⚠️ You've reached your daily limit!\n\n"
                        "Use /upgrade to get more checks per day."
                    )
                    return "ok", 200

                # Download file
                try:
                    file_obj = bot.get_file(doc.file_id)
                    local_path = str(TEMP_DIR / f"{user_id}_{now_ts()}_{filename}")
                    file_obj.download(custom_path=local_path)
                    print(f"📥 File saved: {local_path}")
                except Exception as e:
                    send_telegram_message(user_id, "❌ Failed to download file. Please try again.")
                    print(f"Download error: {e}")
                    return "ok", 200

                # Store file info and ask for options
                update_user_session(
                    user_id, 
                    waiting_for_options=1,
                    current_file_path=local_path,
                    current_filename=filename
                )
                
                ask_for_report_options(user_id)
                return "ok", 200

            else:
                # Handle any other text
                send_telegram_message(
                    user_id,
                    "🤔 I didn't understand that.\n\n"
                    "Try one of these commands:\n"
                    "/start - Welcome message\n"
                    "/check - Start a check\n"
                    "/id - Your account info\n"
                    "/upgrade - Upgrade plan"
                )
                return "ok", 200

        # Handle callback queries (button clicks)
        if update.callback_query:
            user_id = update.callback_query.from_user.id
            data = update.callback_query.data
            
            if data == "premium":
                send_telegram_message(user_id, "💎 You selected Premium plan!\n\nThis would redirect to payment in a real implementation.")
            elif data == "pro":
                send_telegram_message(user_id, "🚀 You selected Pro plan!\n\nThis would redirect to payment in a real implementation.")
            elif data == "elite":
                send_telegram_message(user_id, "🏆 You selected Elite plan!\n\nThis would redirect to payment in a real implementation.")
            elif data == "upgrade_after_free":
                markup = InlineKeyboardMarkup([
                    [InlineKeyboardButton("💎 Premium - 5 checks/day", callback_data="premium")],
                    [InlineKeyboardButton("🚀 Pro - 30 checks/day", callback_data="pro")],
                    [InlineKeyboardButton("🏆 Elite - 100 checks/day", callback_data="elite")]
                ])
                send_telegram_message(
                    user_id,
                    "📊 Choose your upgrade plan:\n\n"
                    "💎 Premium: 5 checks per day\n"
                    "🚀 Pro: 30 checks per day\n"
                    "🏆 Elite: 100 checks per day",
                    reply_markup=markup
                )
                
        return "ok", 200
        
    except Exception as e:
        print(f"❌ Webhook error: {e}")
        import traceback
        traceback.print_exc()
        return "error", 500

# ---------------------------
# Setup webhook
# ---------------------------
def setup_webhook():
    """Set up Telegram webhook"""
    try:
        webhook_url = f"{WEBHOOK_BASE_URL}/webhook/{TELEGRAM_BOT_TOKEN}"
        print(f"🔗 Setting webhook to: {webhook_url}")
        
        response = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook",
            params={"url": webhook_url, "drop_pending_updates": True}
        )
        
        result = response.json()
        print(f"📡 Webhook setup result: {result}")
        
        if result.get("ok"):
            print("✅ Webhook set successfully!")
        else:
            print(f"❌ Failed to set webhook: {result}")
            
    except Exception as e:
        print(f"❌ Webhook setup error: {e}")

# ---------------------------
# Scheduler
# ---------------------------
scheduler = BackgroundScheduler()

def reset_daily_usage():
    db.execute("UPDATE users SET used_today=0")
    db.execute("UPDATE meta SET v='0' WHERE k='global_alloc'")
    db.commit()
    print("🔄 Daily usage reset")

scheduler.add_job(reset_daily_usage, 'cron', hour=0)
scheduler.start()

# ---------------------------
# Startup
# ---------------------------
if __name__ == "__main__":
    print("🚀 Starting TurnitQ Bot...")
    setup_webhook()
    port = int(os.environ.get("PORT", 5000))
    print(f"🌐 Server starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)