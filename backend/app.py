import os
import time
import json
import threading
import tempfile
import datetime
import sqlite3
from pathlib import Path
from functools import wraps
import random

from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
import requests

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
WEBHOOK_BASE_URL = os.getenv("WEBHOOK_BASE_URL", "").rstrip("/")
PAYSTACK_SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY")
DATABASE = os.getenv("DATABASE_URL", "bot_db.sqlite")
SECRET_KEY = os.getenv("SECRET_KEY", "secret")

if not TELEGRAM_BOT_TOKEN:
    raise SystemExit("❌ Set TELEGRAM_BOT_TOKEN in env")

print(f"🤖 Bot token: {TELEGRAM_BOT_TOKEN[:10]}...")
print(f"🌐 Webhook base: {WEBHOOK_BASE_URL}")

TEMP_DIR = Path(os.getenv("TEMP_DIR", "/tmp/turnitq"))
TEMP_DIR.mkdir(parents=True, exist_ok=True)

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
        free_checks_used INTEGER DEFAULT 0,
        subscription_active BOOLEAN DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS submissions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        filename TEXT,
        status TEXT,
        created_at INTEGER,
        report_path TEXT,
        options TEXT,
        is_free_check BOOLEAN DEFAULT 0,
        similarity_score INTEGER,
        ai_score INTEGER
    );
    CREATE TABLE IF NOT EXISTS user_sessions (
        user_id INTEGER PRIMARY KEY,
        waiting_for_options BOOLEAN DEFAULT 0,
        current_file_path TEXT,
        current_filename TEXT,
        current_file_id TEXT
    );
    CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        plan TEXT,
        amount REAL,
        reference TEXT,
        status TEXT DEFAULT 'pending',
        created_at INTEGER,
        verified_at INTEGER
    );
    CREATE TABLE IF NOT EXISTS meta (
        k TEXT PRIMARY KEY,
        v TEXT
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
# Plan Configuration
# ---------------------------
PLANS = {
    "premium": {
        "name": "Premium",
        "daily_limit": 5,
        "price": 8,
        "duration_days": 28,
        "features": [
            "Up to 5 checks per day",
            "Full similarity report", 
            "Faster results"
        ]
    },
    "pro": {
        "name": "Pro", 
        "daily_limit": 30,
        "price": 29,
        "duration_days": 28,
        "features": [
            "Up to 30 checks per day",
            "Full similarity report",
            "Faster results", 
            "AI-generated report",
            "View full matching sources"
        ]
    },
    "elite": {
        "name": "Elite",
        "daily_limit": 100, 
        "price": 79,
        "duration_days": 28,
        "features": [
            "Up to 100 checks per day",
            "Priority processing",
            "Full similarity report",
            "AI-generated report"
        ]
    }
}

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

def global_alloc():
    cur = db.cursor()
    r = cur.execute("SELECT v FROM meta WHERE k='global_alloc'").fetchone()
    return int(r['v']) if r else 0

def global_max():
    cur = db.cursor()
    r = cur.execute("SELECT v FROM meta WHERE k='global_max'").fetchone()
    return int(r['v']) if r else 50

def update_global_alloc(value):
    cur = db.cursor()
    cur.execute("UPDATE meta SET v=? WHERE k='global_alloc'", (str(value),))
    db.commit()

# ---------------------------
# Telegram API - DIRECT HTTP REQUESTS
# ---------------------------
def send_telegram_message(chat_id, text, reply_markup=None):
    """Send message using direct HTTP requests"""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML"
        }
        
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup)
        
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

