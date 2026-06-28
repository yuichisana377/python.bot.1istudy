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
import time

# ================================
#  設定
# ================================
GITHUB_REPO          = os.getenv("GITHUB_REPO")
GITHUB_TOKEN         = os.getenv("GITHUB_TOKEN")
TOKEN                = os.getenv("TOKEN")
SUBJECT_CATEGORY_ID  = os.getenv("SUBJECT_CATEGORY_ID")  # カテゴリID（優先）
SUBJECT_CATEGORY     = os.getenv("SUBJECT_CATEGORY")     # カテゴリ名（フォールバック）
JST = timezone("Asia/Tokyo")

scheduler = AsyncIOScheduler(timezone=JST)

# ================================
#  Flask アプリ
# ================================
from flask import Flask, request, jsonify, make_response
from flask_cors import CORS

app = Flask("")
CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=False)

# preflightリクエストを全ルートで明示許可
@app.before_request
def handle_preflight():
    if request.method == "OPTIONS":
        res = make_response()
        res.headers["Access-Control-Allow-Origin"]  = "*"
        res.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        res.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        return res, 200

@app.route("/")
def home():
    return "I'm alive"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    # use_reloader=False が必須（Threadと競合するため）
    app.run(host="0.0.0.0", port=port, use_reloader=False, threaded=True)

def keep_alive():
    t = Thread(target=run_flask, daemon=True)
    t.start()
    print("[INFO] Flask thread started")

# ================================
#  Discord Bot
# ================================
intents = discord.Intents.default()
intents.message_content = True
intents.presences = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ================================
#  GitHub ユーティリティ
# ================================
def github_get(filename):
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}"}
    r = requests.get(url, headers=headers)
    if r.status_code == 404:
        return None, None
    data = r.json()
    content = base64.b64decode(data["content"]).decode()
    return json.loads(content), data["sha"]

def github_put(filename, content_obj, sha=None):
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}"}
    encoded = base64.b64encode(
        json.dumps(content_obj, ensure_ascii=False, indent=2).encode()
    ).decode()
    payload = {"message": f"update {filename}", "content": encoded}
    if sha:
        payload["sha"] = sha
    requests.put(url, headers=headers, json=payload)

# GitHub APIを別スレッドで実行するasyncラッパー
async def async_github_get(filename):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, github_get, filename)

async def async_github_put(filename, content_obj, sha=None):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, github_put, filename, content_obj, sha)

# ================================
#  設定ファイル
# ================================
def load_config(guild_id: int):
    data, _ = github_get(f"config_{guild_id}.json")
    return data or {}

def save_config(guild_id: int, data: dict):
    _, sha = github_get(f"config_{guild_id}.json")
    github_put(f"config_{guild_id}.json", data, sha)

async def async_load_config(guild_id: int):
    data, _ = await async_github_get(f"config_{guild_id}.json")
    return data or {}

async def async_save_config(guild_id: int, data: dict):
    _, sha = await async_github_get(f"config_{guild_id}.json")
    await async_github_put(f"config_{guild_id}.json", data, sha)

def list_all_configs():
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents"
    headers = {"Authorization": f"token {GITHUB_TOKEN}"}
    r = requests.get(url, headers=headers)
    files = r.json()
    return [
        f["name"] for f in files
        if isinstance(f, dict)
        and f["name"].startswith("config_")
        and f["name"].endswith(".json")
    ]

# ================================
#  予定データ
# ================================
def load_plans(guild_id: int):
    data, _ = github_get(f"plans_{guild_id}.json")
    return data or []

def save_plans(guild_id: int, plans: list):
    _, sha = github_get(f"plans_{guild_id}.json")
    github_put(f"plans_{guild_id}.json", plans, sha)

async def async_load_plans(guild_id: int):
    data, _ = await async_github_get(f"plans_{guild_id}.json")
    return data or []

async def async_save_plans(guild_id: int, plans: list):
    _, sha = await async_github_get(f"plans_{guild_id}.json")
    await async_github_put(f"plans_{guild_id}.json", plans, sha)

