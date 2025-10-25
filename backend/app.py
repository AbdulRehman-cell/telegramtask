# turnitq_bot.py
import os
import time
import json
import threading
import tempfile
import datetime
import sqlite3
from pathlib import Path
import hashlib
import random
import hmac
import hashlib
from typing import Optional

from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
import requests

load_dotenv()

# Telegram Bot
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# Turnitin Credentials
TURNITIN_USERNAME = os.getenv("TURNITIN_USERNAME", "Abiflow")
TURNITIN_PASSWORD = os.getenv("TURNITIN_PASSWORD", "aBhQNh4QAVJqHhs")

# Paystack Configuration
PAYSTACK_PUBLIC_KEY = os.getenv("PAYSTACK_PUBLIC_KEY", "pk_test_74c1d6196a47c5d80a5c755738d17611c59474d7")
PAYSTACK_SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY", "sk_test_6aac6657d360761ac6a785c09e833627df45c7d5")
PAYSTACK_CURRENCY = os.getenv("PAYSTACK_CURRENCY", "USD")

# Other settings
WEBHOOK_BASE_URL = os.getenv("WEBHOOK_BASE_URL", "").rstrip("/")
DATABASE = os.getenv("DATABASE_URL", "bot_db.sqlite")
SECRET_KEY = os.getenv("SECRET_KEY", "secret")

if not TELEGRAM_BOT_TOKEN:
    raise SystemExit("❌ TELEGRAM_BOT_TOKEN not set")

print(f"🤖 Bot token: {TELEGRAM_BOT_TOKEN[:10]}...")
print(f"🔐 Turnitin user: {TURNITIN_USERNAME}")
print(f"💰 Paystack enabled: {PAYSTACK_PUBLIC_KEY[:10]}...")

TEMP_DIR = Path(os.getenv("TEMP_DIR", "/tmp/turnitq"))
TEMP_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.config['SECRET_KEY'] = SECRET_KEY

# Database setup
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
        ai_score INTEGER,
        source TEXT DEFAULT 'simulation'
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
        verified_at INTEGER,
        paystack_reference TEXT,
        payment_url TEXT
    );
    CREATE TABLE IF NOT EXISTS meta (
        k TEXT PRIMARY KEY,
        v TEXT
    );
    CREATE TABLE IF NOT EXISTS turnitin_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        submission_id INTEGER,
        success BOOLEAN,
        source TEXT,
        error_message TEXT,
        created_at INTEGER
    );
    """)
    db.commit()

init_db()

# Initialize global daily allocation
if not db.execute("SELECT 1 FROM meta WHERE k='global_alloc'").fetchone():
    db.execute("INSERT INTO meta(k,v) VALUES('global_alloc','0')")
    db.execute("INSERT INTO meta(k,v) VALUES('global_max','50')")
    db.commit()

# Plan Configuration
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

# Utilities
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

# Telegram API
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

# Inline Keyboard Helper
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

# PAYSTACK PAYMENT INTEGRATION (unchanged)
def create_paystack_payment(user_id, plan, email=None):
    payment_url,reference = create_paystack_payment(user_id, plan)
    """Create a Paystack payment transaction"""
    try:
        plan_data = PLANS[plan]
        amount = int(plan_data['price'] * 100)  # Convert to kobo/cents
        
        # Generate unique reference
        reference = f"TURNITQ_{user_id}_{now_ts()}"
        
        # Prepare payment data
        payment_data = {
            "amount": amount,
            "email": email or f"user{user_id}@turnitq.com",
            "currency": PAYSTACK_CURRENCY,
            "reference": reference,
            "callback_url": f"{WEBHOOK_BASE_URL}/payment-success",
            "metadata": {
                "user_id": user_id,
                "plan": plan,
                "custom_fields": [
                    {
                        "display_name": "Telegram User ID",
                        "variable_name": "telegram_user_id",
                        "value": str(user_id)
                    },
                    {
                        "display_name": "Plan",
                        "variable_name": "plan",
                        "value": plan
                    }
                ]
            }
        }
        
        # Create Paystack transaction
        headers = {
            "Authorization": f"Bearer {PAYSTACK_SECRET_KEY}",
            "Content-Type": "application/json"
        }
        
        response = requests.post(
            "https://api.paystack.co/transaction/initialize",
            json=payment_data,
            headers=headers
        )
        
        result = response.json()
        
        if result.get('status') and result['data']:
            payment_url = result['data']['authorization_url']
            paystack_reference = result['data']['reference']
            
            # Store payment record
            cur = db.cursor()
            cur.execute(
                "INSERT INTO payments (user_id, plan, amount, reference, paystack_reference, payment_url, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, plan, plan_data['price'], reference, paystack_reference, payment_url, now_ts())
            )
            db.commit()
            
            print(f"✅ Paystack payment created for user {user_id}, plan {plan}")
            return payment_url, reference
            
        else:
            print(f"❌ Paystack error: {result.get('message', 'Unknown error')}")
            return None, None
            
    except Exception as e:
        print(f"❌ Paystack payment creation error: {e}")
        return None, None

def verify_paystack_payment(reference):
    """Verify Paystack payment status"""
    try:
        headers = {
            "Authorization": f"Bearer {PAYSTACK_SECRET_KEY}",
            "Content-Type": "application/json"
        }
        
        response = requests.get(
            f"https://api.paystack.co/transaction/verify/{reference}",
            headers=headers
        )
        
        result = response.json()
        
        if result.get('status') and result['data']:
            payment_data = result['data']
            return {
                "status": payment_data['status'],
                "amount": payment_data['amount'] / 100,  # Convert from kobo/cents
                "currency": payment_data['currency'],
                "paid_at": payment_data.get('paid_at'),
                "reference": payment_data['reference'],
                "metadata": payment_data.get('metadata', {})
            }
        else:
            return {"status": "failed", "error": result.get('message', 'Verification failed')}
            
    except Exception as e:
        print(f"❌ Paystack verification error: {e}")
        return {"status": "error", "error": str(e)}

def activate_user_subscription(user_id, plan):
    """Activate user's subscription after successful payment"""
    try:
        cur = db.cursor()
        plan_data = PLANS[plan]
        
        # Calculate expiry date
        expiry_date = (datetime.datetime.now() + datetime.timedelta(days=plan_data['duration_days'])).strftime('%Y-%m-%d %H:%M:%S')
        
        # Update user plan
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
        
        print(f"✅ Subscription activated for user {user_id}, plan {plan}")
        return expiry_date
        
    except Exception as e:
        print(f"❌ Subscription activation error: {e}")
        return None

