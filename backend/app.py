import os
import time
import json
import threading
import tempfile
import datetime
import sqlite3
from pathlib import Path
from functools import wraps

from flask import Flask, request, jsonify, abort
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
import requests
from telegram import Bot, Update, InputFile, KeyboardButton, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup

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

bot = Bot(token=TELEGRAM_BOT_TOKEN)
app = Flask(__name__)
app.config['SECRET_KEY'] = SECRET_KEY

# ---------------------------
# Simple SQLite helpers
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
# Mock processing (replace this with real automation later)
# ---------------------------
def mock_process_file(submission_id, file_path, options):
    """
    Simulate Playwright/Selenium processing and create a fake PDF report.
    """
    cur = db.cursor()
    cur.execute("UPDATE submissions SET status=? WHERE id=?", ("processing", submission_id))
    db.commit()

    # Simulate a delay for upload + processing
    time.sleep(8)  # simulate upload + waiting for report

    # Create a fake PDF report (a tiny PDF)
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
                bot.send_document(chat_id=user_id, document=InputFile(f, filename=os.path.basename(report_path)), caption=caption)
        except Exception as e:
            print("Error sending report:", e)

# ---------------------------
# Background worker for submissions
# ---------------------------
def start_processing(submission_id, file_path, options):
    t = threading.Thread(target=mock_process_file, args=(submission_id, file_path, options), daemon=True)
    t.start()

# ---------------------------
# Telegram helper: send messages
# ---------------------------
def send_text(chat_id, text, reply_markup=None):
    try:
        bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
    except Exception as e:
        print("send_text error:", e)

def require_json(f):
    @wraps(f)
    def inner(*args, **kwargs):
        if not request.is_json:
            return jsonify({"error":"expected application/json"}), 400
        return f(*args, **kwargs)
    return inner

# ---------------------------
# Flask routes: Webhook for Telegram
# ---------------------------
  
  
@app.route(f"/webhook/{TELEGRAM_BOT_TOKEN}", methods=["POST"])
def telegram_webhook():
    update = Update.de_json(request.get_json(force=True), bot)
    # handle commands and messages quickly; offload heavy work
    if update.message:
        user_id = update.message.from_user.id
        text = update.message.text or ""
        # Commands
        if text.startswith("/start"):
            send_text(user_id, "👋 Welcome to TurnitQ!\nUpload your document to check its originality instantly.\nUse /check to begin.")
            return "hello", 200
        if text.startswith("/id"):
            u = user_get(user_id)
            reply = f"👤 Your Account Info:\nUser ID: {user_id}\nPlan: {u['plan']}\nDaily Total Checks: {u['daily_limit']} - {u['used_today']}\nSubscription ends: {u['expiry_date'] or 'N/A'}"
            send_text(user_id, reply)
            return "hello", 200
        if text.startswith("/upgrade"):
            # Check capacity before showing Paystack link
            plan = "Premium"
            plan_checks = 5
            galloc = global_alloc()
            gmax = int(meta_get("global_max", "50"))
            if galloc + plan_checks > gmax:
                send_text(user_id, "Sorry, that plan is full right now. Please try a smaller plan or check back later.")
                return "", 200
            # Reserve slot for 10 minutes
            now = now_ts()
            expires = now + 10*60
            cur = db.cursor()
            cur.execute("INSERT INTO reservations(user_id, plan, created_at, expires_at, reference) VALUES(?,?,?,?,?)",
                        (user_id, plan, now, expires, "tempref_"+str(now)))
            db.commit()
            # increment global allocation tentatively
            global_alloc_add(plan_checks)
            # generate a fake Paystack link (replace with real link creation)
            pay_link = f"https://paystack.com/pay/fakepay?ref=tempref_{now}"
            markup = InlineKeyboardMarkup([[InlineKeyboardButton("Pay (Sandbox)", url=pay_link)]])
            send_text(user_id, f"Your slot is reserved for 10 minutes. Click the button to pay for {plan}.", reply_markup=markup)
            return "", 200
        if text.startswith("/cancel"):
            # Cancel user's running submission (simple implementation: mark last submission cancelled)
            cur = db.cursor()
            cur.execute("SELECT id FROM submissions WHERE user_id=? AND status='processing' ORDER BY created_at DESC LIMIT 1", (user_id,))
            row = cur.fetchone()
            if row:
                cur.execute("UPDATE submissions SET status=? WHERE id=?", ("cancelled", row["id"]))
                db.commit()
                send_text(user_id, "❌ Your check has been cancelled.")
            else:
                send_text(user_id, "You have no running checks.")
            return "", 200

        # Otherwise, handle file uploads
        if update.message.document:
            doc = update.message.document
            filename = doc.file_name or f"file_{now_ts()}"
            if not allowed_file(filename):
                send_text(user_id, "⚠️ Only .pdf and .docx files are allowed.")
                return "", 200

            u = user_get(user_id)
            # cooldown check
            last = u["last_submission"] or 0
            if now_ts() - last < 60:
                send_text(user_id, "⏳ Please wait 1 minute before submitting another document.")
                return "", 200

            # daily limit
            if u["used_today"] >= u["daily_limit"]:
                send_text(user_id, "⚠️ You’ve reached your daily limit. Subscribe to continue using TurnitQ.")
                return "", 200

            # accept file: download it
            file_obj = bot.get_file(doc.file_id)
            local_path = str(TEMP_DIR / f"{user_id}_{int(time.time())}_{filename}")
            try:
                file_obj.download(custom_path=local_path)
            except Exception as e:
                send_text(user_id, "Failed to download file. Try again.")
                print("download error", e)
                return "", 200

            # create submission record
            created = now_ts()
            cur = db.cursor()
            cur.execute("INSERT INTO submissions(user_id, filename, status, created_at) VALUES(?,?,?,?)",
                        (user_id, filename, "queued", created))
            sub_id = cur.lastrowid
            db.commit()

            # update user last_submission and usage
            cur.execute("UPDATE users SET last_submission=?, used_today=used_today+1 WHERE user_id=?", (created, user_id))
            db.commit()

            send_text(user_id, "✅ File received. Checking with TurnitQ — please wait a few seconds…")
            # kick off background processing (mock)
            start_processing(sub_id, local_path, options={})
            return "", 200

    return "", 200
