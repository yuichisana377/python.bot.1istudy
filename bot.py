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
GITHUB_TOKEN   = os.getenv("GITHUB_TOKEN")
TOKEN          = os.getenv("TOKEN")
SUBJECT_CATEGORY = os.getenv("SUBJECT_CATEGORY") # 科目チャンネルが入っているカテゴリ名
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
#  Discord Bot
# ================================
intents = discord.Intents.default()
intents.message_content = True
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

# ================================
#  設定ファイル
# ================================
def load_config(guild_id: int):
    data, _ = github_get(f"config_{guild_id}.json")
    return data or {}

def save_config(guild_id: int, data: dict):
    _, sha = github_get(f"config_{guild_id}.json")
    github_put(f"config_{guild_id}.json", data, sha)

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

# ================================
#  ログ
# ================================
def write_log(guild_id: int, log_type: str, detail: str):
    filename = f"logs_{guild_id}.json"
    logs, sha = github_get(filename)
    logs = logs or []

    now_jst = datetime.now(JST)
    now_str = now_jst.strftime("%Y-%m-%d %H:%M:%S")

    # 30日より古いログを削除
    logs = [
        log for log in logs
        if (now_jst - datetime.strptime(log["time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST)).days <= 30
    ]

    logs.append({"time": now_str, "type": log_type, "detail": detail})
    github_put(filename, logs, sha)

# ================================
#  科目チャンネルユーティリティ
# ================================
def get_subject_channels(guild: discord.Guild) -> list[discord.TextChannel]:
    """SUBJECT_CATEGORY に属するテキストチャンネルを返す。"""
    if not SUBJECT_CATEGORY:
        return guild.text_channels
    for cat in guild.categories:
        if cat.name == SUBJECT_CATEGORY:
            return cat.text_channels
    return []

def get_subject_channel_by_name(guild: discord.Guild, name: str):
    """科目名（チャンネル名）からチャンネルオブジェクトを返す。"""
    for ch in get_subject_channels(guild):
        if ch.name == name:
            return ch
    return None

# ================================
#  日付パース共通関数
# ================================
def parse_date(date: str):
    """日付文字列をパースして YYYY-MM-DD 形式で返す。失敗時は None。"""
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
    """
    予定を追加する共通処理。
    成功時: (True, 結果メッセージ)
    失敗時: (False, エラーメッセージ)
    """
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
#  /add コマンド
# ================================
@bot.tree.command(name="add", description="予定を追加する")
@app_commands.describe(
    date="日付（例: 6-20, 2026-06-20）",
    subject="科目（省略するとこのチャンネル名を使用）",
    category="分類（宿題・提出・持ち物など）",
    content="内容"
)
async def add_plan(
    interaction: discord.Interaction,
    date: str,
    category: str,
    content: str,
    subject: str = None,
):
    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild

    # subject 未指定 → 実行チャンネル名を科目として使う
    if not subject:
        subject = interaction.channel.name

    ok, msg = await add_plan_internal(guild.id, subject, date, category, content)

    if ok:
        # 登録した科目のチャンネルに送る
        target_channel = get_subject_channel_by_name(guild, subject)
        if target_channel:
            await target_channel.send(msg)
        else:
            # 科目チャンネルが見つからなければ実行チャンネルに送る
            await interaction.channel.send(msg)
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
    return [
        app_commands.Choice(name=c, value=c)
        for c in candidates if current in c
    ][:25]

# ================================
#  /list コマンド
# ================================
@bot.tree.command(name="list", description="予定一覧を表示する")
@app_commands.describe(date="all または 日付（例: 6/15, 2026-06-15）")
async def list_plans(interaction: discord.Interaction, date: str):
    guild_id = interaction.guild.id
    plans = load_plans(guild_id)

    if date.lower() == "all":
        if not plans:
            await interaction.response.send_message("予定はありません。", ephemeral=True)
            return
        sorted_plans = sorted(plans, key=lambda p: p["date"])
        msg = "📘 **すべての予定一覧**\n"
        for p in sorted_plans:
            msg += f"- {p['date']}：{p['subject']} {p['content']}\n"
        await interaction.response.send_message(msg, ephemeral=True)
        return

    date_str = parse_date(date)
    if not date_str:
        await interaction.response.send_message("日付の形式が正しくありません！", ephemeral=True)
        return

    selected = [p for p in plans if p["date"] == date_str]
    if not selected:
        await interaction.response.send_message(f"{date} の予定はありません。", ephemeral=True)
        return

    msg = f"📘 **{date_str} の予定**\n"
    for p in selected:
        msg += f"- {p['subject']} {p['content']}\n"
    await interaction.response.send_message(msg, ephemeral=True)

# ================================
#  /delete コマンド
# ================================
@bot.tree.command(name="delete", description="予定を削除する")
@app_commands.describe(target="削除したい予定")
async def delete_plan(interaction: discord.Interaction, target: str):
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
    write_log(guild_id, "delete", detail=f"{deleted['date']} / {deleted['subject']} / {deleted['content']}")
    await interaction.response.send_message(f"削除しました！\n{target}")

@delete_plan.autocomplete("target")
async def delete_autocomplete(interaction: discord.Interaction, current: str):
    guild_id = interaction.guild.id
    plans = load_plans(guild_id)
    choices = []
    for p in plans:
        label = f"{p['date']}/{p['subject']}{p['content']}"
        if current in label:
            choices.append(app_commands.Choice(name=label, value=label))
    return choices[:25]

# ================================
#  /edit コマンド
# ================================
@bot.tree.command(name="edit", description="予定を編集する")
@app_commands.describe(
    target="編集したい予定",
    date="新しい日付",
    subject="新しい科目（省略するとこのチャンネル名を使用）",
    category="新しい分類",
    content="新しい内容"
)
async def edit_plan(
    interaction: discord.Interaction,
    target: str,
    date: str = None,
    subject: str = None,
    category: str = None,
    content: str = None,
):
    guild = interaction.guild
    plans = load_plans(guild.id)

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

    # 日付の更新
    if date:
        date_str = parse_date(date)
        if not date_str:
            await interaction.response.send_message("日付の形式が正しくありません！", ephemeral=True)
            return
        found["date"] = date_str

    # 科目の更新（未指定なら実行チャンネル名を使う。それも変えたくない場合は subject="" を渡さない）
    if subject:
        found["subject"] = subject
    # subject が None（未指定）のときは科目を変えない

    # カテゴリ・内容の更新
    if category and content:
        found["content"] = f"【{category}】{content}"
    elif category:
        body = found["content"].split("】", 1)[1] if "】" in found["content"] else found["content"]
        found["content"] = f"【{category}】{body}"
    elif content:
        tag = found["content"].split("】", 1)[0] + "】" if "】" in found["content"] else ""
        found["content"] = f"{tag}{content}"

    save_plans(guild.id, plans)
    after_str = f"{found['date']} / {found['subject']} / {found['content']}"
    write_log(guild.id, "edit", detail=f"{before_str} → {after_str}")

    # 編集結果を科目チャンネルに送る
    target_channel = get_subject_channel_by_name(guild, found["subject"])
    result_msg = f"編集しました！\n\n【編集前】\n{before_str}\n\n【編集後】\n{after_str}"
    if target_channel:
        await target_channel.send(result_msg)
        await interaction.response.send_message("完了しました！", ephemeral=True)
    else:
        await interaction.response.send_message(result_msg)

@edit_plan.autocomplete("target")
async def edit_target_autocomplete(interaction: discord.Interaction, current: str):
    guild_id = interaction.guild.id
    plans = load_plans(guild_id)
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
    return [
        app_commands.Choice(name=c, value=c)
        for c in candidates if current in c
    ][:25]

# ================================
#  /cleanup コマンド
# ================================
@bot.tree.command(name="cleanup", description="過去の予定を削除する")
async def cleanup_command(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    plans = load_plans(guild_id)
    today = datetime.now(JST).strftime("%Y-%m-%d")

    deleted_dates = sorted({p["date"] for p in plans if p["date"] < today})
    new_plans = [p for p in plans if p["date"] >= today]
    save_plans(guild_id, new_plans)

    if deleted_dates:
        write_log(guild_id, "cleanup", detail="削除した日付: " + ", ".join(deleted_dates))
        await interaction.response.send_message(
            f"🧹 {len(deleted_dates)}件削除しました！\n" + "\n".join(deleted_dates),
            ephemeral=True
        )
    else:
        await interaction.response.send_message("削除する予定はありませんでした！", ephemeral=True)

# ================================
#  /setchannel コマンド
# ================================
@bot.tree.command(name="setchannel", description="通知チャンネルを設定する")
async def setchannel(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    config = load_config(guild_id)
    config["remind_channel_id"] = interaction.channel.id
    save_config(guild_id, config)
    await interaction.response.send_message(
        f"通知チャンネルを **#{interaction.channel.name}** に設定しました！"
    )

# ================================
#  /help コマンド
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
    await interaction.response.send_message(msg, ephemeral=True)

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
    today = datetime.now(JST).strftime("%Y-%m-%d")
    for filename in list_all_configs():
        guild_id = int(filename.replace("config_", "").replace(".json", ""))
        plans = load_plans(guild_id)
        deleted_dates = sorted({p["date"] for p in plans if p["date"] < today})
        new_plans = [p for p in plans if p["date"] >= today]
        if deleted_dates:
            save_plans(guild_id, new_plans)
            write_log(guild_id, "cleanup", detail="削除した日付: " + ", ".join(deleted_dates))
            print(f"{guild_id} の過去予定を削除しました。")

# ================================
#  Flask API（WebUI用）
# ================================
@app.route("/channels", methods=["GET"])
def get_channels():
    """SUBJECT_CATEGORY 内のチャンネル一覧を返す。"""
    guild_id = request.args.get("guild_id")
    if not guild_id:
        return jsonify({"ok": False, "error": "missing guild_id"})
    guild = bot.get_guild(int(guild_id))
    if not guild:
        return jsonify({"ok": False, "error": "guild not found"})
    channels = [
        {"id": str(ch.id), "name": ch.name}
        for ch in get_subject_channels(guild)
    ]
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

    # 登録した科目のチャンネルに通知
    if ok and guild:
        target_channel = get_subject_channel_by_name(guild, subject)
        if target_channel:
            asyncio.run_coroutine_threadsafe(
                target_channel.send(msg),
                bot.loop
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
    new_subject  = data.get("subject")   # 省略可
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

    # 編集結果を科目チャンネルに通知
    if guild:
        target_channel = get_subject_channel_by_name(guild, found["subject"])
        if target_channel:
            msg = f"編集しました！\n\n【編集前】\n{before_str}\n\n【編集後】\n{after_str}"
            asyncio.run_coroutine_threadsafe(
                target_channel.send(msg),
                bot.loop
            ).result(timeout=10)

    return jsonify({"ok": True, "message": f"編集しました！\n{before_str} → {after_str}"})

@app.route("/delete_schedule", methods=["POST"])
def delete_schedule():
    data     = request.json
    guild_id = data.get("guild_id")
    target   = data.get("target")

    if not all([guild_id, target]):
        return jsonify({"ok": False, "error": "missing fields"})

    guild_id = int(guild_id)
    plans    = load_plans(guild_id)
    deleted  = None
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
    return jsonify({"ok": True, "message": "削除しました！"})

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
    await bot.tree.sync()
    if not started:
        scheduler.start()
        started = True
        print("Scheduler started!")

bot.run(TOKEN)
keep_alive()