# ================================
#  ログ
# ================================
def write_log(guild_id: int, log_type: str, detail: str):
    filename = f"logs_{guild_id}.json"
    logs, sha = github_get(filename)
    logs = logs or []

    now_jst = datetime.now(JST)
    now_str = now_jst.strftime("%Y-%m-%d %H:%M:%S")

    logs = [
        log for log in logs
        if (now_jst - datetime.strptime(log["time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST)).days <= 30
    ]

    logs.append({"time": now_str, "type": log_type, "detail": detail})
    github_put(filename, logs, sha)
# 非同期版 write_log（async_write_log）
async def async_write_log(guild_id: int, log_type: str, detail: str):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, write_log, guild_id, log_type, detail)

# ================================
#  科目チャンネルユーティリティ
# ================================
def get_subject_channels(guild: discord.Guild) -> list:
    if SUBJECT_CATEGORY_ID:
        for cat in guild.categories:
            if cat.id == int(SUBJECT_CATEGORY_ID):
                return list(cat.text_channels)
    if SUBJECT_CATEGORY:
        for cat in guild.categories:
            if cat.name == SUBJECT_CATEGORY:
                return list(cat.text_channels)
    return list(guild.text_channels)

def get_subject_channel_by_name(guild: discord.Guild, name: str):
    for ch in get_subject_channels(guild):
        if ch.name == name:
            return ch
    return None

# ================================
#  日付パース
# ================================
def parse_date(date: str):
    try:
        if "-" in date and len(date.split("-")[0]) == 4:
            parsed = datetime.strptime(date, "%Y-%m-%d")
        else:
            date = date.replace("/", "-")
            m, d = date.split("-")
            y = datetime.now().year
            parsed = datetime.strptime(f"{y}-{int(m):02d}-{int(d):02d}", "%Y-%m-%d")
        return parsed.strftime("%Y-%m-%d")
    except Exception:
        return None

# ================================
#  add 内部関数
# ================================
async def add_plan_internal(guild_id: int, subject: str, date: str, category: str, content: str):
    date_str = parse_date(date)
    if not date_str:
        return False, "日付の形式が正しくありません！"

    today = datetime.now(JST).date()
    if datetime.strptime(date_str, "%Y-%m-%d").date() < today:
        return False, "過去の日付は登録できません！"

    tagged_content = f"【{category}】{content}"
    plans = load_plans(guild_id)
    plans.append({"date": date_str, "subject": subject, "content": tagged_content})
    save_plans(guild_id, plans)
    write_log(guild_id, "add", detail=f"{date_str} / {subject} / {tagged_content}")

    return True, f"登録しました！\n{date_str} / {subject} / {tagged_content}"

# ================================
#  /add
# ================================
@bot.tree.command(name="add", description="予定を追加する")
@app_commands.describe(
    date="日付（例: 6-20, 2026-06-20）",
    subject="科目（省略するとこのチャンネル名を使用）",
    category="分類（宿題・提出・持ち物など）",
    content="内容"
)
async def add_plan(interaction: discord.Interaction, date: str, category: str, content: str, subject: str = None):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    if not subject:
        subject = interaction.channel.name
    ok, msg = await add_plan_internal(guild.id, subject, date, category, content)
    if ok:
        target_channel = get_subject_channel_by_name(guild, subject)
        await (target_channel or interaction.channel).send(msg)
    else:
        await interaction.followup.send(msg, ephemeral=True)
        return
    await interaction.followup.send("完了しました！", ephemeral=True)

@add_plan.autocomplete("subject")
async def add_subject_autocomplete(interaction: discord.Interaction, current: str):
    channels = get_subject_channels(interaction.guild)
    return [
        app_commands.Choice(name=ch.name, value=ch.name)
        for ch in channels if current.lower() in ch.name.lower()
    ][:25]

@add_plan.autocomplete("category")
async def add_category_autocomplete(interaction: discord.Interaction, current: str):
    candidates = ["宿題", "提出", "持ち物", "テスト", "その他"]
    return [app_commands.Choice(name=c, value=c) for c in candidates if current in c][:25]

# ================================
#  /list
# ================================
@bot.tree.command(name="list", description="予定一覧を表示する")
@app_commands.describe(date="all または 日付（例: 6/15, 2026-06-15）")
async def list_plans(interaction: discord.Interaction, date: str):
    await interaction.response.defer(ephemeral=True)
    guild_id = interaction.guild.id
    plans = await async_load_plans(guild_id)

    if date.lower() == "all":
        if not plans:
            await interaction.followup.send("予定はありません。", ephemeral=True)
            return
        sorted_plans = sorted(plans, key=lambda p: p["date"])
        msg = "📘 **すべての予定一覧**\n"
        for p in sorted_plans:
            msg += f"- {p['date']}：{p['subject']} {p['content']}\n"
        await interaction.followup.send(msg, ephemeral=True)
        return

    date_str = parse_date(date)
    if not date_str:
        await interaction.followup.send("日付の形式が正しくありません！", ephemeral=True)
        return

    selected = [p for p in plans if p["date"] == date_str]
    if not selected:
        await interaction.followup.send(f"{date} の予定はありません。", ephemeral=True)
        return

    msg = f"📘 **{date_str} の予定**\n"
    for p in selected:
        msg += f"- {p['subject']} {p['content']}\n"
    await interaction.followup.send(msg, ephemeral=True)

# ================================
#  /delete
# ================================
@bot.tree.command(name="delete", description="予定を削除する")
@app_commands.describe(target="削除したい予定")
async def delete_plan(interaction: discord.Interaction, target: str):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    plans = await async_load_plans(guild.id)

    deleted = None
    new_plans = []
    for p in plans:
        label = f"{p['date']}/{p['subject']}{p['content']}"
        if label == target:
            deleted = p
        else:
            new_plans.append(p)

    if not deleted:
        await interaction.followup.send("その予定は見つかりませんでした。", ephemeral=True)
        return

    save_plans(guild.id, new_plans)
    write_log(guild.id, "delete", detail=f"{deleted['date']} / {deleted['subject']} / {deleted['content']}")

    msg = f"削除しました！\n{target}"
    target_channel = get_subject_channel_by_name(guild, deleted["subject"])
    await (target_channel or interaction.channel).send(msg)
    await interaction.followup.send("完了しました！", ephemeral=True)

@delete_plan.autocomplete("target")
async def delete_autocomplete(interaction: discord.Interaction, current: str):
    plans = load_plans(interaction.guild.id)
    choices = []
    for p in plans:
        label = f"{p['date']}/{p['subject']}{p['content']}"
        if current in label:
            choices.append(app_commands.Choice(name=label, value=label))
    return choices[:25]

# ================================
#  /edit
# ================================
@bot.tree.command(name="edit", description="予定を編集する")
@app_commands.describe(
    target="編集したい予定",
    date="新しい日付",
    subject="新しい科目",
    category="新しい分類",
    content="新しい内容"
)
async def edit_plan(interaction: discord.Interaction, target: str, date: str = None, subject: str = None, category: str = None, content: str = None):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    plans = await async_load_plans(guild.id)

    found = None
    for p in plans:
        label = f"{p['date']}/{p['subject']}{p['content']}"
        if label == target:
            found = p
            break

    if not found:
        await interaction.followup.send("その予定が見つかりませんでした。", ephemeral=True)
        return

    before_str = f"{found['date']} / {found['subject']} / {found['content']}"

    if date:
        date_str = parse_date(date)
        if not date_str:
            await interaction.followup.send("日付の形式が正しくありません！", ephemeral=True)
            return
        found["date"] = date_str

    if subject:
        found["subject"] = subject

    if category and content:
        found["content"] = f"【{category}】{content}"
    elif category:
        body = found["content"].split("】", 1)[1] if "】" in found["content"] else found["content"]
        found["content"] = f"【{category}】{body}"
    elif content:
        tag = found["content"].split("】", 1)[0] + "】" if "】" in found["content"] else ""
        found["content"] = f"{tag}{content}"

    await async_save_plans(guild.id, plans)
    after_str = f"{found['date']} / {found['subject']} / {found['content']}"
    await async_write_log(guild.id, "edit", detail=f"{before_str} → {after_str}")

    msg = f"編集しました！\n\n【編集前】\n{before_str}\n\n【編集後】\n{after_str}"
    target_channel = get_subject_channel_by_name(guild, found["subject"])
    await (target_channel or interaction.channel).send(msg)
    await interaction.followup.send("完了しました！", ephemeral=True)

@edit_plan.autocomplete("target")
async def edit_target_autocomplete(interaction: discord.Interaction, current: str):
    plans = load_plans(interaction.guild.id)
    choices = []
    for p in plans:
        label = f"{p['date']}/{p['subject']}{p['content']}"
        if current in label:
            choices.append(app_commands.Choice(name=label, value=label))
    return choices[:25]

@edit_plan.autocomplete("subject")
async def edit_subject_autocomplete(interaction: discord.Interaction, current: str):
    channels = get_subject_channels(interaction.guild)
    return [
        app_commands.Choice(name=ch.name, value=ch.name)
        for ch in channels if current.lower() in ch.name.lower()
    ][:25]

@edit_plan.autocomplete("category")
async def edit_category_autocomplete(interaction: discord.Interaction, current: str):
    candidates = ["宿題", "提出", "持ち物", "テスト", "その他"]
    return [app_commands.Choice(name=c, value=c) for c in candidates if current in c][:25]

# ================================
#  /cleanup
# ================================
@bot.tree.command(name="cleanup", description="過去の予定を削除する")
async def cleanup_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild_id = interaction.guild.id
    plans = await async_load_plans(guild_id)

    today = datetime.now(JST).date()
    threshold = today - timedelta(days=7)  # ★ 1週間前

    deleted_dates = sorted({p["date"] for p in plans if datetime.strptime(p["date"], "%Y-%m-%d").date() < threshold})
    new_plans = [p for p in plans if datetime.strptime(p["date"], "%Y-%m-%d").date() >= threshold]

    await async_save_plans(guild_id, new_plans)

    if deleted_dates:
        threshold = today - timedelta(days=7)
deleted_dates = sorted({
    p["date"] for p in plans
    if datetime.strptime(p["date"], "%Y-%m-%d").date() < threshold
})

await async_write_log(
    guild_id,
    "cleanup",
    detail="削除した日付: " + ", ".join(deleted_dates)
)

        await interaction.followup.send(
            f"🧹 {len(deleted_dates)}件削除しました！\n" + "\n".join(deleted_dates),
            ephemeral=True
        )
    else:
        await interaction.followup.send("削除する予定はありませんでした！", ephemeral=True)

# ================================
#  /setchannel
# ================================
@bot.tree.command(name="setchannel", description="通知チャンネルを設定する")
async def setchannel(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild_id = interaction.guild.id
    config = await async_load_config(guild_id)
    config["remind_channel_id"] = interaction.channel.id
    await async_save_config(guild_id, config)
    await interaction.followup.send(
        f"通知チャンネルを **#{interaction.channel.name}** に設定しました！"
    )

# ================================
#  /help
# ================================
@bot.tree.command(name="help", description="使えるコマンド一覧")
async def help_command(interaction: discord.Interaction):
    msg = (
        "📘 **使えるコマンド一覧**\n\n"
        "**/add** — 予定を登録する\n"
        "**/list** — 予定を表示する\n"
        "**/delete** — 予定を削除する\n"
        "**/edit** — 予定を編集する\n"
        "**/cleanup** — 過去の予定を削除する\n"
        "**/setchannel** — 通知チャンネルを設定する\n"
    )
    await interaction.response.send_message(msg, ephemeral=True)  # helpは即応答OK

# ================================
#  自動通知
# ================================
async def send_tomorrow_plans():
    for filename in list_all_configs():
        guild_id = int(filename.replace("config_", "").replace(".json", ""))
        config = load_config(guild_id)
        channel_id = config.get("remind_channel_id")
        if not channel_id:
            continue
        channel = bot.get_channel(channel_id)
        if not channel:
            continue
        tomorrow = (datetime.now(JST) + timedelta(days=1)).strftime("%Y-%m-%d")
        plans = [p for p in load_plans(guild_id) if p["date"] == tomorrow]
        if plans:
            msg = "こんばんは！明日の予定です。\n"
            for p in plans:
                msg += f"・{p['subject']} {p['content']}\n"
        else:
            msg = "こんばんは！明日の予定はありません。\n"
        await channel.send(msg + "@everyone")

async def send_today_plans():
    for filename in list_all_configs():
        guild_id = int(filename.replace("config_", "").replace(".json", ""))
        config = load_config(guild_id)
        channel_id = config.get("remind_channel_id")
        if not channel_id:
            continue
        channel = bot.get_channel(channel_id)
        if not channel:
            continue
        today = datetime.now(JST).strftime("%Y-%m-%d")
        plans = [p for p in load_plans(guild_id) if p["date"] == today]
        if plans:
            msg = "おはようございます！今日の予定です。\n"
            for p in plans:
                msg += f"・{p['subject']} {p['content']}\n"
        else:
            msg = "おはようございます！今日の予定はありません。\n"
        await channel.send(msg + "@everyone")

async def cleanup_past_plans():
    today = datetime.now(JST).date()
    threshold = today - timedelta(days=7)

    for filename in list_all_configs():
        guild_id = int(filename.replace("config_", "").replace(".json", ""))
        plans = load_plans(guild_id)

        # 削除対象を計算
        deleted_dates = sorted({
            p["date"] for p in plans
            if datetime.strptime(p["date"], "%Y-%m-%d").date() < threshold
        })

        # 新しい予定リスト
        new_plans = [
            p for p in plans
            if datetime.strptime(p["date"], "%Y-%m-%d").date() >= threshold
        ]

        # 保存
        save_plans(guild_id, new_plans)

        # ★ 保存後に plans を読み直す（これが重要）
        plans = load_plans(guild_id)

        # ★ 再計算（ログ用）
        deleted_dates = sorted({
            p["date"] for p in plans
            if datetime.strptime(p["date"], "%Y-%m-%d").date() < threshold
        })

        if deleted_dates:
            write_log(
                guild_id,
                "cleanup",
                detail="削除した日付: " + ", ".join(deleted_dates)
            )
            print(f"{guild_id} の過去予定を削除しました。")


# ================================
#  Flask API（WebUI用）
# ================================
@app.route("/channels", methods=["GET"])
def get_channels():
    guild_id = request.args.get("guild_id")
    if not guild_id:
        return jsonify({"ok": False, "error": "missing guild_id"})
    guild = bot.get_guild(int(guild_id))
    if not guild:
        return jsonify({"ok": False, "error": "guild not found"})
    channels = [{"id": str(ch.id), "name": ch.name} for ch in get_subject_channels(guild)]
    return jsonify({"ok": True, "channels": channels})

@app.route("/add_schedule", methods=["POST"])
def add_schedule():
    data = request.json
    guild_id = data.get("guild_id")
    date     = data.get("date")
    subject  = data.get("subject")
    category = data.get("category")
    content  = data.get("content")

    if not all([guild_id, date, subject, category, content]):
        return jsonify({"ok": False, "error": "missing fields"})

    guild = bot.get_guild(int(guild_id))
    future = asyncio.run_coroutine_threadsafe(
        add_plan_internal(int(guild_id), subject, date, category, content),
        bot.loop
    )
    ok, msg = future.result(timeout=30)

    if ok and guild:
        target_channel = get_subject_channel_by_name(guild, subject)
        if target_channel:
            asyncio.run_coroutine_threadsafe(
                target_channel.send(msg), bot.loop
            ).result(timeout=10)

    return jsonify({"ok": ok, "message": msg})

@app.route("/list_schedule", methods=["GET"])
def list_schedule():
    guild_id = request.args.get("guild_id")
    if not guild_id:
        return jsonify({"ok": False, "error": "missing guild_id"})
    plans = load_plans(int(guild_id))
    return jsonify({"ok": True, "plans": sorted(plans, key=lambda p: p["date"])})

@app.route("/edit_schedule", methods=["POST"])
def edit_schedule():
    data         = request.json
    guild_id     = data.get("guild_id")
    target       = data.get("target")
    new_date     = data.get("date")
    new_subject  = data.get("subject")
    new_category = data.get("category")
    new_content  = data.get("content")

    if not all([guild_id, target]):
        return jsonify({"ok": False, "error": "missing fields"})

    guild_id = int(guild_id)
    guild    = bot.get_guild(guild_id)
    plans    = load_plans(guild_id)

    found = None
    for p in plans:
        label = f"{p['date']}/{p['subject']}{p['content']}"
        if label == target:
            found = p
            break

    if not found:
        return jsonify({"ok": False, "error": "plan not found"})

    before_str = f"{found['date']} / {found['subject']} / {found['content']}"

    if new_date:
        date_str = parse_date(new_date)
        if not date_str:
            return jsonify({"ok": False, "error": "invalid date"})
        found["date"] = date_str

    if new_subject:
        found["subject"] = new_subject

    if new_category and new_content:
        found["content"] = f"【{new_category}】{new_content}"
    elif new_category:
        body = found["content"].split("】", 1)[1] if "】" in found["content"] else found["content"]
        found["content"] = f"【{new_category}】{body}"
    elif new_content:
        tag = found["content"].split("】", 1)[0] + "】" if "】" in found["content"] else ""
        found["content"] = f"{tag}{new_content}"

    save_plans(guild_id, plans)
    after_str = f"{found['date']} / {found['subject']} / {found['content']}"
    write_log(guild_id, "edit", detail=f"{before_str} → {after_str}")

    if guild:
        target_channel = get_subject_channel_by_name(guild, found["subject"])
        if target_channel:
            msg = f"編集しました！\n\n【編集前】\n{before_str}\n\n【編集後】\n{after_str}"
            asyncio.run_coroutine_threadsafe(
                target_channel.send(msg), bot.loop
            ).result(timeout=10)

    return jsonify({"ok": True, "message": f"編集しました！\n{before_str} → {after_str}"})

@app.route("/delete_schedule", methods=["POST"])
def delete_schedule():
    data     = request.json
    guild_id = data.get("guild_id")
    target   = data.get("target")

    if not all([guild_id, target]):
        return jsonify({"ok": False, "error": "missing fields"})

    guild_id  = int(guild_id)
    guild     = bot.get_guild(guild_id)
    plans     = load_plans(guild_id)
    deleted   = None
    new_plans = []
    for p in plans:
        label = f"{p['date']}/{p['subject']}{p['content']}"
        if label == target:
            deleted = p
        else:
            new_plans.append(p)

    if not deleted:
        return jsonify({"ok": False, "error": "plan not found"})

    save_plans(guild_id, new_plans)
    write_log(guild_id, "delete", detail=f"{deleted['date']} / {deleted['subject']} / {deleted['content']}")

    if guild:
        target_channel = get_subject_channel_by_name(guild, deleted["subject"])
        if target_channel:
            asyncio.run_coroutine_threadsafe(
                target_channel.send(f"削除しました！\n{target}"), bot.loop
            ).result(timeout=10)

    return jsonify({"ok": True, "message": "削除しました！"})
# ================================
#  時間割データ
# ================================
def load_timetable(guild_id: int):
    data, _ = github_get(f"timetable_{guild_id}.json")
    return data or {}

def save_timetable(guild_id: int, data: dict):
    _, sha = github_get(f"timetable_{guild_id}.json")
    github_put(f"timetable_{guild_id}.json", data, sha)

# ================================
#  Flask API — 時間割
# ================================
@app.route("/list_timetable", methods=["GET"])
def list_timetable():
    guild_id = request.args.get("guild_id")
    if not guild_id:
        return jsonify({"ok": False, "error": "missing guild_id"})
    data = load_timetable(int(guild_id))
    overrides = [{"key": k, **v} for k, v in data.items()]
    return jsonify({"ok": True, "overrides": overrides})

@app.route("/update_timetable", methods=["POST"])
def update_timetable():
    data     = request.json
    guild_id = data.get("guild_id")
    key      = data.get("key")   # "change:YYYY-MM-DD:N"
    if not all([guild_id, key]):
        return jsonify({"ok": False, "error": "missing fields"})
    tt = load_timetable(int(guild_id))
    tt[key] = {
        "key":     key,
        "type":    "change",
        "date":    data.get("date"),
        "period":  data.get("period"),
        "subject": data.get("subject"),
        "items":   data.get("items", []),
        "note":    data.get("note", ""),
    }
    save_timetable(int(guild_id), tt)
    write_log(int(guild_id), "edit", detail=f"時間割変更: {key} → {data.get('subject')}")
    return jsonify({"ok": True})

@app.route("/set_holiday", methods=["POST"])
def set_holiday():
    data     = request.json
    guild_id = data.get("guild_id")
    key      = data.get("key")   # "holiday:YYYY-MM-DD"
    if not all([guild_id, key]):
        return jsonify({"ok": False, "error": "missing fields"})
    tt = load_timetable(int(guild_id))
    tt[key] = {
        "key":    key,
        "type":   "holiday",
        "date":   data.get("date"),
        "reason": data.get("reason", "休校"),
        "note":   data.get("note", ""),
    }
    save_timetable(int(guild_id), tt)
    write_log(int(guild_id), "edit", detail=f"休校設定: {data.get('date')} {data.get('reason')}")
    return jsonify({"ok": True})

@app.route("/delete_timetable", methods=["POST"])
def delete_timetable():
    data     = request.json
    guild_id = data.get("guild_id")
    key      = data.get("key")
    if not all([guild_id, key]):
        return jsonify({"ok": False, "error": "missing fields"})
    tt = load_timetable(int(guild_id))
    if key in tt:
        del tt[key]
        save_timetable(int(guild_id), tt)
        write_log(int(guild_id), "edit", detail=f"時間割変更削除: {key}")
    return jsonify({"ok": True})
# ================================
#  ユーザーデータ（users_{guild_id}.json）
# ================================

def load_users(guild_id: int):
    """users_{guild_id}.json を読み込む。なければ空リストを返す。"""
    data, _ = github_get(f"users_{guild_id}.json")
    return data or []

def save_users(guild_id: int, users: list):
    """users_{guild_id}.json を保存する。"""
    _, sha = github_get(f"users_{guild_id}.json")
    github_put(f"users_{guild_id}.json", users, sha)


# ================================
#  Flask API — ユーザー認証
# ================================

@app.route("/get_users", methods=["GET"])
def get_users():
    """
    GET /get_users?guild_id=...
    → { "ok": true, "users": [ {"id":"1I001","nickname":"Yuki","created_at":"2026-06-25"}, ... ] }
    """
    guild_id = request.args.get("guild_id")
    if not guild_id:
        return jsonify({"ok": False, "error": "missing guild_id"})
    try:
        users = load_users(int(guild_id))
        return jsonify({"ok": True, "users": users})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/add_user", methods=["POST"])
def add_user():
    data     = request.json
    guild_id = data.get("guild_id")
    user_id  = data.get("id", "").strip().upper()
    nickname = data.get("nickname", "").strip()
    created  = data.get("created_at") or datetime.now(JST).strftime("%Y-%m-%d")

    if not all([guild_id, user_id, nickname]):
        return jsonify({"ok": False, "error": "missing fields"})
    if len(nickname) > 16:
        return jsonify({"ok": False, "error": "nickname too long"})

    try:
        users = load_users(int(guild_id))

        if any(u["id"] == user_id for u in users):
            return jsonify({"ok": False, "error": "already_exists"})

        users.append({"id": user_id, "nickname": nickname, "created_at": created})
        save_users(int(guild_id), users)

        # write_log は呼ばない（予定管理用のため）
        return jsonify({"ok": True})

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/list_logs", methods=["GET"])
def list_logs():
    guild_id = request.args.get("guild_id")
    if not guild_id:
        return jsonify({"ok": False, "error": "missing guild_id"})
    logs, _ = github_get(f"logs_{guild_id}.json")
    logs = sorted(logs or [], key=lambda l: l["time"], reverse=True)
    return jsonify({"ok": True, "logs": logs})

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

    # ★ これを追加（Slash コマンド同期）
    synced = await bot.tree.sync()
    print(f"Synced {len(synced)} commands")

    if not started:
        scheduler.start()
        started = True
        print("Scheduler started!")

# ================================
#  単語カードデータ
#  GitHubの words/ フォルダに JSON として保存
# ================================

CARDS_DIR = "words"   # リポジトリ内のフォルダ名

def list_card_files():
    """words/ フォルダのファイル一覧を返す"""
    url     = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{CARDS_DIR}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}"}
    r = requests.get(url, headers=headers)
    if r.status_code == 404:
        return []
    files = r.json()
    return [f for f in files if isinstance(f, dict) and f["name"].endswith(".json")]

def get_card_file(filename):
    """words/{filename} の内容と SHA を返す"""
    data, sha = github_get(f"{CARDS_DIR}/{filename}")
    return data, sha

def put_card_file(filename, content_obj, sha=None):
    """words/{filename} に書き込む（sha があれば上書き）"""
    github_put(f"{CARDS_DIR}/{filename}", content_obj, sha)

def generate_card_filename():
    """set_YYYYMMDD_HHMM_xxxxxx.json 形式のファイル名を生成"""
    import random, string
    now    = datetime.now(JST)
    date   = now.strftime("%Y%m%d")
    time_  = now.strftime("%H%M")
    rand   = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"set_{date}_{time_}_{rand}.json"


# ================================
#  Flask API — 単語カード
# ================================

@app.route("/list_cards", methods=["GET"])
def list_cards():
    """words/ フォルダの JSON 一覧と各セットの name・cards・count を返す"""
    try:
        files = list_card_files()
        result = []
        for f in files:
            data, _ = github_get(f"{CARDS_DIR}/{f['name']}")
            if data is None:
                continue
            cards = data.get("cards", [])
            result.append({
                "filename": f["name"],
                "name":     data.get("name", f["name"]),
                "cards":    cards,           # ← カードの中身も返す
                "count":    len(cards),
            })
        return jsonify({"ok": True, "sets": result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/save_cards", methods=["POST"])
def save_cards():
    """
    新規作成・上書き保存
    body: { "name": str, "cards": [...], "filename": str (上書き時のみ) }
    """
    data     = request.json
    name     = data.get("name")
    cards    = data.get("cards")
    filename = data.get("filename")   # 上書きのときだけ送られてくる

    if not name or not isinstance(cards, list):
        return jsonify({"ok": False, "error": "name と cards は必須です"})

    is_update = bool(filename)

    # 新規のときはファイル名を自動生成
    if not filename:
        filename = generate_card_filename()

    # 上書きのときは既存の SHA が必要
    sha = None
    if is_update:
        _, sha = get_card_file(filename)

    content = {"name": name, "cards": cards}
    put_card_file(filename, content, sha)

    return jsonify({"ok": True, "filename": filename, "is_update": is_update})


@app.route("/delete_cards", methods=["POST"])
def delete_cards():
    """
    ファイルを削除
    body: { "filename": str }
    """
    data     = request.json
    filename = data.get("filename")

    if not filename:
        return jsonify({"ok": False, "error": "filename は必須です"})

    url     = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{CARDS_DIR}/{filename}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}"}

    # SHA を取得
    r = requests.get(url, headers=headers)
    if r.status_code == 404:
        return jsonify({"ok": False, "error": "ファイルが見つかりません"})

    sha = r.json().get("sha")
    requests.delete(url, headers=headers, json={
        "message": f"delete {filename}",
        "sha":     sha,
    })

    return jsonify({"ok": True})

import time

keep_alive()

print(f"[INFO] TOKEN set: {bool(TOKEN)}, length: {len(TOKEN) if TOKEN else 0}")
print(f"[INFO] Starting bot.run()...")

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
    print(f"[ERROR] HTTPException: {e.status} {e.text}")
    raise
except Exception as e:
    print(f"[ERROR] bot.run failed: {type(e).__name__}: {e}")
    raise
