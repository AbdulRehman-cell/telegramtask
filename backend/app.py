import os
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8291206067:AAHzmYHr1iHFn1XOo4AfVGwEULRUNZfLvCc")
WEBHOOK_BASE_URL = os.getenv("WEBHOOK_BASE_URL", "")

def send_telegram_message(chat_id, text):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": chat_id, "text": text}
        response = requests.post(url, json=payload)
        return response.json().get("ok", False)
    except:
        return False

@app.route("/")
def home():
    return "🤖 Bot is running!"

@app.route(f"/webhook/{TELEGRAM_BOT_TOKEN}", methods=["POST", "GET"])
def telegram_webhook():
    if request.method == "GET":
        return "✅ Webhook active! Send POST requests.", 200
    
    try:
        data = request.get_json(force=True)
        
        if 'message' in data:
            chat_id = data['message']['chat']['id']
            text = data['message'].get('text', '')
            
            if text == '/start':
                send_telegram_message(chat_id, "✅ Bot is working! Use /check")
            elif text == '/check':
                send_telegram_message(chat_id, "📄 Upload a PDF or DOCX file")
            else:
                send_telegram_message(chat_id, "Try /start or /check")
        
        return "ok", 200
    except:
        return "ok", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)