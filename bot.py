import discord
from discord import app_commands
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from flask_cors import CORS
from threading import Thread
from pytz import timezone
import json
import os
import requests
import base64
import asyncio

# ================================
#  設定
# ================================
GITHUB_REPO    = os.getenv("GITHUB_REPO")       # 例: "yourname/yourrepo"
GITHUB_TOKEN   = os.getenv("GITHUB_TOKEN")
TOKEN          = os.getenv("TOKEN")
SUBJECT_CATEGORY_ID = os.getenv("SUBJECT_CATEGORY_ID")  # カテゴリID（優先）
SUBJECT_CATEGORY    = os.getenv("SUBJECT_CATEGORY")     # カテゴリ名（IDがない場合に使用）
JST = timezone("Asia/Tokyo")

scheduler = AsyncIOScheduler(timezone=JST)

# ================================
#  Flask アプリ
# ================================
app = Flask("")
CORS(app)

@app.route("/")
def home():
    return "I'm alive"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

def keep_alive():
    t = Thread(target=run_flask)
    t.daemon = True
    t.start()

# ================================
#  スケジューラー & 起動
# ================================
scheduler.add_job(send_tomorrow_plans, "cron", hour=20, minute=0)
scheduler.add_job(send_today_plans,    "cron", hour=5,  minute=30)
scheduler.add_job(cleanup_past_plans,  "cron", hour=0,  minute=0)

started = False

@bot.event
async def on_ready():
    global started
    print(f"Bot is ready! {bot.user}")
    await bot.tree.sync()
    if not started:
        scheduler.start()
        started = True
        print("Scheduler started!")

keep_alive()

import time

# 429レート制限時は待機してから終了（Renderが自動再起動する）
try:
    bot.run(TOKEN)
except discord.errors.HTTPException as e:
    if e.status == 429:
        retry_after = e.response.headers.get('Retry-After', '120')
        wait = max(int(float(retry_after)), 60)
        print(f"[WARNING] Discord rate limited (429). Waiting {wait}s before exit...")
        time.sleep(wait)
        raise SystemExit(1)
    raise