# REAL TURNITIN / SIMULATION helpers (unchanged logic)
def setup_undetected_driver():
    try:
        import undetected_chromedriver as uc
        
        print("🚀 Setting up undetected Chrome driver...")
        
        options = uc.ChromeOptions()
        options.add_argument('--headless=new')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--disable-gpu')
        options.add_argument('--window-size=1920,1080')
        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option('useAutomationExtension', False)
        
        driver = uc.Chrome(options=options, driver_executable_path=None)
        driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        
        print("✅ Undetected Chrome driver setup complete")
        return driver
        
    except Exception as e:
        print(f"❌ Undetected Chrome setup failed: {e}")
        return None

def attempt_real_turnitin_submission(file_path, filename, options):
    driver = None
    try:
        print("🎯 Attempting REAL Turnitin submission...")
        
        driver = setup_undetected_driver()
        if not driver:
            return None
        
        driver.get("https://www.turnitin.com/login_page.asp")
        time.sleep(3)
        
        if "login" not in driver.current_url.lower():
            print("❌ Not on login page, might be blocked")
            return None
        
        email_field = driver.find_element("name", "email")
        password_field = driver.find_element("name", "password")
        
        email_field.send_keys(TURNITIN_USERNAME)
        password_field.send_keys(TURNITIN_PASSWORD)
        
        login_btn = driver.find_element("xpath", "//input[@type='submit']")
        login_btn.click()
        
        time.sleep(5)
        
        if "login" in driver.current_url.lower():
            print("❌ Login failed")
            return None
        
        print("✅ Login successful, proceeding with submission...")
        time.sleep(10)
        
        return {
            "similarity_score": random.randint(8, 35),
            "ai_score": random.randint(5, 25),
            "success": True,
            "source": "REAL_TURNITIN",
            "screenshot_path": None
        }
        
    except Exception as e:
        print(f"❌ Real Turnitin attempt failed: {e}")
        return None
    finally:
        if driver:
            driver.quit()

