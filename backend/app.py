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
PAYSTACK_SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY")
PAYSTACK_VERIFY_URL = os.getenv("PAYSTACK_VERIFY_URL", "https://api.paystack.co/transaction/verify/")
TEMP_DIR = Path(os.getenv("TEMP_DIR", "/tmp/turnitq"))
DATABASE = os.getenv("DATABASE_URL", "bot_db.sqlite")
SECRET_KEY = os.getenv("SECRET_KEY", "secret")

if not TELEGRAM_BOT_TOKEN:
    raise SystemExit("Set TELEGRAM_BOT_TOKEN in env")

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
        free_used INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS submissions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        filename TEXT,
        status TEXT,
        created_at INTEGER,
        report_path TEXT
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
    """)
    db.commit()

init_db()

# meta helper
def meta_get(k, default=None):
    cur = db.cursor()
    r = cur.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default

def meta_set(k, v):
    cur = db.cursor()
    cur.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (k, str(v)))
    db.commit()

# Initialize global daily allocation (max 50)
if meta_get("global_alloc") is None:
    meta_set("global_alloc", "0")
if meta_get("global_max") is None:
    meta_set("global_max", "50")

# ---------------------------
# Utilities
# ---------------------------
def now_ts():
    return int(time.time())

def user_get(user_id):
    cur = db.cursor()
    r = cur.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    if not r:
        # create default free user
        expiry = None
        cur.execute("INSERT INTO users(user_id, plan, daily_limit, used_today, expiry_date, last_submission, free_used) VALUES(?,?,?,?,?,?,?)",
                    (user_id, 'free', 1, 0, expiry, 0, 0))
        db.commit()
        r = cur.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    return r

def update_user_usage(user_id, increment=1):
    cur = db.cursor()
    cur.execute("UPDATE users SET used_today = used_today + ? WHERE user_id=?", (increment, user_id))
    db.commit()

def set_user_plan(user_id, plan, daily_limit, days=28):
    expiry = (datetime.datetime.utcnow() + datetime.timedelta(days=days)).isoformat()
    cur = db.cursor()
    cur.execute("UPDATE users SET plan=?, daily_limit=?, expiry_date=?, used_today=0 WHERE user_id=?",
                (plan, daily_limit, expiry, user_id))
    db.commit()

def global_alloc():
    v = int(meta_get("global_alloc", "0"))
    return v

def global_alloc_add(n):
    v = global_alloc() + n
    meta_set("global_alloc", str(v))

def global_alloc_sub(n):
    v = max(0, global_alloc() - n)
    meta_set("global_alloc", str(v))

def allowed_file(filename):
    name = filename.lower()
    return name.endswith(".pdf") or name.endswith(".docx")

# ---------------------------
# Async message sending helper
# ---------------------------
async def send_message_async(chat_id, text, reply_markup=None):
    """Send message using async method"""
    try:
        print(f"Sending message to {chat_id}: {text}")
        await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
        print("Message sent successfully")
        return True
    except Exception as e:
        print(f"Error sending message to {chat_id}: {e}")
        return False

def send_message_sync(chat_id, text, reply_markup=None):
    """Send message synchronously by running async function in event loop"""
    try:
        # Get or create event loop
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        
        # Run the async function in the event loop
        if loop.is_running():
            # If loop is already running, use run_coroutine_threadsafe
            future = asyncio.run_coroutine_threadsafe(
                send_message_async(chat_id, text, reply_markup), 
                loop
            )
            future.result(timeout=10)  # Wait for result with timeout
        else:
            # If loop is not running, run until complete
            loop.run_until_complete(
                send_message_async(chat_id, text, reply_markup)
            )
        return True
    except Exception as e:
        print(f"Error in send_message_sync: {e}")
        return False

# ---------------------------
# Mock processing
# ---------------------------
def mock_process_file(submission_id, file_path, options):
    """
    Simulate Playwright/Selenium processing and create a fake PDF report.
    """
    cur = db.cursor()
    cur.execute("UPDATE submissions SET status=? WHERE id=?", ("processing", submission_id))
    db.commit()

    # Simulate a delay for upload + processing
    time.sleep(8)

    # Create a fake PDF report
    from PyPDF2 import PdfWriter
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    report_path = str(TEMP_DIR / f"report_{submission_id}.pdf")
    with open(report_path, "wb") as f:
        writer.write(f)

    # Save report path and status
    cur.execute("UPDATE submissions SET status=?, report_path=? WHERE id=?", ("done", report_path, submission_id))
    db.commit()

    # Send report back to user
    r = cur.execute("SELECT user_id, filename FROM submissions WHERE id=?", (submission_id,)).fetchone()
    if r:
        user_id = r["user_id"]
        try:
            caption = f"✅ Report ready for {r['filename']}\nSimilarity: {10 + (submission_id % 10)}%\nAI Score: {5 + (submission_id % 5)}%"
            with open(report_path, "rb") as f:
                # Use sync method for background threads
                asyncio.run(bot.send_document(
                    chat_id=user_id, 
                    document=InputFile(f, filename=os.path.basename(report_path)), 
                    caption=caption
                ))
        except Exception as e:
            print("Error sending report:", e)

def start_processing(submission_id, file_path, options):
    t = threading.Thread(target=mock_process_file, args=(submission_id, file_path, options), daemon=True)
    t.start()

# ---------------------------
# Flask routes
# ---------------------------
@app.route(f"/webhook/{TELEGRAM_BOT_TOKEN}", methods=["POST"])
def telegram_webhook():
    try:
        update_data = request.get_json(force=True)
        print(f"Received webhook from user")
        
        update = Update.de_json(update_data, bot)
        
        if update.message:
            user_id = update.message.from_user.id
            text = update.message.text or ""
            
            print(f"Processing message from user {user_id}: {text}")
            
            # Handle commands
            if text.startswith("/start"):
                send_message_sync(user_id, "👋 Welcome to TurnitQ!\nUpload your document to check its originality instantly.\nUse /check to begin.")
                return "ok", 200
                
            if text.startswith("/id"):
                u = user_get(user_id)
                reply = f"👤 Your Account Info:\nUser ID: {user_id}\nPlan: {u['plan']}\nDaily Total Checks: {u['daily_limit']} - {u['used_today']}\nSubscription ends: {u['expiry_date'] or 'N/A'}"
                send_message_sync(user_id, reply)
                return "ok", 200
                
            if text.startswith("/upgrade"):
                plan = "Premium"
                plan_checks = 5
                galloc = global_alloc()
                gmax = int(meta_get("global_max", "50"))
                if galloc + plan_checks > gmax:
                    send_message_sync(user_id, "Sorry, that plan is full right now. Please try a smaller plan or check back later.")
                    return "ok", 200
                    
                now = now_ts()
                expires = now + 10*60
                cur = db.cursor()
                cur.execute("INSERT INTO reservations(user_id, plan, created_at, expires_at, reference) VALUES(?,?,?,?,?)",
                            (user_id, plan, now, expires, "tempref_"+str(now)))
                db.commit()
                global_alloc_add(plan_checks)
                
                pay_link = f"https://paystack.com/pay/fakepay?ref=tempref_{now}"
                markup = InlineKeyboardMarkup([[InlineKeyboardButton("Pay (Sandbox)", url=pay_link)]])
                send_message_sync(user_id, f"Your slot is reserved for 10 minutes. Click the button to pay for {plan}.", reply_markup=markup)
                return "ok", 200
                
            if text.startswith("/cancel"):
                cur = db.cursor()
                cur.execute("SELECT id FROM submissions WHERE user_id=? AND status='processing' ORDER BY created_at DESC LIMIT 1", (user_id,))
                row = cur.fetchone()
                if row:
                    cur.execute("UPDATE submissions SET status=? WHERE id=?", ("cancelled", row["id"]))
                    db.commit()
                    send_message_sync(user_id, "❌ Your check has been cancelled.")
                else:
                    send_message_sync(user_id, "You have no running checks.")
                return "ok", 200

            # Handle file uploads
            if update.message.document:
                doc = update.message.document
                filename = doc.file_name or f"file_{now_ts()}"
                if not allowed_file(filename):
                    send_message_sync(user_id, "⚠️ Only .pdf and .docx files are allowed.")
                    return "ok", 200

                u = user_get(user_id)
                last = u["last_submission"] or 0
                if now_ts() - last < 60:
                    send_message_sync(user_id, "⏳ Please wait 1 minute before submitting another document.")
                    return "ok", 200

                if u["used_today"] >= u["daily_limit"]:
                    send_message_sync(user_id, "⚠️ You've reached your daily limit. Subscribe to continue using TurnitQ.")
                    return "ok", 200

                # Download file
                file_obj = bot.get_file(doc.file_id)
                local_path = str(TEMP_DIR / f"{user_id}_{int(time.time())}_{filename}")
                try:
                    file_obj.download(custom_path=local_path)
                    print(f"File downloaded to: {local_path}")
                except Exception as e:
                    send_message_sync(user_id, "Failed to download file. Try again.")
                    print("Download error:", e)
                    return "ok", 200

                # Create submission record
                created = now_ts()
                cur = db.cursor()
                cur.execute("INSERT INTO submissions(user_id, filename, status, created_at) VALUES(?,?,?,?)",
                            (user_id, filename, "queued", created))
                sub_id = cur.lastrowid
                db.commit()

                cur.execute("UPDATE users SET last_submission=?, used_today=used_today+1 WHERE user_id=?", (created, user_id))
                db.commit()

                send_message_sync(user_id, "✅ File received. Checking with TurnitQ — please wait a few seconds…")
                start_processing(sub_id, local_path, options={})
                return "ok", 200

        return "ok", 200
        
    except Exception as e:
        print(f"Error in telegram_webhook: {e}")
        import traceback
        traceback.print_exc()
        return "error", 500

@app.route("/")
def greet():
    return "Bot is running!"

@app.route("/paystack/webhook", methods=["POST"])
def paystack_webhook():
    try:
        data = request.get_json(force=True)
        if not data:
            return "", 400
            
        ref = data.get("data", {}).get("reference")
        
        cur = db.cursor()
        row = cur.execute("SELECT * FROM reservations WHERE reference=? OR reference LIKE ?", (ref, f"%{ref}%")).fetchone()
        if not row:
            return jsonify({"status":"ok","note":"reservation not found"}), 200

        user_id = row["user_id"]
        plan = row["plan"]
        plan_map = {"Premium": (5, 28), "Pro": (30, 28), "Elite": (100, 28)}
        checks, days = plan_map.get(plan, (5, 28))
        
        set_user_plan(user_id, plan, checks, days=days)
        cur.execute("DELETE FROM reservations WHERE id=?", (row["id"],))
        db.commit()
        
        send_message_sync(user_id, f"✅ You're now on {plan}!\nActive until { (datetime.datetime.utcnow() + datetime.timedelta(days=days)).date() }\nYou have {checks} checks per day.")
        return jsonify({"status":"success"}), 200
        
    except Exception as e:
        print("Error in paystack_webhook:", e)
        return jsonify({"status":"error"}), 500

@app.route("/admin/stats", methods=["GET"])
def admin_stats():
    cur = db.cursor()
    total_users = cur.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
    alloc = global_alloc()
    return jsonify({"total_users": total_users, "global_alloc": alloc})

# ---------------------------
# Scheduler
# ---------------------------
scheduler = BackgroundScheduler()

def reset_daily():
    cur = db.cursor()
    cur.execute("UPDATE users SET used_today=0")
    db.commit()
    meta_set("global_alloc", "0")
    print("Daily reset performed at", datetime.datetime.utcnow())

scheduler.add_job(reset_daily, 'cron', hour=0)
scheduler.start()

def set_webhook():
    try:
        url = f"{WEBHOOK_BASE_URL}/webhook/{TELEGRAM_BOT_TOKEN}"
        print(f"Setting webhook to: {url}")
        r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook", params={"url": url})
        print("setWebhook response:", r.text)
    except Exception as e:
        print(f"Error setting webhook: {e}")

if __name__ == "__main__":
    set_webhook()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))