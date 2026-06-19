import discord
from discord import app_commands
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import datetime, timedelta
import json
import os
from flask import Flask
from threading import Thread
from pytz import timezone
import requests
import base64

GITHUB_REPO = "yuichisana377/python.bot.1istudy"
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

scheduler = AsyncIOScheduler(timezone=timezone("Asia/Tokyo"))

app = Flask('')

@app.route('/')
def home():
    return "I'm alive"

def run():
    app.run(host='0.0.0.0', port=10000)

def keep_alive():
    t = Thread(target=run)
    t.start()

TOKEN = os.getenv("TOKEN")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ================================
#  設定ファイル
# ================================
def load_config(guild_id: int):
    filename = f"config_{guild_id}.json"
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}"}

    r = requests.get(url, headers=headers)
    if r.status_code == 404:
        return {}

    data = r.json()
    content = base64.b64decode(data["content"]).decode()
    return json.loads(content)


def save_config(guild_id: int, data: dict):
    filename = f"config_{guild_id}.json"
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}"}

    r = requests.get(url, headers=headers)
    sha = r.json()["sha"] if r.status_code != 404 else None

    new_content = base64.b64encode(
        json.dumps(data, ensure_ascii=False, indent=2).encode()
    ).decode()

    payload = {"message": f"update {filename}", "content": new_content}
    if sha:
        payload["sha"] = sha

    requests.put(url, headers=headers, json=payload)


def list_all_configs():
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents"
    headers = {"Authorization": f"token {GITHUB_TOKEN}"}

    r = requests.get(url, headers=headers)
    files = r.json()

    return [
        f["name"] for f in files
        if f["name"].startswith("config_") and f["name"].endswith(".json")
    ]

# ================================
#  予定データ
# ================================
def load_plans(guild_id: int):
    filename = f"plans_{guild_id}.json"
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}"}

    r = requests.get(url, headers=headers)
    if r.status_code == 404:
        return []

    data = r.json()
    content = base64.b64decode(data["content"]).decode()
    return json.loads(content)


def save_plans(guild_id: int, plans: list):
    filename = f"plans_{guild_id}.json"
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}"}

    r = requests.get(url, headers=headers)
    sha = r.json()["sha"] if r.status_code != 404 else None

    new_content = base64.b64encode(
        json.dumps(plans, ensure_ascii=False, indent=2).encode()
    ).decode()

    payload = {"message": f"update {filename}", "content": new_content}
    if sha:
        payload["sha"] = sha

    requests.put(url, headers=headers, json=payload)

# ================================
#  ログ保存（edit だけ before/after）
# ================================
def write_log(guild_id: int, log_type: str, before=None, after=None, detail=None):
    filename = f"logs_{guild_id}.json"
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}"}

    r = requests.get(url, headers=headers)
    if r.status_code == 404:
        logs = []
        sha = None
    else:
        data = r.json()
        logs = json.loads(base64.b64decode(data["content"]).decode())
        sha = data["sha"]

    jst = timezone("Asia/Tokyo")
    now_jst = datetime.now(jst).strftime("%Y-%m-%d %H:%M:%S")

    entry = {
        "time": now_jst,
        "type": log_type
    }

    if log_type == "edit":
        entry["before"] = before
        entry["after"] = after
    else:
        entry["detail"] = detail

    logs.append(entry)

    new_content = base64.b64encode(
        json.dumps(logs, ensure_ascii=False, indent=2).encode()
    ).decode()

    payload = {"message": f"update {filename}", "content": new_content}
    if sha:
        payload["sha"] = sha

    requests.put(url, headers=headers, json=payload)

# ================================
#  /add
# ================================
@bot.tree.command(name="add", description="予定を追加する")
@app_commands.describe(
    date="日付（例: 6-20, 2026-06-20）",
    category="分類（宿題・提出・持ち物など）",
    content="内容"
)
async def add_plan(interaction, date: str, category: str, content: str):

    try:
        if "-" in date and len(date.split("-")[0]) == 4:
            parsed = datetime.strptime(date, "%Y-%m-%d")
        else:
            date = date.replace("/", "-")
            m, d = date.split("-")
            y = datetime.now().year
            parsed = datetime.strptime(f"{y}-{int(m):02d}-{int(d):02d}", "%Y-%m-%d")
    except:
        await interaction.response.send_message("日付の形式が正しくありません！", ephemeral=True)
        return

    date = parsed.strftime("%Y-%m-%d")

    today = datetime.now().date()
    if datetime.strptime(date, "%Y-%m-%d").date() < today:
        await interaction.response.send_message("過去の日付は登録できません！", ephemeral=True)
        return

    subject = interaction.channel.name
    tagged = f"【{category}】{content}"

    guild_id = interaction.guild.id
    plans = load_plans(guild_id)

    plans.append({"date": date, "subject": subject, "content": tagged})
    save_plans(guild_id, plans)

    write_log(guild_id, "add", detail=f"{date} / {subject} / {tagged}")

    await interaction.response.send_message(
        f"登録しました！\n{date} / {subject} / {tagged}"
    )