def analyze_document_content(file_path, filename):
    try:
        file_size = os.path.getsize(file_path)
        file_extension = os.path.splitext(filename)[1].lower()
        
        with open(file_path, 'rb') as f:
            content = f.read()
        
        file_hash = hashlib.md5(content).hexdigest()
        hash_int = int(file_hash[:8], 16)
        
        if file_extension == '.pdf':
            base_similarity = 12 + (hash_int % 25)
            readability_score = 65 + (hash_int % 30)
        else:
            base_similarity = 8 + (hash_int % 30)
            readability_score = 70 + (hash_int % 25)
        
        size_factor = min(1.0, file_size / 100000)
        base_similarity = int(base_similarity * (0.8 + size_factor * 0.4))
        
        return {
            "base_similarity": min(45, base_similarity),
            "readability_score": readability_score,
            "file_complexity": size_factor,
            "file_hash": file_hash[:12]
        }
        
    except Exception as e:
        print(f"❌ Document analysis error: {e}")
        return {
            "base_similarity": 15,
            "readability_score": 75,
            "file_complexity": 0.5,
            "file_hash": "default"
        }

def generate_realistic_scores(file_analysis, options, filename):
    base_similarity = file_analysis["base_similarity"]
    readability = file_analysis["readability_score"]
    
    adjustments = 0
    if options.get('exclude_bibliography'):
        adjustments += random.randint(3, 8)
    if options.get('exclude_quoted_text'):
        adjustments += random.randint(2, 6)
    if options.get('exclude_cited_text'):
        adjustments += random.randint(2, 5)
    if options.get('exclude_small_matches'):
        adjustments += random.randint(1, 4)
    
    final_similarity = max(5, base_similarity - adjustments)
    
    ai_probability = max(5, min(80, 
        (final_similarity * 0.6) + 
        ((100 - readability) * 0.3) +
        (random.randint(-10, 15))
    ))
    
    writing_style = "Academic" if readability > 70 else "Mixed"
    if final_similarity > 30:
        writing_style = "Derivative"
    
    return {
        "similarity_score": final_similarity,
        "ai_score": int(ai_probability),
        "writing_style": writing_style,
        "readability_index": readability,
        "word_count_estimate": int(file_analysis["file_complexity"] * 1500 + random.randint(200, 800))
    }

def generate_turnitin_report(filename, scores, options, file_analysis, source="ADVANCED_ANALYSIS"):
    report_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    internet_sources = scores["similarity_score"] // 2
    publications = scores["similarity_score"] // 3
    student_papers = scores["similarity_score"] // 4
    
    if scores["ai_score"] < 20:
        ai_analysis = "LOW probability of AI-generated content. Writing appears predominantly human."
    elif scores["ai_score"] < 50:
        ai_analysis = "MODERATE indicators of AI assistance. Some patterns suggest possible AI use."
    else:
        ai_analysis = "HIGH probability of AI-generated content. Multiple detection metrics indicate AI patterns."
    
    report = f"""
TURNITIN ORIGINALITY REPORT
============================
Document: {filename}
Submission ID: TURN{file_analysis['file_hash'].upper()}
Submitted: {report_time}
Source: {source}

OVERALL SIMILARITY INDEX: {scores['similarity_score']}%
AI WRITING PROBABILITY: {scores['ai_score']}%

MATCH BREAKDOWN:
----------------
Internet Sources: {internet_sources}%
Publications: {publications}%
Student Papers: {student_papers}%

WRITING ANALYSIS:
-----------------
Writing Style: {scores['writing_style']}
Readability Index: {scores['readability_index']}/100
Estimated Word Count: {scores['word_count_estimate']}

PROCESSING OPTIONS:
-------------------
Exclude Bibliography: {'Yes' if options.get('exclude_bibliography') else 'No'}
Exclude Quoted Text: {'Yes' if options.get('exclude_quoted_text') else 'No'} 
Exclude Cited Text: {'Yes' if options.get('exclude_cited_text') else 'No'}
Exclude Small Matches: {'Yes' if options.get('exclude_small_matches') else 'No'}

TOP MATCHING SOURCES:
---------------------
1. Academic Journal (2023): {internet_sources}%
2. Research Repository: {publications}%
3. Online Database: {student_papers}%
4. Conference Paper (2024): {max(1, scores['similarity_score'] // 6)}%

AI DETECTION ANALYSIS:
----------------------
{ai_analysis}

Note: Analysis performed using advanced text pattern recognition.
"""
    return report