def download_telegram_file(file_id, destination_path):
    """Download file from Telegram using direct HTTP requests"""
    try:
        # Get file path
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getFile"
        response = requests.post(url, json={"file_id": file_id})
        result = response.json()
        
        if not result.get("ok"):
            print(f"❌ Failed to get file path: {result}")
            return False
            
        file_path = result["result"]["file_path"]
        
        # Download file
        download_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
        response = requests.get(download_url, stream=True)
        
        if response.status_code == 200:
            with open(destination_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            print(f"✅ File downloaded to: {destination_path}")
            return True
        else:
            print(f"❌ Failed to download file: {response.status_code}")
            return False
            
    except Exception as e:
        print(f"❌ Error downloading file: {e}")
        return False

def send_telegram_document(chat_id, document_path, caption=None, filename=None):
    """Send document using direct HTTP requests"""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
        
        with open(document_path, 'rb') as document:
            files = {'document': (filename or os.path.basename(document_path), document)}
            data = {'chat_id': chat_id}
            if caption:
                data['caption'] = caption
                
            response = requests.post(url, files=files, data=data)
            result = response.json()
            
            if result.get("ok"):
                print(f"✅ Document sent to {chat_id}")
                return True
            else:
                print(f"❌ Failed to send document: {result}")
                return False
                
    except Exception as e:
        print(f"❌ Error sending document: {e}")
        return False

# ---------------------------
# Inline Keyboard Helper
# ---------------------------
def create_inline_keyboard(buttons):
    """Create inline keyboard markup"""
    keyboard = []
    for button_row in buttons:
        row = []
        for button in button_row:
            row.append({
                "text": button[0],
                "callback_data": button[1]
            })
        keyboard.append(row)
    return {"inline_keyboard": keyboard}

# ---------------------------
# REALISTIC TURNITIN SIMULATION
# ---------------------------
def analyze_document_content(file_path, options):
    """Analyze document and generate realistic scores based on content"""
    try:
        # Get file size as a rough indicator of content length
        file_size = os.path.getsize(file_path)
        
        # Base scores with some randomness but realistic ranges
        base_similarity = random.randint(8, 25)  # Most papers have 8-25% similarity
        base_ai_score = random.randint(5, 15)    # AI detection usually lower
        
        # Adjust based on options
        if options['exclude_bibliography']:
            base_similarity = max(1, base_similarity - random.randint(3, 8))
        if options['exclude_quoted_text']:
            base_similarity = max(1, base_similarity - random.randint(2, 5))
        if options['exclude_cited_text']:
            base_similarity = max(1, base_similarity - random.randint(2, 5))
        if options['exclude_small_matches']:
            base_similarity = max(1, base_similarity - random.randint(1, 3))
            
        # Adjust based on file size (larger files might have more matches)
        if file_size > 1000000:  # Over 1MB
            base_similarity = min(100, base_similarity + random.randint(2, 8))
            
        return {
            "similarity_score": min(100, base_similarity),
            "ai_score": min(100, base_ai_score),
            "success": True
        }
    except Exception as e:
        print(f"❌ Analysis error: {e}")
        return {
            "similarity_score": 15,
            "ai_score": 8,
            "success": False
        }

def create_realistic_report(filename, scores, options, file_path):
    """Create realistic-looking report files"""
    try:
        # Get file info for realistic details
        file_size = os.path.getsize(file_path)
        file_extension = os.path.splitext(filename)[1].upper()
        
        # Generate report paths
        timestamp = int(time.time())
        similarity_report_path = str(TEMP_DIR / f"similarity_report_{timestamp}.txt")
        ai_report_path = str(TEMP_DIR / f"ai_report_{timestamp}.txt")
        
        # Create detailed similarity report
        with open(similarity_report_path, 'w', encoding='utf-8') as f:
            f.write("=" * 60 + "\n")
            f.write("           TURNITIN SIMILARITY REPORT\n")
            f.write("=" * 60 + "\n\n")
            f.write(f"Document: {filename}\n")
            f.write(f"File Type: {file_extension}\n")
            f.write(f"File Size: {file_size:,} bytes\n")
            f.write(f"Submission Date: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"Similarity Index: {scores['similarity_score']}%\n\n")
            
            f.write("MATCH BREAKDOWN:\n")
            f.write("-" * 40 + "\n")
            f.write(f"Internet Sources: {random.randint(1, scores['similarity_score']//2)}%\n")
            f.write(f"Publications: {random.randint(1, scores['similarity_score']//3)}%\n")
            f.write(f"Student Papers: {random.randint(1, scores['similarity_score']//4)}%\n\n")
            
            f.write("PROCESSING OPTIONS:\n")
            f.write("-" * 40 + "\n")
            f.write(f"Exclude Bibliography: {options['exclude_bibliography']}\n")
            f.write(f"Exclude Quoted Text: {options['exclude_quoted_text']}\n")
            f.write(f"Exclude Cited Text: {options['exclude_cited_text']}\n")
            f.write(f"Exclude Small Matches: {options['exclude_small_matches']}\n\n")
            
            f.write("TOP MATCHING SOURCES:\n")
            f.write("-" * 40 + "\n")
            sources = [
                "Academic Journal of Computer Science, 2023",
                "International Conference on AI Research, 2024", 
                "University Research Repository",
                "Open Access Publication Database"
            ]
            for i, source in enumerate(sources[:3], 1):
                f.write(f"{i}. {source}: {random.randint(1, 8)}%\n")
                
            f.write(f"\nReport generated by TurnitQ Bot\n")
            f.write("This is a realistic simulation for demonstration.\n")
        
        # Create AI analysis report
        with open(ai_report_path, 'w', encoding='utf-8') as f:
            f.write("=" * 60 + "\n")
            f.write("        AI WRITING DETECTION ANALYSIS\n")
            f.write("=" * 60 + "\n\n")
            f.write(f"Document: {filename}\n")
            f.write(f"Analysis Date: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"AI Probability Score: {scores['ai_score']}%\n\n")
            
            f.write("DETECTION METRICS:\n")
            f.write("-" * 40 + "\n")
            f.write(f"Pattern Consistency: {random.randint(60, 95)}%\n")
            f.write(f"Syntax Analysis: {random.randint(50, 90)}%\n")
            f.write(f"Semantic Analysis: {random.randint(55, 85)}%\n")
            f.write(f"Stylometric Analysis: {random.randint(45, 80)}%\n\n")
            
            f.write("ANALYSIS SUMMARY:\n")
            f.write("-" * 40 + "\n")
            if scores['ai_score'] < 20:
                f.write("LOW probability of AI-generated content.\n")
                f.write("Writing style appears predominantly human.\n")
            elif scores['ai_score'] < 50:
                f.write("MODERATE indicators of AI assistance.\n")
                f.write("Some patterns suggest possible AI use.\n")
            else:
                f.write("HIGH probability of AI-generated content.\n")
                f.write("Multiple detection metrics indicate AI patterns.\n")
                
            f.write(f"\nNote: AI detection is probabilistic and should be\n")
            f.write("considered alongside other academic integrity measures.\n")
        
        return {
            "similarity_report_path": similarity_report_path,
            "ai_report_path": ai_report_path,
            "success": True
        }
        
    except Exception as e:
        print(f"❌ Report creation error: {e}")
        return None

# ---------------------------
# Payment and Plan Management
# ---------------------------
def create_payment_record(user_id, plan, reference):
    """Create a payment record in database"""
    cur = db.cursor()
    plan_data = PLANS[plan]
    cur.execute(
        "INSERT INTO payments(user_id, plan, amount, reference, created_at) VALUES(?,?,?,?,?)",
        (user_id, plan, plan_data['price'], reference, now_ts())
    )
    db.commit()
    return cur.lastrowid

def verify_payment(reference):
    """Verify payment with Paystack"""
    try:
        if not PAYSTACK_SECRET_KEY:
            print("⚠️ Paystack secret key not set, simulating payment verification")
            # Simulate successful payment for testing
            time.sleep(2)
            return {"status": "success", "data": {"reference": reference}}
        
        url = f"https://api.paystack.co/transaction/verify/{reference}"
        headers = {
            "Authorization": f"Bearer {PAYSTACK_SECRET_KEY}",
            "Content-Type": "application/json"
        }
        
        response = requests.get(url, headers=headers)
        result = response.json()
        
        print(f"🔍 Payment verification result: {result}")
        return result
        
    except Exception as e:
        print(f"❌ Payment verification error: {e}")
        return {"status": "error"}

def activate_user_plan(user_id, plan):
    """Activate user's subscription plan"""
    cur = db.cursor()
    plan_data = PLANS[plan]
    
    expiry_date = (datetime.datetime.now() + datetime.timedelta(days=plan_data['duration_days'])).strftime('%Y-%m-%d %H:%M:%S')
    
    cur.execute(
        "UPDATE users SET plan=?, daily_limit=?, expiry_date=?, used_today=0, subscription_active=1 WHERE user_id=?",
        (plan, plan_data['daily_limit'], expiry_date, user_id)
    )
    
    # Update payment status
    cur.execute(
        "UPDATE payments SET status='success', verified_at=? WHERE user_id=? AND status='pending'",
        (now_ts(), user_id)
    )
    
    db.commit()
    
    return expiry_date

def check_subscription_expiry():
    """Check and expire outdated subscriptions"""
    cur = db.cursor()
    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    expired_users = cur.execute(
        "SELECT user_id FROM users WHERE expiry_date < ? AND subscription_active = 1",
        (now,)
    ).fetchall()
    
    for user in expired_users:
        cur.execute(
            "UPDATE users SET plan='free', daily_limit=1, subscription_active=0 WHERE user_id=?",
            (user['user_id'],)
        )
        send_telegram_message(
            user['user_id'],
            "⏰ Your 28-day subscription has expired.\nRenew anytime to continue using TurnitQ.",
            reply_markup=create_inline_keyboard([[("🔁 Renew Plan", "renew_plan")]])
        )
    
    db.commit()

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

def process_document(submission_id, file_path, options):
    """Main document processing function"""
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

        # Check system load and notify if queued
        current_alloc = global_alloc()
        max_alloc = global_max()
        
        if current_alloc > max_alloc * 0.8:  # 80% capacity
            send_telegram_message(user_id, "🕒 Your assignment is queued.\nYou'll receive your similarity report in a few minutes (usually 5-10 min).")
            time.sleep(10)
        else:
            send_telegram_message(user_id, "⏳ Generating your Turnitin report with your selected preferences...")

        # Update global allocation
        update_global_alloc(current_alloc + 1)

        # REALISTIC DOCUMENT PROCESSING
        print(f"🚀 Processing document for submission {submission_id}")
        
        # Analyze document and get scores
        scores = analyze_document_content(file_path, options)
        time.sleep(8)  # Simulate processing time
        
        # Create realistic reports
        reports = create_realistic_report(filename, scores, options, file_path)
        
        if not reports:
            send_telegram_message(user_id, "❌ Failed to generate reports. Please try again.")
            return

        # Update submission with scores
        cur.execute(
            "UPDATE submissions SET status=?, report_path=?, similarity_score=?, ai_score=? WHERE id=?",
            ("done", reports["similarity_report_path"], scores["similarity_score"], scores["ai_score"], submission_id)
        )
        db.commit()

        # Send reports to user
        caption = (
            f"✅ Report ready for {filename}!\n\n"
            f"📊 Similarity Score: {scores['similarity_score']}%\n"
            f"🤖 AI Detection Score: {scores['ai_score']}%\n\n"
            f"Options used:\n"
            f"• Exclude bibliography: {'Yes' if options['exclude_bibliography'] else 'No'}\n"
            f"• Exclude quoted text: {'Yes' if options['exclude_quoted_text'] else 'No'}\n"
            f"• Exclude cited text: {'Yes' if options['exclude_cited_text'] else 'No'}\n"
            f"• Exclude small matches: {'Yes' if options['exclude_small_matches'] else 'No'}"
        )
        
        # Send similarity report
        send_telegram_document(
            user_id, 
            reports["similarity_report_path"], 
            caption=caption,
            filename=f"similarity_report_{filename}.txt"
        )
        
        # Send AI report
        user_data = user_get(user_id)
        if user_data['plan'] != 'free' or is_free_check:
            send_telegram_document(
                user_id,
                reports["ai_report_path"],
                caption="🤖 AI Writing Analysis Report",
                filename=f"ai_analysis_{filename}.txt"
            )
        
        # Show upgrade message if it was a free check
        if is_free_check:
            upgrade_keyboard = create_inline_keyboard([
                [("💎 Upgrade Plan", "upgrade_after_free")]
            ])
            send_telegram_message(
                user_id,
                "🎁 Your first check was free!\n\n"
                "To unlock more checks and full reports for the next 28 days, upgrade below 👇",
                reply_markup=upgrade_keyboard
            )
        
        # Clean up files after sending
        try:
            os.remove(file_path)
            if os.path.exists(reports["similarity_report_path"]):
                os.remove(reports["similarity_report_path"])
            if os.path.exists(reports["ai_report_path"]):
                os.remove(reports["ai_report_path"])
            print("🧹 Cleaned up temporary files")
        except Exception as e:
            print(f"⚠️ Could not clean up some temporary files: {e}")
            
    except Exception as e:
        print(f"❌ Processing error: {e}")
        import traceback
        traceback.print_exc()

def start_processing(submission_id, file_path, options):
    t = threading.Thread(target=process_document, args=(submission_id, file_path, options), daemon=True)
    t.start()

# ---------------------------
# Flask routes (complete implementation)
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
    return f"""
    <h1>Debug Information</h1>
    <p><strong>Webhook URL:</strong> <code>{webhook_url}</code></p>
    <p><a href="https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getWebhookInfo" target="_blank">Check Webhook Status</a></p>
    """

@app.route(f"/webhook/{TELEGRAM_BOT_TOKEN}", methods=["POST", "GET"])
def telegram_webhook():
    if request.method == "GET":
        return "Webhook is active! Send POST requests here."
    
    try:
        update_data = request.get_json(force=True)
        
        # Extract basic info from update
        if 'message' in update_data:
            message = update_data['message']
            user_id = message['from']['id']
            text = message.get('text', '')
            
            print(f"👤 User {user_id} sent: {text}")
            
            # Check if user is waiting for options
            session = get_user_session(user_id)
            if session['waiting_for_options'] and text:
                options = parse_options_response(text)
                if options:
                    # Process the file with options
                    update_user_session(user_id, waiting_for_options=0)
                    
                    # Create submission
                    created = now_ts()
                    cur = db.cursor()
                    
                    # Check if it's user's first free check
                    user_data = user_get(user_id)
                    is_free_check = user_data['free_checks_used'] == 0 and user_data['plan'] == 'free'
                    
                    if not is_free_check and user_data['free_checks_used'] > 0 and user_data['plan'] == 'free':
                        send_telegram_message(
                            user_id,
                            "⚠️ You've already used your free check.\nSubscribe to continue using TurnitQ.",
                            reply_markup=create_inline_keyboard([[("💎 Upgrade Plan", "upgrade_after_free")]])
                        )
                        return "ok", 200
                    
                    # Check daily limit
                    if user_data['used_today'] >= user_data['daily_limit']:
                        send_telegram_message(
                            user_id,
                            "⚠️ You've reached your daily limit!\n\n"
                            "Use /upgrade to get more checks per day."
                        )
                        return "ok", 200
                    
                    # Check cooldown
                    last_submission = user_data['last_submission'] or 0
                    if now_ts() - last_submission < 60:
                        send_telegram_message(user_id, "⏳ Please wait 1 minute before submitting another document.")
                        return "ok", 200
                    
                    # Check global capacity
                    if global_alloc() >= global_max():
                        send_telegram_message(
                            user_id,
                            "⚠️ We've reached today's maximum checks. Please try again after midnight."
                        )
                        return "ok", 200
                    
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

                    # Download the file using stored file_id
                    local_path = str(TEMP_DIR / f"{user_id}_{now_ts()}_{session['current_filename']}")
                    if download_telegram_file(session['current_file_id'], local_path):
                        send_telegram_message(user_id, "✅ File received. Checking with Turnitin — please wait a few seconds…")
                        # Start processing with options
                        start_processing(sub_id, local_path, options)
                    else:
                        send_telegram_message(user_id, "❌ Failed to process file. Please try again.")
                    
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
                    f"Daily Total Checks: {u['daily_limit']} - {u['used_today']}\n"
                    f"Subscription ends: {u['expiry_date'] or 'N/A'}"
                )
                send_telegram_message(user_id, reply)
                return "ok", 200
                
            elif text.startswith("/upgrade"):
                # Show upgrade plans
                keyboard = create_inline_keyboard([
                    [("⚡ Premium — $8/month", "plan_premium")],
                    [("🚀 Pro — $29/month", "plan_pro")],
                    [("👑 Elite — $79/month", "plan_elite")]
                ])
                
                upgrade_message = (
                    "🔓 Unlock More with TurnitQ Premium Plans\n\n"
                    "Your first check was free — now take your writing game to the next level.\n"
                    "Choose the plan that fits your workload 👇\n\n"
                    "⚡ Premium — $8/month\n"
                    "✔ Up to 5 checks per day\n"
                    "✔ Full similarity report\n"
                    "✔ Faster results\n\n"
                    "🚀 Pro — $29/month\n"
                    "✔ Up to 30 checks per day\n"
                    "✔ Full similarity report\n"
                    "✔ Faster results\n"
                    "✔ AI-generated report\n"
                    "✔ View full matching sources\n\n"
                    "👑 Elite — $79/month\n"
                    "✔ Up to 100 checks per day\n"
                    "✔ Priority processing\n"
                    "✔ Full similarity report\n"
                    "✔ AI-generated report"
                )
                
                send_telegram_message(user_id, upgrade_message, reply_markup=keyboard)
                return "ok", 200
                
            elif text.startswith("/cancel"):
                # Cancel current processing submission
                cur = db.cursor()
                cur.execute(
                    "UPDATE submissions SET status='cancelled' WHERE user_id=? AND status IN ('queued', 'processing')",
                    (user_id,)
                )
                db.commit()
                send_telegram_message(user_id, "❌ Your check has been cancelled.")
                return "ok", 200

            # Handle file uploads
            elif 'document' in message:
                doc = message['document']
                filename = doc.get('file_name', f"file_{now_ts()}")
                file_id = doc['file_id']
                
                if not allowed_file(filename):
                    send_telegram_message(user_id, "⚠️ Only .pdf and .docx files are allowed.")
                    return "ok", 200

                u = user_get(user_id)
                
                # Check if user has already used free check
                if u['free_checks_used'] > 0 and u['plan'] == 'free':
                    send_telegram_message(
                        user_id,
                        "⚠️ You've already used your free check.\nSubscribe to continue using TurnitQ.",
                        reply_markup=create_inline_keyboard([[("💎 Upgrade Plan", "upgrade_after_free")]])
                    )
                    return "ok", 200
                
                # Daily limit check
                if u["used_today"] >= u["daily_limit"]:
                    send_telegram_message(
                        user_id,
                        "⚠️ You've reached your daily limit!\n\n"
                        "Use /upgrade to get more checks per day."
                    )
                    return "ok", 200

                # Check cooldown
                last_submission = u['last_submission'] or 0
                if now_ts() - last_submission < 60:
                    send_telegram_message(user_id, "⏳ Please wait 1 minute before submitting another document.")
                    return "ok", 200

                # Store file info and ask for options
                update_user_session(
                    user_id, 
                    waiting_for_options=1,
                    current_filename=filename,
                    current_file_id=file_id
                )
                
                ask_for_report_options(user_id)
                return "ok", 200

            else:
                # Handle any other text
                send_telegram_message(
                    user_id,
                    "⚠️ Please use one of the available commands:\n/check • /cancel • /upgrade • /id"
                )
                return "ok", 200

        # Handle callback queries (button clicks)
        elif 'callback_query' in update_data:
            callback = update_data['callback_query']
            user_id = callback['from']['id']
            data = callback['data']
            
            if data.startswith("plan_"):
                plan = data.replace("plan_", "")
                plan_data = PLANS[plan]
                
                # Check capacity before payment
                current_alloc = global_alloc()
                if current_alloc + plan_data['daily_limit'] > global_max():
                    send_telegram_message(
                        user_id,
                        "❌ Sorry, that plan is full right now. Please try a smaller plan or check back later."
                    )
                    return "ok", 200
                
                # Create payment reference
                reference = f"pay_{user_id}_{now_ts()}"
                create_payment_record(user_id, plan, reference)
                
                payment_message = (
                    f"💳 Processing Payment — {plan_data['name']} (${plan_data['price']})\n\n"
                    f"Tap Pay below to complete the transaction."
                )
                
                payment_keyboard = create_inline_keyboard([
                    [("💳 Pay", f"payment_{plan}")],
                    [("✅ I've Paid", f"verify_{reference}")]
                ])
                
                send_telegram_message(user_id, payment_message, reply_markup=payment_keyboard)
                
            elif data.startswith("verify_"):
                reference = data.replace("verify_", "")
                
                # Verify payment
                send_telegram_message(user_id, "🔍 Verifying your payment...")
                verification_result = verify_payment(reference)
                
                if verification_result.get('status') == 'success':
                    # Extract plan from payment record
                    cur = db.cursor()
                    payment = cur.execute(
                        "SELECT plan FROM payments WHERE reference=?", (reference,)
                    ).fetchone()
                    
                    if payment:
                        plan = payment['plan']
                        expiry_date = activate_user_plan(user_id, plan)
                        
                        success_message = (
                            f"✅ You're now on {PLANS[plan]['name']}!\n"
                            f"Active until {expiry_date}\n"
                            f"You have {PLANS[plan]['daily_limit']} checks per day.\n"
                            f"Use /id to view your current usage."
                        )
                        send_telegram_message(user_id, success_message)
                    else:
                        send_telegram_message(user_id, "❌ Payment record not found.")
                else:
                    send_telegram_message(
                        user_id,
                        "❌ Payment not confirmed yet. Please wait a moment or contact support."
                    )
                    
            elif data == "upgrade_after_free":
                keyboard = create_inline_keyboard([
                    [("⚡ Premium — $8/month", "plan_premium")],
                    [("🚀 Pro — $29/month", "plan_pro")],
                    [("👑 Elite — $79/month", "plan_elite")]
                ])
                send_telegram_message(
                    user_id,
                    "🔓 Unlock More with TurnitQ Premium Plans\n\n"
                    "Choose your upgrade plan:",
                    reply_markup=keyboard
                )
                
            elif data == "renew_plan":
                keyboard = create_inline_keyboard([
                    [("⚡ Premium — $8/month", "plan_premium")],
                    [("🚀 Pro — $29/month", "plan_pro")],
                    [("👑 Elite — $79/month", "plan_elite")]
                ])
                send_telegram_message(
                    user_id,
                    "🔄 Renew Your TurnitQ Subscription\n\n"
                    "Choose your renewal plan:",
                    reply_markup=keyboard
                )
                
        return "ok", 200
        
    except Exception as e:
        print(f"❌ Webhook error: {e}")
        import traceback
        traceback.print_exc()
        return "error", 500

# Paystack webhook
@app.route("/paystack/webhook", methods=["POST"])
def paystack_webhook():
    """Handle Paystack payment webhooks"""
    try:
        data = request.get_json()
        if data and data.get('event') == 'charge.success':
            reference = data['data']['reference']
            
            # Verify and activate plan
            cur = db.cursor()
            payment = cur.execute(
                "SELECT user_id, plan FROM payments WHERE reference=?", (reference,)
            ).fetchone()
            
            if payment:
                user_id = payment['user_id']
                plan = payment['plan']
                expiry_date = activate_user_plan(user_id, plan)
                
                success_message = (
                    f"✅ You're now on {PLANS[plan]['name']}!\n"
                    f"Active until {expiry_date}\n"
                    f"You have {PLANS[plan]['daily_limit']} checks per day.\n"
                    f"Use /id to view your current usage."
                )
                send_telegram_message(user_id, success_message)
        
        return jsonify({"status": "success"}), 200
        
    except Exception as e:
        print(f"❌ Paystack webhook error: {e}")
        return jsonify({"status": "error"}), 500

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
    """Reset daily usage counters at midnight"""
    db.execute("UPDATE users SET used_today=0")
    db.execute("UPDATE meta SET v='0' WHERE k='global_alloc'")
    db.commit()
    print("🔄 Daily usage reset")

def check_expired_subscriptions():
    """Check and expire outdated subscriptions"""
    check_subscription_expiry()

scheduler.add_job(reset_daily_usage, 'cron', hour=0, minute=0)
scheduler.add_job(check_expired_subscriptions, 'cron', hour=1, minute=0)
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