@add_plan.autocomplete("category")
async def category_autocomplete(interaction, current):
    candidates = ["宿題", "提出", "持ち物", "テスト", "その他"]
    return [
        app_commands.Choice(name=c, value=c)
        for c in candidates if current in c
    ][:25]

# ================================
#  /delete
# ================================
@bot.tree.command(name="delete", description="予定を削除する")
@app_commands.describe(target="削除したい予定")
async def delete(interaction, target: str):

    guild_id = interaction.guild.id
    plans = load_plans(guild_id)

    deleted = None
    new_plans = []

    for p in plans:
        label = f"{p['date']}/{p['subject']}{p['content']}"
        if label == target:
            deleted = p
        else:
            new_plans.append(p)

    if not deleted:
        await interaction.response.send_message("その予定は見つかりませんでした。", ephemeral=True)
        return

    save_plans(guild_id, new_plans)

    write_log(
        guild_id,
        "delete",
        detail=f"{deleted['date']} / {deleted['subject']} / {deleted['content']}"
    )

    await interaction.response.send_message(f"削除しました！\n{target}")

@delete.autocomplete("target")
async def delete_autocomplete(interaction, current):
    guild_id = interaction.guild.id
    plans = load_plans(guild_id)

    choices = []
    for p in plans:
        label = f"{p['date']}/{p['subject']}{p['content']}"
        if current in label:
            choices.append(app_commands.Choice(name=label, value=label))

    return choices[:25]

# ================================
#  /cleanup
# ================================
@bot.tree.command(name="cleanup", description="過去の予定を削除する")
async def cleanup_command(interaction):

    guild_id = interaction.guild.id
    plans = load_plans(guild_id)

    today = datetime.now(timezone("Asia/Tokyo")).strftime("%Y-%m-%d")

    deleted_dates = sorted({p["date"] for p in plans if p["date"] < today})
    new_plans = [p for p in plans if p["date"] >= today]

    save_plans(guild_id, new_plans)

    if deleted_dates:
        write_log(
            guild_id,
            "cleanup",
            detail="削除した日付: " + ", ".join(deleted_dates)
        )
        await interaction.response.send_message(
            f"🧹 {len(deleted_dates)}件削除しました！\n" +
            "\n".join(deleted_dates),
            ephemeral=True
        )
    else:
        await interaction.response.send_message("削除する予定はありませんでした！", ephemeral=True)

# ================================
#  /edit
# ================================
@bot.tree.command(name="edit", description="予定を編集する")
@app_commands.describe(
    target="編集したい予定",
    date="新しい日付",
    category="新しい分類",
    content="新しい内容"
)
async def edit_plan(interaction, target: str, date: str = None, category: str = None, content: str = None):

    guild_id = interaction.guild.id
    plans = load_plans(guild_id)

    found = None
    for p in plans:
        label = f"{p['date']}/{p['subject']}{p['content']}"
        if label == target:
            found = p
            break

    if not found:
        await interaction.response.send_message("その予定が見つかりませんでした。", ephemeral=True)
        return

    before_str = f"{found['date']} / {found['subject']} / {found['content']}"

    if date:
        try:
            if "-" in date and len(date.split("-")[0]) == 4:
                parsed = datetime.strptime(date, "%Y-%m-%d")
            else:
                date = date.replace("/", "-")
                m, d = date.split("-")
                y = datetime.now().year
                parsed = datetime.strptime(f"{y}-{int(m):02d}-{int(d):02d}", "%Y-%m-%d")
            found["date"] = parsed.strftime("%Y-%m-%d")
        except:
            await interaction.response.send_message("日付の形式が正しくありません！", ephemeral=True)
            return

    if category and content:
        found["content"] = f"【{category}】{content}"
    elif category:
        old = found["content"]
        body = old.split("】", 1)[1] if "】" in old else old
        found["content"] = f"【{category}】{body}"
    elif content:
        old = found["content"]
        tag = old.split("】", 1)[0] + "】" if "】" in old else ""
        found["content"] = f"{tag}{content}"

    save_plans(guild_id, plans)

    after_str = f"{found['date']} / {found['subject']} / {found['content']}"

    write_log(guild_id, "edit", before=before_str, after=after_str)

    await interaction.response.send_message(
        f"編集しました！\n\n【編集前】\n{before_str}\n\n【編集後】\n{after_str}"
    )