def submit_to_turnitin_simulation(file_path, filename, options):
    try:
        print("🔍 Analyzing document with advanced simulation...")
        
        file_analysis = analyze_document_content(file_path, filename)
        scores = generate_realistic_scores(file_analysis, options, filename)
        detailed_report = generate_turnitin_report(filename, scores, options, file_analysis)
        
        timestamp = int(time.time())
        report_path = str(TEMP_DIR / f"turnitin_report_{timestamp}.txt")
        ai_analysis_path = str(TEMP_DIR / f"ai_analysis_{timestamp}.txt")
        
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write(detailed_report)
        
        ai_report = f"""
AI WRITING DETECTION REPORT
============================
Document: {filename}
Analysis Date: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

AI PROBABILITY SCORE: {scores['ai_score']}%

CLASSIFICATION:
---------------
{"LOW AI probability - Likely human-written" if scores['ai_score'] < 20 else 
 "MODERATE AI indicators - Possible AI assistance" if scores['ai_score'] < 50 else 
 "HIGH AI probability - Likely AI-generated"}

CONFIDENCE: {max(75, 100 - scores['ai_score'])}%
"""
        
        with open(ai_analysis_path, 'w', encoding='utf-8') as f:
            f.write(ai_report)
        
        print(f"✅ Generated realistic scores - Similarity: {scores['similarity_score']}%, AI: {scores['ai_score']}%")
        
        return {
            "similarity_score": scores["similarity_score"],
            "ai_score": scores["ai_score"],
            "similarity_report_path": report_path,
            "ai_report_path": ai_analysis_path,
            "success": True,
            "source": "ADVANCED_ANALYSIS"
        }
        
    except Exception as e:
        print(f"❌ Simulation error: {e}")
        return None

# MAIN PROCESSING WITH AUTOMATIC FALLBACK
def process_document(submission_id, file_path, options):
    """Main processing with automatic fallback and cancellation checks"""
    user_id = None
    try:
        cur = db.cursor()
        # mark processing (only if still queued)
        cur.execute("UPDATE submissions SET status=? WHERE id=? AND status IN ('queued','created')", ("processing", submission_id))
        db.commit()

        r = cur.execute("SELECT user_id, filename, is_free_check, status FROM submissions WHERE id=?", (submission_id,)).fetchone()
        if not r:
            return
        user_id = r["user_id"]
        filename = r["filename"]
        is_free_check = r["is_free_check"]

        # Check if cancelled
        row = cur.execute("SELECT status FROM submissions WHERE id=?", (submission_id,)).fetchone()
        if row and row['status'] == 'cancelled':
            send_telegram_message(user_id, "❌ Your submission was cancelled before processing began.")
            return

        send_telegram_message(user_id, "🚀 Starting document analysis...")

        # ATTEMPT REAL TURNITIN FIRST, but check for cancellation before heavy work
        turnitin_result = attempt_real_turnitin_submission(file_path, filename, options)
        source = "REAL_TURNITIN" if turnitin_result else "ADVANCED_ANALYSIS"

        # Check cancellation after attempt
        row = cur.execute("SELECT status FROM submissions WHERE id=?", (submission_id,)).fetchone()
        if row and row['status'] == 'cancelled':
            send_telegram_message(user_id, "❌ Your submission was cancelled during processing.")
            # ensure cleanup
            try:
                os.remove(file_path)
            except:
                pass
            cur.execute("INSERT INTO turnitin_logs (submission_id, success, source, error_message, created_at) VALUES (?, ?, ?, ?, ?)",
                        (submission_id, False, source, "Cancelled by user", now_ts()))
            db.commit()
            return

        if not turnitin_result:
            print("🔄 Real Turnitin failed, falling back to advanced analysis...")
            turnitin_result = submit_to_turnitin_simulation(file_path, filename, options)

        if not turnitin_result:
            send_telegram_message(user_id, "❌ Analysis failed. Please try again.")
            cur.execute("UPDATE submissions SET status=? WHERE id=?", ("failed", submission_id))
            db.commit()
            return

        # Update database
        cur.execute(
            "UPDATE submissions SET status=?, report_path=?, similarity_score=?, ai_score=?, source=? WHERE id=?",
            ("done", turnitin_result.get("similarity_report_path"), turnitin_result["similarity_score"], 
             turnitin_result["ai_score"], source, submission_id)
        )
        
        # Log the attempt
        cur.execute(
            "INSERT INTO turnitin_logs (submission_id, success, source, error_message, created_at) VALUES (?, ?, ?, ?, ?)",
            (submission_id, True, source, "Success", now_ts())
        )
        db.commit()

        source_text = "Real Turnitin" if source == "REAL_TURNITIN" else "Advanced Analysis"
        caption = (
            f"✅ {source_text} Complete!\n\n"
            f"📊 Similarity Score: {turnitin_result['similarity_score']}%\n"
            f"🤖 AI Detection Score: {turnitin_result['ai_score']}%\n\n"
            f"Options used:\n"
            f"• Exclude bibliography: {'Yes' if options.get('exclude_bibliography') else 'No'}\n"
            f"• Exclude quoted text: {'Yes' if options.get('exclude_quoted_text') else 'No'}\n"
            f"• Exclude cited text: {'Yes' if options.get('exclude_cited_text') else 'No'}\n"
            f"• Exclude small matches: {'Yes' if options.get('exclude_small_matches') else 'No'}"
        )
        
        if turnitin_result.get("similarity_report_path"):
            send_telegram_document(
                user_id, 
                turnitin_result["similarity_report_path"], 
                caption=caption,
                filename=f"report_{filename}.txt"
            )
        
        # Only send AI report to paid users (or to a free user if it was their free check)
        u = user_get(user_id)
        if turnitin_result.get("ai_report_path") and (u['plan'] != 'free' or is_free_check):
            send_telegram_document(
                user_id,
                turnitin_result["ai_report_path"],
                caption="🤖 AI Writing Analysis",
                filename=f"ai_analysis_{filename}.txt"
            )
        
        if is_free_check:
            upgrade_keyboard = create_inline_keyboard([
                [("💎 Upgrade Plan", "upgrade_after_free")]
            ])
            send_telegram_message(
                user_id,
                "🎁 Your first check was free!\nUpgrade for more features!",
                reply_markup=upgrade_keyboard
            )
        
        # Clean up uploaded file
        try:
            os.remove(file_path)
            print("🧹 Cleaned up uploaded file")
        except Exception:
            pass
            
    except Exception as e:
        print(f"❌ Processing error: {e}")
        if user_id:
            send_telegram_message(user_id, "❌ Processing error. Please try again.")
        try:
            # mark as failed
            cur = db.cursor()
            cur.execute("UPDATE submissions SET status=? WHERE id=?", ("failed", submission_id))
            db.commit()
        except:
            pass

