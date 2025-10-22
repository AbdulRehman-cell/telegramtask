# db.py
import sqlite3
from datetime import datetime
DB = 'turnitq.db'

def init_db():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        telegram_id INTEGER PRIMARY KEY,
        plan TEXT DEFAULT 'free',
        plan_expiry TEXT,
        used_today INTEGER DEFAULT 0,
        daily_limit INTEGER DEFAULT 1,
        free_used INTEGER DEFAULT 0,
        cooldown_until TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER,
        file_path TEXT,
        status TEXT,
        created_at TEXT,
        result_path TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS reservations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER,
        plan TEXT,
        checks_reserved INTEGER,
        created_at TEXT,
        expires_at TEXT,
        status TEXT
    )''')
    conn.commit()
    conn.close()

# simple user helpers

def get_user(tid):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute('SELECT telegram_id, plan, plan_expiry, used_today, daily_limit, free_used, cooldown_until FROM users WHERE telegram_id=?', (tid,))
    row = c.fetchone()
    conn.close()
    if not row:
        return None
    return dict(telegram_id=row[0], plan=row[1], plan_expiry=row[2], used_today=row[3], daily_limit=row[4], free_used=bool(row[5]), cooldown_until=row[6])

def ensure_user(tid):
    u = get_user(tid)
    if u:
        return u
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute('INSERT INTO users (telegram_id, plan, daily_limit, free_used) VALUES (?,?,?,?)', (tid, 'free', 1, 0))
    conn.commit(); conn.close()
    return get_user(tid)

def increment_used(tid):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute('UPDATE users SET used_today = used_today + 1 WHERE telegram_id=?', (tid,))
    conn.commit(); conn.close()

# job helpers

def create_job(tid, file_path):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute('INSERT INTO jobs (telegram_id, file_path, status, created_at) VALUES (?,?,?,?)', (tid, file_path, 'queued', datetime.utcnow().isoformat()))
    job_id = c.lastrowid
    conn.commit(); conn.close()
    return job_id

def fetch_next_job():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT id, telegram_id, file_path FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1")
    row = c.fetchone()
    if not row:
        conn.close(); return None
    job = {'id': row[0], 'telegram_id': row[1], 'file_path': row[2]}
    c.execute('UPDATE jobs SET status=? WHERE id=?', ('processing', job['id']))
    conn.commit(); conn.close()
    return job

def mark_done(job_id, result_path):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute('UPDATE jobs SET status=?, result_path=? WHERE id=?', ('completed', result_path, job_id))
    conn.commit(); conn.close()

def mark_failed(job_id):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute('UPDATE jobs SET status=? WHERE id=?', ('failed', job_id))
    conn.commit(); conn.close()