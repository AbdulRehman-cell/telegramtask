import os
import logging
import sqlite3
import threading
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Dispatcher, CommandHandler, MessageHandler, Filters, CallbackQueryHandler
from dotenv import load_dotenv
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import json
import time
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
import undetected_chromedriver as uc
import tempfile
import asyncio

# Load environment variables
load_dotenv()

app = Flask(__name__)

# Configuration
TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TURNITIN_USERNAME = os.getenv('TURNITIN_USERNAME')
TURNITIN_PASSWORD = os.getenv('TURNITIN_PASSWORD')
PAYSTACK_SECRET_KEY = os.getenv('PAYSTACK_SECRET_KEY')
DATABASE_URL = os.getenv('DATABASE_URL', 'bot_db.sqlite')

# Initialize bot and dispatcher
bot = Bot(token=TELEGRAM_TOKEN)
dispatcher = Dispatcher(bot, None, use_context=True)

# Global variables
DAILY_LIMIT = 50
current_daily_usage = 0
processing_lock = threading.Lock()
user_cooldown = {}

# Database setup
def init_db():
    conn = sqlite3.connect(DATABASE_URL)
    cursor = conn.cursor()
    
    # Users table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            plan TEXT DEFAULT 'free',
            subscription_end DATE,
            daily_checks_used INTEGER DEFAULT 0,
            total_checks INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Checks table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            filename TEXT,
            similarity_score REAL,
            ai_score REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (user_id)
        )
    ''')
    
    # Payments table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            plan TEXT,
            amount REAL,
            paystack_reference TEXT,
            status TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (user_id)
        )
    ''')
    
    conn.commit()
    conn.close()

init_db()

# Database helper functions
def get_user(user_id):
    conn = sqlite3.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
    user = cursor.fetchone()
    conn.close()
    
    if user:
        return {
            'user_id': user[0],
            'username': user[1],
            'first_name': user[2],
            'last_name': user[3],
            'plan': user[4],
            'subscription_end': user[5],
            'daily_checks_used': user[6],
            'total_checks': user[7],
            'created_at': user[8]
        }
    return None

def create_user(user_id, username, first_name, last_name):
    conn = sqlite3.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR IGNORE INTO users (user_id, username, first_name, last_name, plan)
        VALUES (?, ?, ?, ?, 'free')
    ''', (user_id, username, first_name, last_name))
    conn.commit()
    conn.close()

def update_user_plan(user_id, plan):
    conn = sqlite3.connect(DATABASE_URL)
    cursor = conn.cursor()
    subscription_end = datetime.now() + timedelta(days=28)
    cursor.execute('''
        UPDATE users 
        SET plan = ?, subscription_end = ?, daily_checks_used = 0
        WHERE user_id = ?
    ''', (plan, subscription_end.date(), user_id))
    conn.commit()
    conn.close()

def increment_daily_checks(user_id):
    conn = sqlite3.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE users 
        SET daily_checks_used = daily_checks_used + 1, total_checks = total_checks + 1
        WHERE user_id = ?
    ''', (user_id,))
    conn.commit()
    conn.close()

def get_total_daily_checks():
    conn = sqlite3.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute('SELECT SUM(daily_checks_used) FROM users')
    total = cursor.fetchone()[0] or 0
    conn.close()
    return total

def can_user_check(user_id):
    user = get_user(user_id)
    if not user:
        return False, "User not found"
    
    # Check subscription
    if user['plan'] == 'free':
        if user['total_checks'] > 0:
            return False, "⚠️ You've already used your free check.\nSubscribe to continue using TurnitQ."
        return True, ""
    
    # Check if subscription expired
    if user['subscription_end'] and datetime.now().date() > datetime.strptime(user['subscription_end'], '%Y-%m-%d').date():
        return False, "⏰ Your 28-day subscription has expired.\nRenew anytime to continue using TurnitQ."
    
    # Check daily limit based on plan
    plan_limits = {'premium': 5, 'pro': 30, 'elite': 100}
    if user['plan'] in plan_limits and user['daily_checks_used'] >= plan_limits[user['plan']]:
        return False, f"⚠️ You've used all your daily checks for {user['plan'].title()} plan.\nTry again tomorrow."
    
    # Check global daily limit
    if get_total_daily_checks() >= DAILY_LIMIT:
        return False, "🚫 We've reached today's maximum checks. Please try again after midnight."
    
    return True, ""