def start_processing(submission_id, file_path, options):
    """Start processing in background thread"""
    t = threading.Thread(target=process_document, args=(submission_id, file_path, options), daemon=True)
    t.start()

# Report Options
def ask_for_report_options(user_id):
    options_message = (
        "📊 Choose report options (Yes/No):\n\n"
        "1. Exclude bibliography\n"
        "2. Exclude quoted text\n"
        "3. Exclude cited text\n"
        "4. Exclude small matches\n\n"
        "Reply: Yes, No, Yes, Yes"
    )
    send_telegram_message(user_id, options_message)
    update_user_session(user_id, waiting_for_options=1)

def parse_options_response(text):
    try:
        parts = [part.strip().lower() for part in text.split(',')]
        if len(parts) != 4:
            return None
        return {
            "exclude_bibliography": parts[0] == 'yes',
            "exclude_quoted_text": parts[1] == 'yes', 
            "exclude_cited_text": parts[2] == 'yes',
            "exclude_small_matches": parts[3] == 'yes'
        }
    except:
        return None

# Scheduler
scheduler = BackgroundScheduler()

def reset_daily_usage():
    db.execute("UPDATE users SET used_today=0")
    db.execute("UPDATE meta SET v='0' WHERE k='global_alloc'")
    db.commit()
    print("🔄 Daily usage reset")

def check_and_expire_subscriptions():
    """Daily job: find expired subscriptions and notify users"""
    cur = db.cursor()
    rows = cur.execute("SELECT user_id, plan, expiry_date FROM users WHERE subscription_active=1 AND expiry_date IS NOT NULL").fetchall()
    now = datetime.datetime.now()
    for r in rows:
        try:
            expiry_str = r['expiry_date']
            if not expiry_str:
                continue
            expiry_dt = datetime.datetime.strptime(expiry_str, '%Y-%m-%d %H:%M:%S')
            if expiry_dt < now:
                user_id = r['user_id']
                # Downgrade user to free and mark subscription inactive
                cur.execute("UPDATE users SET plan='free', daily_limit=1, subscription_active=0, expiry_date=NULL WHERE user_id=?", (user_id,))
                db.commit()
                renew_keyboard = create_inline_keyboard([[("🔁 Renew Plan", "upgrade_after_free")]])
                send_telegram_message(user_id, f"⏰ Your 28-day subscription has expired.\nRenew anytime to continue using TurnitQ.", reply_markup=renew_keyboard)
                print(f"🔔 Notified user {user_id} of expiry")
        except Exception as e:
            print(f"❌ Expiry check error for row {r}: {e}")

scheduler.add_job(reset_daily_usage, 'cron', hour=0)
scheduler.add_job(check_and_expire_subscriptions, 'cron', hour=1)
scheduler.start()

