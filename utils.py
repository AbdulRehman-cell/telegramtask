# utils.py
from datetime import datetime, timedelta
import os


def allowed_file(filename):
    return filename.lower().endswith('.pdf') or filename.lower().endswith('.docx')


def in_cooldown(cooldown_until):
    if not cooldown_until:
        return False, 0
    then = datetime.fromisoformat(cooldown_until)
    now = datetime.utcnow()
    if now < then:
        return True, int((then - now).total_seconds())
    return False, 0


def set_cooldown_seconds(seconds=60):
    return (datetime.utcnow() + timedelta(seconds=seconds)).isoformat()