# Scheduler for resetting daily counters
def reset_daily_usage():
    conn = sqlite3.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute('UPDATE users SET daily_checks_used = 0')
    conn.commit()
    conn.close()
    logging.info("Daily usage counters reset")

scheduler = BackgroundScheduler()
scheduler.add_job(reset_daily_usage, 'cron', hour=0, minute=0)
scheduler.start()

# Turnitin Automation
class TurnitinAutomation:
    def __init__(self):
        self.driver = None
        
    def setup_driver(self):
        chrome_options = Options()
        chrome_options.add_argument('--headless')
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        self.driver = uc.Chrome(options=chrome_options)
        
    def login_to_turnitin(self):
        try:
            self.driver.get("https://www.turnitin.com/login_page.asp")
            
            # Wait for login page to load
            WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.NAME, "email"))
            )
            
            # Enter credentials
            email_field = self.driver.find_element(By.NAME, "email")
            password_field = self.driver.find_element(By.NAME, "password")
            
            email_field.send_keys(TURNITIN_USERNAME)
            password_field.send_keys(TURNITIN_PASSWORD)
            
            # Click login button
            login_button = self.driver.find_element(By.XPATH, "//button[contains(text(), 'Log In')]")
            login_button.click()
            
            # Wait for dashboard to load
            WebDriverWait(self.driver, 30).until(
                EC.presence_of_element_located((By.CLASS_NAME, "dashboard"))
            )
            
            return True
        except Exception as e:
            logging.error(f"Login failed: {str(e)}")
            return False
    
    def submit_document(self, file_path, options):
        try:
            # Navigate to submission page
            self.driver.get("https://www.turnitin.com/newreport_classic.asp")
            
            # Upload file
            file_input = self.driver.find_element(By.NAME, "uploaded_file")
            file_input.send_keys(file_path)
            
            # Set options (simplified - actual implementation would need to map options to Turnitin settings)
            # This is a placeholder for the actual option setting logic
            
            # Submit
            submit_button = self.driver.find_element(By.NAME, "submit_button")
            submit_button.click()
            
            # Wait for processing
            WebDriverWait(self.driver, 300).until(  # 5 minute timeout
                EC.presence_of_element_located((By.CLASS_NAME, "similarity-score"))
            )
            
            # Get results
            similarity_score = self.driver.find_element(By.CLASS_NAME, "similarity-score").text
            ai_score = self.driver.find_element(By.CLASS_NAME, "ai-score").text
            
            # Download reports (simplified - would need actual download logic)
            return {
                'similarity_score': similarity_score,
                'ai_score': ai_score,
                'report_path': file_path  # Placeholder
            }
            
        except Exception as e:
            logging.error(f"Document submission failed: {str(e)}")
            return None
    
    def close(self):
        if self.driver:
            self.driver.quit()

# Telegram Bot Handlers
def start(update, context):
    user = update.effective_user
    create_user(user.id, user.username, user.first_name, user.last_name)
    
    welcome_text = """👋 Welcome to TurnitQ!
Upload your document to check its originality instantly.
Use /check to begin."""
    
    update.message.reply_text(welcome_text)