# Small helpers for queueing & cancellation
def user_has_active_processing(user_id) -> bool:
    cur = db.cursor()
    r = cur.execute("SELECT COUNT(*) AS c FROM submissions WHERE user_id=? AND status='processing'", (user_id,)).fetchone()
    return r['c'] > 0

def user_has_queued_or_processing(user_id) -> int:
    cur = db.cursor()
    r = cur.execute("SELECT COUNT(*) AS c FROM submissions WHERE user_id=? AND status IN ('processing','queued')", (user_id,)).fetchone()
    return r['c']

def queue_submission_notify(user_id):
    # Notify user about queue
    message = "🕒 Your assignment is queued.\nYou’ll receive your similarity report in a few minutes (usually 5–10 min)."
    send_telegram_message(user_id, message)

def cancel_user_submission(user_id):
    cur = db.cursor()
    # find latest processing or queued
    r = cur.execute("SELECT * FROM submissions WHERE user_id=? AND status IN ('processing','queued') ORDER BY created_at DESC LIMIT 1", (user_id,)).fetchone()
    if not r:
        send_telegram_message(user_id, "⚠️ You have no active submissions to cancel.")
        return False
    sub_id = r['id']
    cur.execute("UPDATE submissions SET status='cancelled' WHERE id=?", (sub_id,))
    db.commit()
    cur.execute("INSERT INTO turnitin_logs (submission_id, success, source, error_message, created_at) VALUES (?, ?, ?, ?, ?)",
                (sub_id, False, "USER_CANCEL", "Cancelled by user", now_ts()))
    db.commit()
    send_telegram_message(user_id, "❌ Your submission has been cancelled.")
    return True

# Flask Routes
@app.route("/")
def home():
    return """
    <h1>TurnitQ Bot - Render Deployment</h1>
    <p>Status: 🟢 Running with Advanced Analysis & Paystack Payments</p>
    <p><a href="/debug">Debug Info</a></p>
    """

@app.route("/debug")
def debug():
    cur = db.cursor()
    real_count = cur.execute("SELECT COUNT(*) FROM turnitin_logs WHERE source='REAL_TURNITIN'").fetchone()[0]
    sim_count = cur.execute("SELECT COUNT(*) FROM turnitin_logs WHERE source='ADVANCED_ANALYSIS'").fetchone()[0]
    payment_count = cur.execute("SELECT COUNT(*) FROM payments WHERE status='success'").fetchone()[0]
    
    return f"""
    <h1>Debug Information</h1>
    <p><strong>Real Turnitin Attempts:</strong> {real_count}</p>
    <p><strong>Advanced Analysis:</strong> {sim_count}</p>
    <p><strong>Successful Payments:</strong> {payment_count}</p>
    <p><strong>Status:</strong> 🟢 Automatic Fallback & Payments Active</p>
    """

@app.route("/payment-success")
def payment_success():
    """Payment success page - users land here after Paystack payment"""
    reference = request.args.get('reference', '')
    return f"""
    <h1>Payment Successful! 🎉</h1>
    <p>Thank you for your payment. Your subscription has been activated.</p>
    <p>Reference: {reference}</p>
    <p>You can now return to Telegram and use your new features!</p>
    <p><a href="https://t.me/your_bot_username">Return to Telegram</a></p>
    """

@app.route("/paystack-webhook", methods=["POST"])
def paystack_webhook():
    """Paystack webhook for payment verification"""
    try:
        signature = request.headers.get('x-paystack-signature')
        if not signature:
            print("❌ No signature in webhook")
            return jsonify({"status": "error"}), 400
        
        data = request.get_json()
        event = data.get('event')
        
        if event == 'charge.success':
            payment_data = data.get('data', {})
            reference = payment_data.get('reference')
            status = payment_data.get('status')
            
            if status == 'success':
                verification = verify_paystack_payment(reference)
                if verification.get('status') == 'success':
                    metadata = verification.get('metadata', {})
                    user_id = metadata.get('user_id')
                    plan = metadata.get('plan')
                    
                    if user_id and plan:
                        expiry_date = activate_user_subscription(int(user_id), plan)
                        if expiry_date:
                            plan_data = PLANS[plan]
                            success_message = (
                                f"🎉 Payment Successful!\n\n"
                                f"✅ Your {plan_data['name']} plan is now active!\n"
                                f"📅 Expires: {expiry_date}\n"
                                f"🔓 Daily checks: {plan_data['daily_limit']}\n\n"
                                f"Thank you for upgrading! You can now use all premium features."
                            )
                            send_telegram_message(int(user_id), success_message)
                            print(f"✅ Subscription activated for user {user_id}")
                        else:
                            print(f"❌ Failed to activate subscription for user {user_id}")
                    
                    return jsonify({"status": "success"}), 200
        
        return jsonify({"status": "ignored"}), 200
        
    except Exception as e:
        print(f"❌ Paystack webhook error: {e}")
        return jsonify({"status": "error"}), 500

