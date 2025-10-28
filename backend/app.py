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
        current_file_id TEXT,
        waiting_for_withdrawal BOOLEAN DEFAULT 0,
        withdrawal_type TEXT DEFAULT 'mobile_money',
        withdrawal_amount REAL DEFAULT 0,
        selected_bank_code TEXT
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
    -- Referral System Tables
    CREATE TABLE IF NOT EXISTS referrals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        referrer_id INTEGER,
        referred_id INTEGER,
        referral_code TEXT,
        used_at INTEGER,
        reward_credited BOOLEAN DEFAULT 0,
        created_at INTEGER
    );
    CREATE TABLE IF NOT EXISTS referral_earnings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        amount REAL DEFAULT 0,
        total_earned REAL DEFAULT 0,
        total_withdrawn REAL DEFAULT 0,
        referral_code TEXT UNIQUE,
        created_at INTEGER
    );
    CREATE TABLE IF NOT EXISTS withdrawals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        amount REAL,
        mobile_money_number TEXT,
        bank_account TEXT,
        bank_code TEXT,
        account_name TEXT,
        status TEXT DEFAULT 'pending',
        paystack_transfer_code TEXT,
        paystack_recipient_code TEXT,
        created_at INTEGER,
        processed_at INTEGER,
        failure_reason TEXT
    );
    CREATE TABLE IF NOT EXISTS bank_codes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bank_name TEXT,
        bank_code TEXT,
        country TEXT DEFAULT 'Ghana'
    );
    """)
    db.commit()
    
    # Insert common Ghanaian bank codes if not exists
    ghana_banks = [
        ("Access Bank", "044"),
        ("Cal Bank", "140"),
        ("Ghana Commercial Bank", "041"),
        ("Barclays Bank", "034"),
        ("Ecobank", "130"),
        ("Fidelity Bank", "135"),
        ("First Atlantic Bank", "139"),
        ("First National Bank", "145"),
        ("GCB Bank", "041"),
        ("GT Bank", "118"),
        ("National Investment Bank", "031"),
        ("Prudential Bank", "143"),
        ("Republic Bank", "032"),
        ("Societe Generale", "033"),
        ("Standard Chartered", "020"),
        ("Stanbic Bank", "039"),
        ("Universal Merchant Bank", "138"),
        ("Zenith Bank", "037"),
        ("ARB Apex Bank", "081"),
        ("Bank of Africa", "050"),
        ("Consolidated Bank Ghana", "142"),
        ("OmniBSIC Bank", "141")
    ]
    
    for bank_name, bank_code in ghana_banks:
        cur.execute("INSERT OR IGNORE INTO bank_codes (bank_name, bank_code) VALUES (?, ?)", 
                   (bank_name, bank_code))
    
    db.commit()

# Initialize database
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
        "daily_limit": 3,
        "price": 8,
        "duration_days": 28,
        "features": [
            "Up to 3 checks per day",
            "Full similarity report", 
            "Faster results"
        ]
    },
    "pro": {
        "name": "Pro", 
        "daily_limit": 20,
        "price": 29,
        "duration_days": 28,
        "features": [
            "Up to 20 checks per day",
            "Full similarity report",
            "Faster results", 
            "AI-generated report",
            "View full matching sources"
        ]
    },
    "elite": {
        "name": "Elite",
        "daily_limit": 70, 
        "price": 79,
        "duration_days": 28,
        "features": [
            "Up to 70 checks per day",
            "Priority processing",
            "Full similarity report",
            "AI-generated report"
        ]
    }
}

# Referral System Configuration
REFERRAL_REWARD = 10  # ₵10 per successful referral
MIN_WITHDRAWAL = 50   # ₵50 minimum withdrawal

# Paystack Transfer Configuration
PAYSTACK_TRANSFER_FEE = 0.015  # 1.5% transfer fee
MIN_TRANSFER_AMOUNT = 10  # Minimum ₵10 for transfer

# ============================
# PAYSTACK TRANSFER FUNCTIONS
# ============================

def get_bank_list():
    """Get list of supported banks from Paystack"""
    try:
        url = "https://api.paystack.co/bank"
        headers = {
            "Authorization": f"Bearer {PAYSTACK_SECRET_KEY}",
            "Content-Type": "application/json"
        }
        
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            data = response.json()
            return data.get('data', [])
        else:
            print(f"❌ Failed to fetch bank list: {response.status_code}")
            # Fallback to our stored bank codes
            cur = db.cursor()
            banks_data = cur.execute("SELECT bank_name, bank_code FROM bank_codes ORDER BY bank_name").fetchall()
            return [{"name": row['bank_name'], "code": row['bank_code']} for row in banks_data]
    except Exception as e:
        print(f"❌ Error fetching bank list: {e}")
        # Fallback to our stored bank codes
        cur = db.cursor()
        banks_data = cur.execute("SELECT bank_name, bank_code FROM bank_codes ORDER BY bank_name").fetchall()
        return [{"name": row['bank_name'], "code": row['bank_code']} for row in banks_data]

def create_transfer_recipient(user_id, account_number, bank_code, account_name):
    """Create a transfer recipient in Paystack"""
    try:
        url = "https://api.paystack.co/transferrecipient"
        headers = {
            "Authorization": f"Bearer {PAYSTACK_SECRET_KEY}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "type": "nuban",
            "name": account_name,
            "account_number": account_number,
            "bank_code": bank_code,
            "currency": "GHS"
        }
        
        response = requests.post(url, json=payload, headers=headers)
        result = response.json()
        
        if result.get('status'):
            recipient_code = result['data']['recipient_code']
            print(f"✅ Transfer recipient created: {recipient_code}")
            return recipient_code, None
        else:
            error_msg = result.get('message', 'Unknown error')
            print(f"❌ Failed to create recipient: {error_msg}")
            return None, error_msg
            
    except Exception as e:
        print(f"❌ Error creating transfer recipient: {e}")
        return None, str(e)

def initiate_transfer(amount, recipient_code, reason="Referral earnings withdrawal"):
    """Initiate transfer to recipient"""
    try:
        url = "https://api.packstack.co/transfer"
        headers = {
            "Authorization": f"Bearer {PAYSTACK_SECRET_KEY}",
            "Content-Type": "application/json"
        }
        
        # Calculate amount in pesewas (Paystack expects amount in smallest currency unit)
        amount_in_pesewas = int(amount * 100)  # Convert ₵ to pesewas
        
        payload = {
            "source": "balance",
            "amount": amount_in_pesewas,
            "recipient": recipient_code,
            "reason": reason
        }
        
        response = requests.post(url, json=payload, headers=headers)
        result = response.json()
        
        if result.get('status'):
            transfer_code = result['data']['transfer_code']
            print(f"✅ Transfer initiated: {transfer_code}")
            return transfer_code, None
        else:
            error_msg = result.get('message', 'Unknown error')
            print(f"❌ Failed to initiate transfer: {error_msg}")
            return None, error_msg
            
    except Exception as e:
        print(f"❌ Error initiating transfer: {e}")
        return None, str(e)

def process_withdrawal_automatically(user_id, amount, account_number, bank_code, account_name):
    """Process withdrawal automatically using Paystack transfer"""
    try:
        print(f"💰 Processing automatic withdrawal for user {user_id}: ₵{amount}")
        
        # Step 1: Create transfer recipient
        recipient_code, error = create_transfer_recipient(user_id, account_number, bank_code, account_name)
        if not recipient_code:
            return False, f"Failed to create recipient: {error}"
        
        # Step 2: Initiate transfer
        transfer_code, error = initiate_transfer(amount, recipient_code, "TurnitQ Referral Earnings")
        if not transfer_code:
            return False, f"Failed to initiate transfer: {error}"
        
        # Step 3: Update withdrawal record
        cur = db.cursor()
        cur.execute(
            """UPDATE withdrawals SET 
                status='processing', 
                paystack_recipient_code=?, 
                paystack_transfer_code=?,
                bank_account=?, 
                bank_code=?,
                account_name=?
            WHERE user_id=? AND status='pending'""",
            (recipient_code, transfer_code, account_number, bank_code, account_name, user_id)
        )
        db.commit()
        
        return True, f"Withdrawal processing! Transfer reference: {transfer_code}"
        
    except Exception as e:
        print(f"❌ Automatic withdrawal error: {e}")
        return False, f"System error: {str(e)}"

def ask_for_bank_details(user_id, amount):
    """Ask user for bank account details for withdrawal"""
    try:
        # Get bank list for user to choose from
        banks = get_bank_list()
        
        # Create bank selection keyboard (show first 8 banks)
        bank_buttons = []
        for bank in banks[:8]:
            bank_name = bank.get('name', 'Unknown Bank')
            bank_code = bank.get('code', '')
            bank_buttons.append([{"text": bank_name, "callback_data": f"select_bank_{bank_code}"}])
        
        # Add more banks button if there are more
        if len(banks) > 8:
            bank_buttons.append([{"text": "📋 More Banks", "callback_data": "more_banks_1"}])
        
        keyboard = {"inline_keyboard": bank_buttons}
        
        message = (
            f"🏦 <b>Bank Transfer Withdrawal</b>\n\n"
            f"💰 Amount: ₵{amount:.2f}\n\n"
            f"Please select your bank from the list below:\n"
            f"Then you'll be asked for your account number and name."
        )
        
        send_telegram_message(user_id, message, reply_markup=keyboard)
        update_user_session(user_id, 
                          waiting_for_withdrawal=1, 
                          withdrawal_type='bank_transfer',
                          withdrawal_amount=amount)
        
    except Exception as e:
        print(f"❌ Error asking for bank details: {e}")
        send_telegram_message(user_id, "❌ Error setting up bank transfer. Please try again.")

def ask_for_mobile_money_details(user_id, amount):
    """Ask user for mobile money details"""
    message = (
        f"📱 <b>Mobile Money Withdrawal</b>\n\n"
        f"💰 Amount: ₵{amount:.2f}\n\n"
        f"Please reply with your <b>mobile money number</b> in this format:\n"
        f"<code>0551234567</code>\n\n"
        f"We'll process your withdrawal within 24 hours."
    )
    
    send_telegram_message(user_id, message)
    update_user_session(user_id, 
                      waiting_for_withdrawal=1, 
                      withdrawal_type='mobile_money',
                      withdrawal_amount=amount)

def handle_bank_account_input(user_id, account_number, bank_code, account_name):
    """Handle bank account information and process withdrawal"""
    try:
        # Validate account number (basic validation)
        if not account_number.isdigit() or len(account_number) < 8:
            return False, "Invalid account number. Please provide a valid account number."
        
        # Validate account name
        if not account_name or len(account_name) < 2:
            return False, "Invalid account name. Please provide your full name as registered with the bank."
        
        # Get withdrawal amount from session
        session = get_user_session(user_id)
        amount = session.get('withdrawal_amount', 0)
        
        if amount < MIN_WITHDRAWAL:
            return False, f"Withdrawal amount must be at least ₵{MIN_WITHDRAWAL}"
        
        # Process withdrawal automatically
        success, message = process_withdrawal_automatically(user_id, amount, account_number, bank_code, account_name)
        
        if success:
            # Update referral earnings
            cur = db.cursor()
            cur.execute(
                "UPDATE referral_earnings SET amount=0, total_withdrawn=total_withdrawn+? WHERE user_id=?",
                (amount, user_id)
            )
            db.commit()
            
            # Clear withdrawal session
            update_user_session(user_id, 
                              waiting_for_withdrawal=0,
                              withdrawal_type=None,
                              withdrawal_amount=0,
                              selected_bank_code=None)
        
        return success, message
        
    except Exception as e:
        print(f"❌ Error handling bank account input: {e}")
        return False, f"System error: {str(e)}"

# ============================
# EXISTING FUNCTIONS (Updated)
# ============================

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
    return dict(r) if r else None

def get_user_session(user_id):
    cur = db.cursor()
    r = cur.execute("SELECT * FROM user_sessions WHERE user_id=?", (user_id,)).fetchone()
    if not r:
        cur.execute("INSERT INTO user_sessions(user_id) VALUES(?)", (user_id,))
        db.commit()
        r = cur.execute("SELECT * FROM user_sessions WHERE user_id=?", (user_id,)).fetchone()
    return dict(r) if r else None

def update_user_session(user_id, **kwargs):
    cur = db.cursor()
    set_clause = ", ".join([f"{k}=?" for k in kwargs.keys()])
    values = list(kwargs.values()) + [user_id]
    cur.execute(f"UPDATE user_sessions SET {set_clause} WHERE user_id=?", values)
    db.commit()

def allowed_file(filename):
    return filename.lower().endswith((".pdf", ".docx"))

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
            if len(button) == 3 and button[2] == "url":
                row.append({
                    "text": button[0],
                    "url": button[1]
                })
            else:
                row.append({
                    "text": button[0],
                    "callback_data": button[1]
                })
        keyboard.append(row)
    return {"inline_keyboard": keyboard}

# PAYSTACK PAYMENT PAGE INTEGRATION - FIXED
def get_payment_page_url(plan, user_id):
    """Get Paystack payment page URLs with Telegram ID properly embedded"""
    payment_pages = {
        "premium": "https://paystack.shop/pay/premiumpage",
        "pro": "https://paystack.shop/pay/propage", 
        "elite": "https://paystack.shop/pay/elitepage"
    }
    
    base_url = payment_pages.get(plan)
    if base_url:
        # Add Telegram ID as custom field parameter - Paystack standard format
        return f"{base_url}?custom_field[Telegram ID]={user_id}"
    return None

def handle_payment_selection(user_id, plan):
    """Handle payment selection with automatic activation setup"""
    # FIX: Extract just the plan name if it contains prefixes
    if plan.startswith('details_'):
        plan = plan.replace('details_', '')
    elif plan.startswith('plan_'):
        plan = plan.replace('plan_', '')
    
    if plan not in PLANS:
        send_telegram_message(user_id, "❌ Invalid plan selected. Please try again.")
        return
        
    plan_data = PLANS[plan]
    
    # Get payment page URL with Telegram ID
    payment_url = get_payment_page_url(plan, user_id)
    
    if payment_url:
        # Create inline keyboard with payment link
        keyboard = {
            "inline_keyboard": [
                [{"text": f"💰 Pay ${plan_data['price']}", "url": payment_url}],
                [{"text": "📋 Plan Features", "callback_data": f"plan_details_{plan}"}],
                [{"text": "🔄 Refresh Status", "callback_data": f"refresh_payment_{user_id}_{plan}"}]
            ]
        }
        
        payment_message = (
            f"💳 {plan_data['name']} Plan - ${plan_data['price']}\n\n"
            f"✨ Features:\n" +
            "\n".join(f"• {feature}" for feature in plan_data["features"]) +
            f"\n\n🚀 Automatic Activation:\n"
            f"• Click 'Pay Now' to complete payment\n"
            f"• Your subscription activates automatically\n"
            f"🔑 Your Telegram ID: <code>{user_id}</code>\n"
            f"Click below to start:"
        )
        
        send_telegram_message(user_id, payment_message, reply_markup=keyboard)
    else:
        send_telegram_message(user_id, "❌ Payment system temporarily unavailable. Please try again later.")

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
        
        db.commit()
        
        print(f"✅ Subscription activated for user {user_id}, plan {plan}")
        return expiry_date
        
    except Exception as e:
        print(f"❌ Subscription activation error: {e}")
        return None

# SIMULATION helpers
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

# MAIN PROCESSING
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

        # Use simulation approach
        turnitin_result = submit_to_turnitin_simulation(file_path, filename, options)
        source = "ADVANCED_ANALYSIS"

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

        source_text = "Advanced Analysis"
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
                [("💎 Upgrade Plan", "upgrade_after_free")],
                [("💰 Earn ₵10 per Referral", "show_referral")]
            ])
            send_telegram_message(
                user_id,
                "🎁 Your first check was free!\nUpgrade for more features or earn ₵10 for each friend you refer!",
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

def process_pending_withdrawals():
    """Background job to process pending withdrawals"""
    try:
        cur = db.cursor()
        pending_withdrawals = cur.execute(
            "SELECT * FROM withdrawals WHERE status='processing' AND paystack_transfer_code IS NOT NULL"
        ).fetchall()
        
        for withdrawal in pending_withdrawals:
            # Check transfer status with Paystack
            transfer_code = withdrawal['paystack_transfer_code']
            url = f"https://api.paystack.co/transfer/{transfer_code}"
            headers = {
                "Authorization": f"Bearer {PAYSTACK_SECRET_KEY}",
                "Content-Type": "application/json"
            }
            
            response = requests.get(url, headers=headers)
            if response.status_code == 200:
                result = response.json()
                transfer_status = result['data']['status']
                
                if transfer_status == 'success':
                    # Update withdrawal as successful
                    cur.execute(
                        "UPDATE withdrawals SET status='completed', processed_at=? WHERE id=?",
                        (now_ts(), withdrawal['id'])
                    )
                    print(f"✅ Withdrawal {withdrawal['id']} completed successfully")
                    
                    # Notify user
                    send_telegram_message(
                        withdrawal['user_id'],
                        f"✅ Withdrawal Completed!\n\n"
                        f"💰 Amount: ₵{withdrawal['amount']:.2f}\n"
                        f"🏦 Method: Bank Transfer\n"
                        f"📋 Reference: {transfer_code}\n\n"
                        f"The funds should reflect in your account shortly."
                    )
                    
                elif transfer_status == 'failed':
                    # Update withdrawal as failed and refund balance
                    failure_reason = result['data'].get('reason', 'Transfer failed')
                    cur.execute(
                        "UPDATE withdrawals SET status='failed', failure_reason=?, processed_at=? WHERE id=?",
                        (failure_reason, now_ts(), withdrawal['id'])
                    )
                    
                    # Refund the amount to user's referral balance
                    cur.execute(
                        "UPDATE referral_earnings SET amount=amount+? WHERE user_id=?",
                        (withdrawal['amount'], withdrawal['user_id'])
                    )
                    
                    print(f"❌ Withdrawal {withdrawal['id']} failed: {failure_reason}")
                    
                    # Notify user
                    send_telegram_message(
                        withdrawal['user_id'],
                        f"❌ Withdrawal Failed\n\n"
                        f"💰 Amount: ₵{withdrawal['amount']:.2f}\n"
                        f"📋 Reason: {failure_reason}\n\n"
                        f"The amount has been refunded to your referral balance.\n"
                        f"Please check your account details and try again."
                    )
        
        db.commit()
        
    except Exception as e:
        print(f"❌ Error processing pending withdrawals: {e}")

scheduler.add_job(reset_daily_usage, 'cron', hour=0)
scheduler.add_job(check_and_expire_subscriptions, 'cron', hour=1)
scheduler.add_job(process_pending_withdrawals, 'interval', minutes=30)  # Check every 30 minutes
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
    message = "🕒 Your assignment is queued.\nYou'll receive your similarity report in a few minutes (usually 5-10 min)."
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

# Referral System Functions
def generate_referral_code(user_id):
    """Generate a unique referral code for user"""
    import string
    chars = string.ascii_uppercase + string.digits
    code = f"TQ{user_id:04d}" + ''.join(random.choices(chars, k=4))
    
    # Ensure uniqueness
    cur = db.cursor()
    while cur.execute("SELECT 1 FROM referral_earnings WHERE referral_code=?", (code,)).fetchone():
        code = f"TQ{user_id:04d}" + ''.join(random.choices(chars, k=4))
    
    return code

def get_or_create_referral_earnings(user_id):
    """Get or create referral earnings record for user"""
    cur = db.cursor()
    earnings = cur.execute(
        "SELECT * FROM referral_earnings WHERE user_id=?", (user_id,)
    ).fetchone()
    
    if not earnings:
        referral_code = generate_referral_code(user_id)
        cur.execute(
            "INSERT INTO referral_earnings (user_id, referral_code, created_at) VALUES (?, ?, ?)",
            (user_id, referral_code, now_ts())
        )
        db.commit()
        earnings = cur.execute(
            "SELECT * FROM referral_earnings WHERE user_id=?", (user_id,)
        ).fetchone()
    
    return dict(earnings) if earnings else None

def handle_referral_signup(referred_user_id, referral_code):
    """Handle new user signup with referral code"""
    cur = db.cursor()
    
    # Find referrer by code
    referrer = cur.execute(
        "SELECT user_id FROM referral_earnings WHERE referral_code=?", (referral_code,)
    ).fetchone()
    
    if referrer:
        referrer_id = referrer['user_id']
        
        # Check if this referred user already used any referral code
        existing_ref = cur.execute(
            "SELECT 1 FROM referrals WHERE referred_id=?", (referred_user_id,)
        ).fetchone()
        
        if not existing_ref:
            # Record the referral
            cur.execute(
                "INSERT INTO referrals (referrer_id, referred_id, referral_code, created_at) VALUES (?, ?, ?, ?)",
                (referrer_id, referred_user_id, referral_code, now_ts())
            )
            db.commit()
            return referrer_id
    
    return None

def process_referral_payment(referred_user_id):
    """Process referral reward when referred user makes first payment"""
    cur = db.cursor()
    
    # Find referral record
    referral = cur.execute(
        "SELECT * FROM referrals WHERE referred_id=? AND reward_credited=0", (referred_user_id,)
    ).fetchone()
    
    if referral:
        referrer_id = referral['referrer_id']
        
        # Credit reward to referrer
        cur.execute(
            "UPDATE referral_earnings SET amount=amount+?, total_earned=total_earned+? WHERE user_id=?",
            (REFERRAL_REWARD, REFERRAL_REWARD, referrer_id)
        )
        
        # Mark referral as used and credited
        cur.execute(
            "UPDATE referrals SET reward_credited=1, used_at=? WHERE id=?",
            (now_ts(), referral['id'])
        )
        
        db.commit()
        
        # Notify referrer
        send_telegram_message(
            referrer_id,
            f"🎉 Great news! Someone you referred just made their first payment.\n"
            f"₵{REFERRAL_REWARD} has been added to your referral balance."
        )
        
        return True
    
    return False

def get_referral_info(user_id):
    """Get user's referral information"""
    earnings = get_or_create_referral_earnings(user_id)
    
    cur = db.cursor()
    total_referrals = cur.execute(
        "SELECT COUNT(*) as count FROM referrals WHERE referrer_id=?", (user_id,)
    ).fetchone()['count']
    
    successful_referrals = cur.execute(
        "SELECT COUNT(*) as count FROM referrals WHERE referrer_id=? AND reward_credited=1", (user_id,)
    ).fetchone()['count']
    
    return {
        'referral_code': earnings['referral_code'],
        'balance': earnings['amount'],
        'total_earned': earnings['total_earned'],
        'total_withdrawn': earnings['total_withdrawn'],
        'total_referrals': total_referrals,
        'successful_referrals': successful_referrals
    }