def check_command(update, context):
    user_id = update.effective_user.id
    
    # Check cooldown
    if user_id in user_cooldown and time.time() - user_cooldown[user_id] < 60:
        update.message.reply_text("⏳ Please wait 1 minute before submitting another document.")
        return
    
    can_check, message = can_user_check(user_id)
    if not can_check:
        keyboard = [[InlineKeyboardButton("💎 Upgrade Plan", callback_data="upgrade")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        update.message.reply_text(message, reply_markup=reply_markup)
        return
    
    update.message.reply_text("""📄 Please upload your document (.docx or .pdf).
Only one file can be processed at a time.""")

def handle_document(update, context):
    user_id = update.effective_user.id
    document = update.message.document
    
    # Check file type
    if document.mime_type not in ['application/pdf', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document']:
        update.message.reply_text("⚠️ Please upload only .pdf or .docx files.")
        return
    
    # Check if user can check
    can_check, message = can_user_check(user_id)
    if not can_check:
        keyboard = [[InlineKeyboardButton("💎 Upgrade Plan", callback_data="upgrade")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        update.message.reply_text(message, reply_markup=reply_markup)
        return
    
    update.message.reply_text("✅ File received. Checking with Turnitin — please wait a few seconds…")
    
    # Ask for report options
    options_text = """Before generating your report, please choose what to include:
1️⃣ Exclude bibliography — Yes / No  
2️⃣ Exclude quoted text — Yes / No  
3️⃣ Exclude cited text — Yes / No
4️⃣ Exclude small matches — Yes / No

Please reply with your choices (e.g. Yes, No, Yes, Yes)"""
    
    update.message.reply_text(options_text)
    
    # Store file info in context for later processing
    context.user_data['pending_file'] = {
        'file_id': document.file_id,
        'file_name': document.file_name
    }

def handle_options(update, context):
    if 'pending_file' not in context.user_data:
        update.message.reply_text("❌ No file pending. Please use /check first.")
        return
    
    options_text = update.message.text
    # Parse options (simplified)
    options = [opt.strip().lower() for opt in options_text.split(',')]
    
    if len(options) != 4:
        update.message.reply_text("❌ Please provide exactly 4 options separated by commas.")
        return
    
    update.message.reply_text("⏳ Generating your Turnitin report with your selected preferences...")
    
    # Process the file in background
    threading.Thread(target=process_document, args=(update, context, options)).start()

def process_document(update, context, options):
    user_id = update.effective_user.id
    file_info = context.user_data['pending_file']
    
    try:
        # Download file
        file = context.bot.get_file(file_info['file_id'])
        with tempfile.NamedTemporaryFile(delete=False, suffix='.pdf') as tmp_file:
            file.download(custom_path=tmp_file.name)
            
            # Initialize Turnitin automation
            turnitin = TurnitinAutomation()
            turnitin.setup_driver()
            
            if turnitin.login_to_turnitin():
                result = turnitin.submit_document(tmp_file.name, options)
                turnitin.close()
                
                if result:
                    # Update user checks
                    increment_daily_checks(user_id)
                    user_cooldown[user_id] = time.time()
                    
                    # Send results
                    result_text = f"""✅ Report ready!

📊 Similarity Score: {result['similarity_score']}
🤖 AI Detection Score: {result['ai_score']}

Your report has been generated successfully."""
                    
                    update.message.reply_text(result_text)
                    
                    # Check if this was free check and offer upgrade
                    user = get_user(user_id)
                    if user['plan'] == 'free' and user['total_checks'] >= 1:
                        offer_upgrade(update)
                    
                    # Clean up
                    del context.user_data['pending_file']
                    return
        
        # If we get here, something failed
        update.message.reply_text("❌ Failed to process document. Please try again later.")
        
    except Exception as e:
        logging.error(f"Error processing document: {str(e)}")
        update.message.reply_text("❌ An error occurred while processing your document.")

def offer_upgrade(update):
    upgrade_text = """🎁 Your first check was free!
To unlock more checks and full reports for the next 28 days, upgrade below 👇"""
    
    keyboard = [[InlineKeyboardButton("💎 Upgrade Plan", callback_data="upgrade")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    update.message.reply_text(upgrade_text, reply_markup=reply_markup)

def upgrade_command(update, context):
    show_upgrade_plans(update)

def show_upgrade_plans(update):
    plans_text = """🔓 Unlock More with TurnitQ Premium Plans
Your first check was free — now take your writing game to the next level.
Choose the plan that fits your workload 👇

⚡ Premium — $8/month
✔ Up to 3 checks per day
✔ Full similarity report
✔ Faster results

🚀 Pro — $29/month
✔ Up to 20 checks per day
✔ Full similarity report
✔ Faster results
✔ AI-generated report
✔ View full matching sources

👑 Elite — $79/month
✔ Up to 70 checks per day
✔ Priority processing
✔ Full similarity report
✔ AI-generated report"""
    
    keyboard = [
        [InlineKeyboardButton("Upgrade to Premium — $8", callback_data="plan_premium")],
        [InlineKeyboardButton("Go Pro — $29", callback_data="plan_pro")],
        [InlineKeyboardButton("Go Elite — $79", callback_data="plan_elite")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    update.message.reply_text(plans_text, reply_markup=reply_markup)

def handle_callback(update, context):
    query = update.callback_query
    query.answer()
    
    if query.data == "upgrade":
        show_upgrade_plans(query)
    elif query.data.startswith("plan_"):
        plan = query.data.split("_")[1]
        handle_plan_selection(query, plan)

def handle_plan_selection(update, plan):
    plan_prices = {'premium': 8, 'pro': 29, 'elite': 79}
    plan_limits = {'premium': 5, 'pro': 30, 'elite': 100}
    
    # Check capacity
    current_usage = get_total_daily_checks()
    if current_usage + plan_limits[plan] > DAILY_LIMIT:
        update.message.reply_text("🚫 Sorry, that plan is full right now. Please try a smaller plan or check back later.")
        return
    
    price = plan_prices[plan]
    
    # Create payment (simplified - would integrate with Paystack)
    payment_text = f"""💳 Processing Payment — {plan.title()} (${price})
Tap Pay below to complete the transaction."""
    
    keyboard = [[InlineKeyboardButton("Pay", callback_data=f"pay_{plan}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    update.message.reply_text(payment_text, reply_markup=reply_markup)

def id_command(update, context):
    user_id = update.effective_user.id
    user = get_user(user_id)
    
    if not user:
        update.message.reply_text("❌ User not found.")
        return
    
    plan_display = user['plan'].title() if user['plan'] != 'free' else 'Free'
    checks_used = user['daily_checks_used']
    
    if user['plan'] == 'free':
        checks_text = "1 free check used" if user['total_checks'] > 0 else "1 free check available"
    else:
        plan_limits = {'premium': 5, 'pro': 30, 'elite': 100}
        daily_limit = plan_limits.get(user['plan'], 0)
        checks_text = f"{checks_used}/{daily_limit} checks used today"
    
    expiry_text = user['subscription_end'] if user['subscription_end'] else "N/A"
    
    info_text = f"""👤 Your Account Info:
User ID: {user_id}
Plan: {plan_display}
Daily Checks: {checks_text}
Subscription ends: {expiry_text}"""
    
    update.message.reply_text(info_text)

def cancel_command(update, context):
    if 'pending_file' in context.user_data:
        del context.user_data['pending_file']
    update.message.reply_text("❌ Your check has been cancelled.")

def handle_invalid(update, context):
    update.message.reply_text("""⚠️ Please use one of the available commands:
/check • /cancel • /upgrade • /id""")

# Register handlers
dispatcher.add_handler(CommandHandler("start", start))
dispatcher.add_handler(CommandHandler("check", check_command))
dispatcher.add_handler(CommandHandler("upgrade", upgrade_command))
dispatcher.add_handler(CommandHandler("id", id_command))
dispatcher.add_handler(CommandHandler("cancel", cancel_command))
dispatcher.add_handler(MessageHandler(Filters.document, handle_document))
dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_options))
dispatcher.add_handler(CallbackQueryHandler(handle_callback))
dispatcher.add_handler(MessageHandler(Filters.all, handle_invalid))

# Flask routes
@app.route('/webhook', methods=['POST'])
def webhook():
    update = Update.de_json(request.get_json(force=True), bot)
    dispatcher.process_update(update)
    return 'OK'

@app.route('/paystack-webhook', methods=['POST'])
def paystack_webhook():
    # Paystack webhook handler (simplified)
    data = request.get_json()
    
    if data and data.get('event') == 'charge.success':
        # Process successful payment
        reference = data['data']['reference']
        # Verify and activate plan
        # This would involve checking the payment and updating the user's plan
        
        return jsonify({'status': 'success'})
    
    return jsonify({'status': 'ignored'})

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({'status': 'healthy', 'timestamp': datetime.now().isoformat()})

@app.route('/set_webhook', methods=['GET'])
def set_webhook():
    webhook_url = f"{os.getenv('WEBHOOK_BASE_URL')}/webhook"
    result = bot.set_webhook(webhook_url)
    return jsonify({'success': result, 'webhook_url': webhook_url})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)