@edit_plan.autocomplete("target")
async def edit_autocomplete(interaction, current):
    guild_id = interaction.guild.id
    plans = load_plans(guild_id)

    choices = []
    for p in plans:
        label = f"{p['date']}/{p['subject']}{p['content']}"
        if current in label:
            choices.append(app_commands.Choice(name=label, value=label))

    return choices[:25]

@edit_plan.autocomplete("category")
async def category_autocomplete(interaction, current):
    candidates = ["宿題", "提出", "持ち物", "テスト", "その他"]
    return [
        app_commands.Choice(name=c, value=c)
        for c in candidates if current in c
    ][:25]


async def send_tomorrow_plans():
    config_files = list_all_configs()

    for filename in config_files:
        guild_id = int(filename.replace("config_", "").replace(".json", ""))

        config = load_config(guild_id)
        channel_id = config.get("remind_channel_id")
        if not channel_id:
            continue

        channel = bot.get_channel(channel_id)
        if not channel:
            continue

        jst = timezone("Asia/Tokyo")
        tomorrow = (datetime.now(jst) + timedelta(days=1)).strftime("%Y-%m-%d")

        plans = load_plans(guild_id)
        tomorrow_plans = [p for p in plans if p["date"] == tomorrow]

        if tomorrow_plans:
            msg = "こんばんは！明日の予定です。\n"
            for p in tomorrow_plans:
                msg += f"・{p['subject']} {p['content']}\n"
            msg += "@everyone"
        else:
            msg = "こんばんは！明日の予定はありません。\n@everyone"

        await channel.send(msg)

async def send_today_plans():
    config_files = list_all_configs()

    for filename in config_files:
        guild_id = int(filename.replace("config_", "").replace(".json", ""))

        config = load_config(guild_id)
        channel_id = config.get("remind_channel_id")
        if not channel_id:
            continue

        channel = bot.get_channel(channel_id)
        if not channel:
            continue

        jst = timezone("Asia/Tokyo")
        today = datetime.now(jst).strftime("%Y-%m-%d")

        plans = load_plans(guild_id)
        today_plans = [p for p in plans if p["date"] == today]

        if today_plans:
            msg = "おはようございます！今日の予定です。\n"
            for p in today_plans:
                msg += f"・{p['subject']} {p['content']}\n"
            msg += "@everyone"
        else:
            msg = "おはようございます！今日の予定はありません。\n@everyone"

        await channel.send(msg)

# ================================
#  自動 cleanup
# ================================
async def cleanup_past_plans():
    config_files = list_all_configs()

    jst = timezone("Asia/Tokyo")
    today = datetime.now(jst).strftime("%Y-%m-%d")

    for filename in config_files:
        guild_id = int(filename.replace("config_", "").replace(".json", ""))

        plans = load_plans(guild_id)
        before_count = len(plans)

        deleted_dates = sorted({p["date"] for p in plans if p["date"] < today})
        new_plans = [p for p in plans if p["date"] >= today]
        after_count = len(new_plans)

        if before_count != after_count:
            save_plans(guild_id, new_plans)

            write_log(
                guild_id,
                "cleanup",
                detail="削除した日付: " + ", ".join(deleted_dates)
            )

            print(f"{guild_id} の過去予定を削除しました。")

# ================================
#  /setchannel
# ================================
@bot.tree.command(name="setchannel", description="通知チャンネルを設定する")
async def setchannel(interaction):
    guild_id = interaction.guild.id
    config = load_config(guild_id)

    channel = interaction.channel
    config["remind_channel_id"] = channel.id
    save_config(guild_id, config)

    await interaction.response.send_message(
        f"通知チャンネルを **#{channel.name}** に設定しました！"
    )

# ================================
#  /help
# ================================
@bot.tree.command(name="help", description="使えるコマンド一覧")
async def help_command(interaction):
    msg = (
        "📘 **使えるコマンド一覧**\n\n"
        "**/add** — 予定を登録する\n"
        "**/list** — 予定を表示する\n"
        "**/delete** — 予定を削除する\n"
        "**/edit** — 予定を編集する\n"
        "**/cleanup** — 過去の予定を削除する\n"
        "**/setchannel** — 通知チャンネルを設定する\n"
    )
    await interaction.response.send_message(msg, ephemeral=True)

# ================================
#  スケジューラー
# ================================
scheduler.add_job(send_tomorrow_plans, "cron", hour=20, minute=0)
scheduler.add_job(send_today_plans, "cron", hour=5, minute=30)
scheduler.add_job(cleanup_past_plans, "cron", hour=0, minute=0)

started = False

@bot.event
async def on_ready():
    global started
    print("Bot is ready!")
    await bot.tree.sync()

    if not started:
        scheduler.start()
        started = True
        print("Scheduler started!")
    



keep_alive()
bot.run(TOKEN)
