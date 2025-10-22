# TurnitQ — FastAPI Telegram Bot (Render-ready)

## Overview
This scaffold accepts document uploads from Telegram and queues them for processing. It includes:
- Telegram bot for receiving files and commands
- FastAPI backend (health, webhook placeholder)
- SQLite DB helpers for users, jobs, and usage
- Scheduler for daily resets and reservation sweeping

## Setup (local)
1. Copy `.env.example` to `.env` and fill values.
2. Install requirements: `pip install -r requirements.txt`.
3. Run backend: `uvicorn backend.app:app --reload`.
4. Run bot (in another terminal): `python bot.py`.

## Deploy (Render)
1. Create a new Web Service on Render, link your GitHub repo.
2. Add environment variables from `.env` to Render settings.
3. Use the `web` command from the `Procfile`.
4. Create a  Background Worker for `bot` using the `bot` Procfile line.

## Replacing processing
Replace `backend/tasks.py` placeholder with your approved Turnitin API or manual process. Do not add automation that violates the Turnitin terms.