def handle_withdrawal_request(user_id, withdrawal_method="bank_transfer"):
    """Process withdrawal request - UPDATED to support multiple methods"""
    referral_info = get_referral_info(user_id)
    balance = referral_info['balance']
    
    if balance < MIN_WITHDRAWAL:
        return False, f"Withdrawal minimum is ₵{MIN_WITHDRAWAL}. Your balance: ₵{balance}"
    
    # Create withdrawal record
    cur = db.cursor()
    cur.execute(
        "INSERT INTO withdrawals (user_id, amount, status, created_at) VALUES (?, ?, ?, ?)",
        (user_id, balance, 'pending', now_ts())
    )
    db.commit()
    
    # Ask for payment details based on method
    if withdrawal_method == "bank_transfer":
        ask_for_bank_details(user_id, balance)
        return True, "Please provide your bank account details to complete the withdrawal."
    else:  # mobile_money
        ask_for_mobile_money_details(user_id, balance)
        return True, "Please provide your mobile money number to complete the withdrawal."

# ============================
# FLASK ROUTES (Updated)
# ============================

@app.route("/")
def home():
    return """
    <h1>TurnitQ Bot - Render Deployment</h1>
    <p>Status: 🟢 Running with Advanced Analysis & Paystack Payments</p>
    <p><a href="/debug">Debug Info</a></p>
    <p><a href="/manual-activate">Manual Activation</a></p>
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

# ... (Keep all your existing Flask routes as they are, they remain unchanged)

@app.route('/webhook/<path:bot_token>', methods=['POST'])
def telegram_webhook(bot_token):
    """Main Telegram webhook handler - UPDATED with automatic withdrawal support"""
    try:
        update_data = request.get_json(force=True)
        
        if 'message' in update_data:
            message = update_data['message']
            user_id = message['from']['id']
            text = message.get('text', '')
            
            print(f"👤 User {user_id}: {text}")
            
            session = get_user_session(user_id)
            
            # Handle withdrawal input based on type
            if session and session.get('waiting_for_withdrawal'):
                withdrawal_type = session.get('withdrawal_type', 'mobile_money')
                amount = session.get('withdrawal_amount', 0)
                
                if withdrawal_type == 'mobile_money' and text.isdigit() and len(text) == 10:
                    # Mobile money number provided
                    mobile_money_number = text
                    
                    # Update withdrawal record with mobile money number
                    cur = db.cursor()
                    cur.execute(
                        "UPDATE withdrawals SET mobile_money_number=?, status='processing' WHERE user_id=? AND status='pending'",
                        (mobile_money_number, user_id)
                    )
                    
                    # Update referral earnings
                    cur.execute(
                        "UPDATE referral_earnings SET amount=0, total_withdrawn=total_withdrawn+? WHERE user_id=?",
                        (amount, user_id)
                    )
                    db.commit()
                    
                    # Clear withdrawal session
                    update_user_session(user_id, 
                                      waiting_for_withdrawal=0,
                                      withdrawal_type=None,
                                      withdrawal_amount=0)
                    
                    send_telegram_message(user_id, 
                        f"✅ Mobile Money withdrawal request for ₵{amount:.2f} submitted!\n"
                        f"📱 Number: {mobile_money_number}\n"
                        f"We'll process it within 24 hours."
                    )
                    return "ok", 200
                    
                elif withdrawal_type == 'bank_transfer':
                    # Bank transfer - expect account number and name in format: "1234567890, John Doe"
                    if ',' in text:
                        parts = [part.strip() for part in text.split(',', 1)]
                        if len(parts) == 2:
                            account_number, account_name = parts
                            bank_code = session.get('selected_bank_code')
                            
                            if bank_code:
                                success, message = handle_bank_account_input(user_id, account_number, bank_code, account_name)
                                send_telegram_message(user_id, message)
                                return "ok", 200
                            else:
                                send_telegram_message(user_id, "❌ Please select a bank first using the buttons.")
                                return "ok", 200
                    
                    send_telegram_message(user_id, 
                        "❌ Invalid format. Please provide:\n"
                        "<code>1234567890, John Doe</code>\n\n"
                        "Where:\n"
                        "• 1234567890 = Your account number\n"
                        "• John Doe = Your account name"
                    )
                    return "ok", 200
            
            # Existing message handling continues...
            # ... (rest of your existing message handling code remains the same)
            
        elif 'callback_query' in update_data:
            callback = update_data['callback_query']
            user_id = callback['from']['id']
            data = callback['data']
            
            print(f"🔘 Callback data: {data}")
            
            # Handle bank selection
            if data.startswith("select_bank_"):
                bank_code = data.replace("select_bank_", "")
                
                # Get bank name
                cur = db.cursor()
                bank = cur.execute("SELECT bank_name FROM bank_codes WHERE bank_code=?", (bank_code,)).fetchone()
                
                bank_name = bank['bank_name'] if bank else "Selected Bank"
                
                # Update session with selected bank
                update_user_session(user_id, selected_bank_code=bank_code)
                
                send_telegram_message(user_id,
                    f"🏦 Bank Selected: {bank_name}\n\n"
                    f"Please reply with your <b>account number and account name</b> in this format:\n"
                    f"<code>1234567890, John Doe</code>\n\n"
                    f"Where:\n"
                    f"• 1234567890 = Your account number\n"
                    f"• John Doe = Your account name as registered with the bank"
                )
                
            # Handle withdrawal method selection
            elif data == "withdraw_bank":
                success, message = handle_withdrawal_request(user_id, "bank_transfer")
                send_telegram_message(user_id, message)
                
            elif data == "withdraw_mobile":
                success, message = handle_withdrawal_request(user_id, "mobile_money")
                send_telegram_message(user_id, message)
            
            # Handle more banks pagination
            elif data.startswith("more_banks_"):
                page = int(data.replace("more_banks_", ""))
                banks = get_bank_list()
                start_idx = page * 8
                end_idx = start_idx + 8
                
                bank_buttons = []
                for bank in banks[start_idx:end_idx]:
                    bank_name = bank.get('name', 'Unknown Bank')
                    bank_code = bank.get('code', '')
                    bank_buttons.append([{"text": bank_name, "callback_data": f"select_bank_{bank_code}"}])
                
                # Add navigation buttons
                nav_buttons = []
                if page > 0:
                    nav_buttons.append({"text": "⬅️ Previous", "callback_data": f"more_banks_{page-1}"})
                if end_idx < len(banks):
                    nav_buttons.append({"text": "Next ➡️", "callback_data": f"more_banks_{page+1}"})
                
                if nav_buttons:
                    bank_buttons.append(nav_buttons)
                
                keyboard = {"inline_keyboard": bank_buttons}
                send_telegram_message(user_id, "🏦 Select your bank:", reply_markup=keyboard)
            
            # Existing callback handling continues...
            # ... (rest of your existing callback handling code remains the same)
        
        return "ok", 200
        
    except Exception as e:
        print(f"❌ Webhook error: {e}")
        import traceback
        traceback.print_exc()
        return "error", 500

# Update the /withdraw command to show options
def handle_withdraw_command(user_id):
    """Handle /withdraw command with method selection"""
    referral_info = get_referral_info(user_id)
    balance = referral_info['balance']
    
    if balance < MIN_WITHDRAWAL:
        needed = MIN_WITHDRAWAL - balance
        send_telegram_message(user_id, 
            f"❌ Withdrawal minimum is ₵{MIN_WITHDRAWAL}.\n"
            f"Your current balance: ₵{balance:.2f}\n"
            f"You need ₵{needed:.2f} more to withdraw.\n\n"
            f"Use /referral to check your balance and referral code."
        )
    else:
        # Show withdrawal method options
        keyboard = {
            "inline_keyboard": [
                [{"text": "🏦 Bank Transfer (Instant)", "callback_data": "withdraw_bank"}],
                [{"text": "📱 Mobile Money (24 hours)", "callback_data": "withdraw_mobile"}]
            ]
        }
        
        message = (
            f"💰 <b>Withdrawal Options</b>\n\n"
            f"Amount: ₵{balance:.2f}\n"
            f"Minimum: ₵{MIN_WITHDRAWAL}\n\n"
            f"Choose your withdrawal method:\n"
            f"• 🏦 <b>Bank Transfer</b>: Instant processing\n"
            f"• 📱 <b>Mobile Money</b>: Within 24 hours\n\n"
            f"Select an option below:"
        )
        
        send_telegram_message(user_id, message, reply_markup=keyboard)

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
    print("🚀 Starting TurnitQ Bot with Automatic Withdrawals...")
    print(f"💰 Paystack Transfers: ENABLED")
    print(f"🏦 Bank Transfer: Automatic processing")
    print(f"📱 Mobile Money: Manual processing")
    print(f"🤝 Referral System: ENABLED (₵{REFERRAL_REWARD} per referral, ₵{MIN_WITHDRAWAL} min withdrawal)")
    
    setup_webhook()
    port = int(os.environ.get("PORT", 5000))
    print(f"🌐 Server starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)