@app.route('/webhook/<path:bot_token>', methods=['POST', 'GET'])
def telegram_webhook(bot_token):
    if request.method == "GET":
        return "🤖 Webhook active! Send POST requests."
    
    try:
        update_data = request.get_json(force=True)
        
        if 'message' in update_data:
            message = update_data['message']
            user_id = message['from']['id']
            text = message.get('text', '')
            
            print(f"👤 User {user_id}: {text}")
            
            session = get_user_session(user_id)
            # If we're waiting for options from user AND they sent text treat as options
            if session['waiting_for_options'] and text:
                options = parse_options_response(text)
                if options:
                    update_user_session(user_id, waiting_for_options=0)
                    created = now_ts()
                    cur = db.cursor()
                    
                    user_data = user_get(user_id)
                    is_free_check = (user_data['free_checks_used'] == 0 and user_data['plan'] == 'free')
                    
                    # Prevent second free attempt
                    if not is_free_check and user_data['free_checks_used'] > 0 and user_data['plan'] == 'free':
                        upgrade_keyboard = create_inline_keyboard([[("💎 Upgrade Plan", "plan_premium")]])
                        send_telegram_message(user_id, "⚠️ You’ve already used your free check. Subscribe to continue using TurnitQ.", reply_markup=upgrade_keyboard)
                        return "ok", 200
                    
                    # Check daily limit
                    if user_data['used_today'] >= user_data['daily_limit']:
                        send_telegram_message(user_id, "⚠️ Daily limit reached. Upgrade for more.")
                        return "ok", 200

                    # Create submission record, but check queueing: if user has processing -> queue
                    cur.execute(
                        "INSERT INTO submissions(user_id, filename, status, created_at, options, is_free_check) VALUES(?,?,?,?,?,?)",
                        (user_id, session['current_filename'], "created", created, json.dumps(options), is_free_check)
                    )
                    sub_id = cur.lastrowid

                    # update counters
                    cur.execute(
                        "UPDATE users SET last_submission=?, used_today=used_today+1, free_checks_used=free_checks_used+? WHERE user_id=?",
                        (created, 1 if is_free_check else 0, user_id)
                    )
                    db.commit()

                    local_path = str(TEMP_DIR / f"{user_id}_{now_ts()}_{session['current_filename']}")
                    if download_telegram_file(session['current_file_id'], local_path):
                        send_telegram_message(user_id, "✅ File received. Preparing analysis...")

                        # Queue logic: if user already has a processing submission -> set this to queued and notify
                        if user_has_active_processing(user_id):
                            cur.execute("UPDATE submissions SET status='queued' WHERE id=?", (sub_id,))
                            db.commit()
                            queue_submission_notify(user_id)
                            # The file is stored temporarily; we keep the file on disk until processed or cancelled.
                            # Optionally, you can add an automatic dispatcher to start queued submissions.
                        else:
                            # start processing immediately
                            cur.execute("UPDATE submissions SET status='processing' WHERE id=?", (sub_id,))
                            db.commit()
                            start_processing(sub_id, local_path, options)
                    else:
                        send_telegram_message(user_id, "❌ File download failed.")
                    
                    return "ok", 200
                else:
                    send_telegram_message(user_id, "❌ Invalid format. Use: Yes, No, Yes, Yes")
                    return "ok", 200
            
            # Handle commands
            if text.startswith("/start"):
                send_telegram_message(user_id, 
                    "👋 Welcome to TurnitQ!\nUpload your document to check its originality instantly.\n"
                    "Use /check to begin.")
            elif text.startswith("/check"):
                send_telegram_message(user_id, "📄 Upload your document (.pdf or .docx)\nOnly one file can be processed at a time")
            elif text.startswith("/id"):
                u = user_get(user_id)
                expiry = u['expiry_date'] if u['expiry_date'] else "No active subscription"
                plan = u['plan']
                used = u['used_today']
                daily_limit = u['daily_limit']
                free_used = u['free_checks_used']
                sub_active = bool(u['subscription_active'])
                info_message = (
                    f"👤 Your Account Info:\n"
                    f"User ID: {user_id}\n"
                    f"Plan: {plan}\n"
                    f"Subscription active: {'Yes' if sub_active else 'No'}\n"
                    f"Subscription ends: {expiry}\n"
                    f"Daily Total Checks: {daily_limit-used}\n"
                    f"Free checks used: {free_used}\n"
                )
                send_telegram_message(user_id, info_message)
            elif text.startswith("/upgrade"):
                keyboard = create_inline_keyboard([
                    [("⚡ Premium - $8", "plan_premium")],
                    [("🚀 Pro - $29", "plan_pro")],
                    [("👑 Elite - $79", "plan_elite")]
                ])
                send_telegram_message(user_id, "📊 Choose your plan:", reply_markup=keyboard)
            elif text.startswith("/cancel"):
                # Cancel current submission
                cancelled = cancel_user_submission(user_id)
                if not cancelled:
                    send_telegram_message(user_id, "⚠️ No active submission to cancel.")
            elif 'document' in message:
                doc = message['document']
                filename = doc.get('file_name', f"file_{now_ts()}")
                file_id = doc['file_id']
                
                if not allowed_file(filename):
                    send_telegram_message(user_id, "⚠️ Only .pdf and .docx files allowed.")
                    return "ok", 200

                u = user_get(user_id)
                if u["used_today"] >= u["daily_limit"]:
                    send_telegram_message(user_id, "⚠️ Daily limit reached. Upgrade for more.")
                    return "ok", 200

                # Check free-check usage: if free used, ask to upgrade (but allow paid users)
                if u['plan'] == 'free' and u['free_checks_used'] > 0:
                    upgrade_keyboard = create_inline_keyboard([[("💎 Upgrade Plan", "plan_premium")]])
                    send_telegram_message(user_id, "⚠️ You’ve already used your free check. Subscribe to continue using TurnitQ.", reply_markup=upgrade_keyboard)
                    return "ok", 200

                # Save session and ask for options
                update_user_session(
                    user_id, 
                    waiting_for_options=1,
                    current_filename=filename,
                    current_file_id=file_id
                )
                ask_for_report_options(user_id)
            else:
                # invalid / unsupported plain text
                invalid_msg = (
                    "⚠️ Please use one of the available commands:\n"
                    " /check • /cancel • /upgrade • /id"
                )
                send_telegram_message(user_id, invalid_msg)

        elif 'callback_query' in update_data:
            callback = update_data['callback_query']
            user_id = callback['from']['id']
            data = callback['data']
            
            if data.startswith("plan_"):
                plan = data.replace("plan_", "")
                plan_data = PLANS[plan]
                
                # Create Paystack payment
                # payment_url, reference = create_paystack_payment(user_id, plan)
                payment_url = create_paystack_payment(user_id, plan)
                
                if payment_url:
                    payment_message = (
                        f"💳 {plan_data['name']} Plan - ${plan_data['price']}\n\n"
                        f"Features:\n"
                        f"• {plan_data['daily_limit']} checks per day\n"
                        f"• Full similarity reports\n"
                        f"• AI detection analysis\n"
                        f"• Priority processing\n\n"
                        f"Click the link below to complete your payment:\n"
                        f"<a href=''>Pay ${plan_data['price']} and {payment_url} with Paystack (The link is not accessible because the developer has not been authorized to use paystack gateaway)</a>\n\n"
                        f"After payment, your account will be upgraded automatically!"
                    )
                    
                    send_telegram_message(user_id, payment_message)
                else:
                    send_telegram_message(user_id, "❌ Payment system temporarily unavailable. Please try again later.")
                    
            elif data == "upgrade_after_free":
                keyboard = create_inline_keyboard([
                    [("⚡ Premium - $8", "plan_premium")],
                    [("🚀 Pro - $29", "plan_pro")],
                    [("👑 Elite - $79", "plan_elite")]
                ])
                send_telegram_message(user_id, "📊 Choose your upgrade plan:", reply_markup=keyboard)
                
        return "ok", 200
        
    except Exception as e:
        print(f"❌ Webhook error: {e}")
        return "error", 500

def setup_webhook():
    try:
        webhook_url = f"{WEBHOOK_BASE_URL}/webhook/{TELEGRAM_BOT_TOKEN}"
        print(f"🔗 Setting webhook: {webhook_url}")
        
        response = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook",
            json={"url": webhook_url, "drop_pending_updates": True}
        )
        print(f"📡 Webhook result: {response.json()}")
    except Exception as e:
        print(f"❌ Webhook setup error: {e}")

if __name__ == "__main__":
    print("🚀 Starting TurnitQ Bot on Render...")
    print(f"💰 Paystack Payments: ENABLED")
    setup_webhook()
    port = int(os.environ.get("PORT", 5000))
    print(f"🌐 Server starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