app.route("/")
def greet():
    return(f"hello")

# ---------------------------
# Paystack webhook (test)
# ---------------------------
@app.route("/paystack/webhook", methods=["POST"])
def paystack_webhook():
    data = request.get_json(force=True)
    # In production, verify signature header and call Paystack verify endpoint
    # Here we simulate verification with reference passed
    # Example payload (simulate): {"event":"charge.success","data":{"reference":"tempref_...","amount":800000}}
    if not data:
        return "", 400
    ev = data.get("event")
    payload = data.get("data", {})
    ref = payload.get("reference")
    # find reservation
    cur = db.cursor()
    row = cur.execute("SELECT * FROM reservations WHERE reference=? OR reference LIKE ?", (ref, f"%{ref}%")).fetchone()
    if not row:
        return jsonify({"status":"ok","note":"reservation not found"}), 200

    user_id = row["user_id"]
    plan = row["plan"]
    # for demo set plan attributes
    plan_map = {"Premium": (5, 28), "Pro": (30, 28), "Elite": (100, 28)}
    checks, days = plan_map.get(plan, (5, 28))
    # activate plan for user
    set_user_plan(user_id, plan, checks, days=days)
    # remove reservation
    cur.execute("DELETE FROM reservations WHERE id=?", (row["id"],))
    db.commit()
    # send confirmation
    send_text(user_id, f"✅ You’re now on {plan}!\nActive until { (datetime.datetime.utcnow() + datetime.timedelta(days=days)).date() }\nYou have {checks} checks per day.")
    return jsonify({"status":"success"}), 200

# ---------------------------
# Admin / debug endpoints
# ---------------------------
@app.route("/admin/stats", methods=["GET"])
def admin_stats():
    cur = db.cursor()
    total_users = cur.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
    alloc = global_alloc()
    return jsonify({"total_users": total_users, "global_alloc": alloc})

# ---------------------------
# Scheduler jobs
# ---------------------------
scheduler = BackgroundScheduler()

def reset_daily():
    cur = db.cursor()
    cur.execute("UPDATE users SET used_today=0")
    db.commit()
    # reset global alloc as well
    meta_set("global_alloc", "0")
    print("Daily reset performed at", datetime.datetime.utcnow())

scheduler.add_job(reset_daily, 'cron', hour=0)  # runs at midnight UTC
scheduler.start()

# ---------------------------
# helper to set webhook (manual step)
# ---------------------------
def set_webhook():
    url = f"{WEBHOOK_BASE_URL}/webhook/{TELEGRAM_BOT_TOKEN}"
    r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook", params={"url": url})
    print("setWebhook response:", r.text)

if __name__ == "__main__":
    # for local testing, you can call set_webhook() once (if WEBHOOK_BASE_URL is reachable).
    # set_webhook()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
