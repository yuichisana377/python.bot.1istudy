import discord
from discord import app_commands
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import datetime, timedelta
from datetime import date as _date
from flask import Flask, request, jsonify, make_response
from flask_cors import CORS
from threading import Thread
from pytz import timezone
import json
import os
import requests
import base64
import asyncio
import time
import logging
logging.basicConfig(level=logging.INFO)
discord_logger = logging.getLogger("discord")
discord_logger.setLevel(logging.DEBUG)


# ================================
#  設定
# ================================
GITHUB_REPO         = os.getenv("GITHUB_REPO")
GITHUB_TOKEN        = os.getenv("GITHUB_TOKEN")
TOKEN               = os.getenv("TOKEN")
SUBJECT_CATEGORY_ID = os.getenv("SUBJECT_CATEGORY_ID")  # カテゴリID（優先）
SUBJECT_CATEGORY    = os.getenv("SUBJECT_CATEGORY")     # カテゴリ名（フォールバック）
JST = timezone("Asia/Tokyo")

# --- 通生/寮生 振り分け用の絵文字 ---
EMOJI_COMMUTER = "🚃"  # 通生
EMOJI_DORM     = "🏠"  # 寮生

scheduler = AsyncIOScheduler(timezone=JST)

# ================================
#  Flask アプリ
# ================================
app = Flask("")
CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=False)

@app.before_request
def handle_preflight():
    if request.method == "OPTIONS":
        res = make_response()
        res.headers["Access-Control-Allow-Origin"]  = "*"
        res.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        res.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        return res, 200

@app.route("/debug_discord")
def debug_discord():
    import time
    result = {}
    try:
        start = time.time()
        r = requests.get("https://discord.com/api/v10/gateway", timeout=8)
        result["ok"] = True
        result["status_code"] = r.status_code
        result["elapsed_sec"] = round(time.time() - start, 2)
        result["body"] = r.text[:200]
    except Exception as e:
        result["ok"] = False
        result["error"] = f"{type(e).__name__}: {e}"
    return jsonify(result)

@app.route("/")
def home():
    return "I'm alive"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
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

async def async_write_log(guild_id: int, log_type: str, detail: str):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, write_log, guild_id, log_type, detail)

# ================================
#  勉強ログ データ
# ================================
def load_study_logs(guild_id: int):
    data, _ = github_get(f"study_logs_{guild_id}.json")
    return data or []

def save_study_logs(guild_id: int, logs: list):
    _, sha = github_get(f"study_logs_{guild_id}.json")
    github_put(f"study_logs_{guild_id}.json", logs, sha)

async def async_load_study_logs(guild_id: int):
    data, _ = await async_github_get(f"study_logs_{guild_id}.json")
    return data or []

async def async_save_study_logs(guild_id: int, logs: list):
    _, sha = await async_github_get(f"study_logs_{guild_id}.json")
    await async_github_put(f"study_logs_{guild_id}.json", logs, sha)

# ================================
#  ポイント データ
# ================================
def load_points(guild_id: int) -> dict:
    data, _ = github_get(f"points_{guild_id}.json")
    return data or {}

def save_points(guild_id: int, pts: dict, sha=None):
    if sha is None:
        _, sha = github_get(f"points_{guild_id}.json")
    github_put(f"points_{guild_id}.json", pts, sha)

# ============================================================
#  課題達成データ
#
#  completed_tasks_{guild_id}.json の形式:
#  {
#    "1I001": [
#      {"id": "task_id_1", "date": "2025-06-30", "points": 5, "nickname": "太郎"},
#      {"id": "task_id_2", "date": "2025-06-28", "points": 10, "nickname": "太郎"}
#    ],
#    "1I002": [
#      {"id": "task_id_3", "date": "2025-06-25", "points": 5, "nickname": "花子"}
#    ]
#  }
#
#  ※ 旧形式（文字列のみ／points・nicknameキーなし）も読み込み時に自動正規化される。
#     データの移行作業は不要。
# ============================================================
def load_completed_tasks(guild_id: int) -> dict:
    data, _ = github_get(f"completed_tasks_{guild_id}.json")
    return data or {}

def save_completed_tasks(guild_id: int, tasks: dict, sha=None):
    if sha is None:
        _, sha = github_get(f"completed_tasks_{guild_id}.json")
    github_put(f"completed_tasks_{guild_id}.json", tasks, sha)


def _normalize_task_entry(entry):
    """旧形式（文字列）・旧dict形式（points/nicknameなし）・新形式を統一する"""
    if isinstance(entry, str):
        return {"id": entry, "date": None, "points": None, "nickname": None}
    entry = dict(entry)
    if "points" not in entry:
        entry["points"] = None
    if "nickname" not in entry:
        entry["nickname"] = None
    return entry


# ================================
#  ユーザーデータ
# ================================
def load_users(guild_id: int):
    data, _ = github_get(f"users_{guild_id}.json")
    return data or []

def save_users(guild_id: int, users: list):
    _, sha = github_get(f"users_{guild_id}.json")
    github_put(f"users_{guild_id}.json", users, sha)

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
#  ポイントを付与すべきカテゴリかどうか
# ================================
POINT_CATEGORIES = ("提出", "宿題")
DEFAULT_TASK_POINTS = 5

# ================================
#  add 内部関数
# ================================
async def add_plan_internal(guild_id: int, subject: str, date: str, category: str, content: str, points=None):
    date_str = parse_date(date)
    if not date_str:
        return False, "日付の形式が正しくありません！"
    today = datetime.now(JST).date()
    if datetime.strptime(date_str, "%Y-%m-%d").date() < today:
        return False, "過去の日付は登録できません！"
    tagged_content = f"【{category}】{content}"

    plan = {"date": date_str, "subject": subject, "content": tagged_content}
    if category in POINT_CATEGORIES:
        plan["points"] = points if points is not None else DEFAULT_TASK_POINTS

    plans = load_plans(guild_id)
    plans.append(plan)
    save_plans(guild_id, plans)

    detail = f"{date_str} / {subject} / {tagged_content}"
    if "points" in plan:
        detail += f" ({plan['points']}pt)"
    write_log(guild_id, "add", detail=detail)

    msg = f"登録しました！\n{date_str} / {subject} / {tagged_content}"
    if "points" in plan:
        msg += f"\n⭐ {plan['points']}pt"
    return True, msg

# ================================
#  /add
# ================================
@bot.tree.command(name="add", description="予定を追加する")
@app_commands.describe(
    date="日付（例: 6-20, 2026-06-20）",
    subject="科目（省略するとこのチャンネル名を使用）",
    category="分類（宿題・提出・持ち物など）",
    content="内容",
    points="ポイント（提出・宿題のみ有効。省略時は5pt）"
)
async def add_plan(interaction: discord.Interaction, date: str, category: str, content: str, subject: str = None, points: int = None):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    if not subject:
        subject = interaction.channel.name
    ok, msg = await add_plan_internal(guild.id, subject, date, category, content, points)
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
            pts_str = f" ⭐{p['points']}pt" if "points" in p else ""
            msg += f"- {p['date']}：{p['subject']} {p['content']}{pts_str}\n"
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
        pts_str = f" ⭐{p['points']}pt" if "points" in p else ""
        msg += f"- {p['subject']} {p['content']}{pts_str}\n"
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
    content="新しい内容",
    points="新しいポイント（提出・宿題のみ有効）"
)
async def edit_plan(interaction: discord.Interaction, target: str, date: str = None, subject: str = None, category: str = None, content: str = None, points: int = None):
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

    # ★ ポイント更新
    current_category = found["content"].split("】", 1)[0].lstrip("【") if "】" in found["content"] else ""
    if points is not None:
        found["points"] = points
    if current_category not in POINT_CATEGORIES and "points" in found:
        # 提出・宿題以外に変更された場合はポイントを外す
        del found["points"]
    elif current_category in POINT_CATEGORIES and "points" not in found:
        found["points"] = DEFAULT_TASK_POINTS

    await async_save_plans(guild.id, plans)
    after_str = f"{found['date']} / {found['subject']} / {found['content']}"
    await async_write_log(guild.id, "edit", detail=f"{before_str} → {after_str}")
    msg = f"編集しました！\n\n【編集前】\n{before_str}\n\n【編集後】\n{after_str}"
    if "points" in found:
        msg += f"\n⭐ {found['points']}pt"
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
    threshold = today - timedelta(days=7)
    deleted_dates = sorted({
        p["date"] for p in plans
        if datetime.strptime(p["date"], "%Y-%m-%d").date() < threshold
    })
    new_plans = [
        p for p in plans
        if datetime.strptime(p["date"], "%Y-%m-%d").date() >= threshold
    ]
    await async_save_plans(guild_id, new_plans)
    if deleted_dates:
        await async_write_log(guild_id, "cleanup", detail="削除した日付: " + ", ".join(deleted_dates))
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
@app_commands.describe(type="どちらの朝通知に使うチャンネルか（省略時は通生）")
@app_commands.choices(type=[
    app_commands.Choice(name="通生（朝5:30 / 夜20:00）", value="commute"),
    app_commands.Choice(name="寮生（朝7:20 / 夜20:00）", value="dorm"),
])
async def setchannel(interaction: discord.Interaction, type: app_commands.Choice[str] = None):
    await interaction.response.defer(ephemeral=True)
    guild_id = interaction.guild.id
    config = await async_load_config(guild_id)

    kind = type.value if type else "commute"
    if kind == "dorm":
        config["remind_channel_id_dorm"] = interaction.channel.id
        label = "寮生（朝7:20）"
    else:
        config["remind_channel_id"] = interaction.channel.id
        label = "通生（朝5:30・夜20:00）"

    await async_save_config(guild_id, config)
    await interaction.followup.send(
        f"{label} の通知チャンネルを **#{interaction.channel.name}** に設定しました！"
    )

# ================================
#  /setup_roles（通生/寮生 振り分けパネル）
# ================================
@bot.tree.command(name="setup_roles", description="通生/寮生 振り分けパネルを投稿します")
@app_commands.describe(通生ロール="通生に付与するロール", 寮生ロール="寮生に付与するロール")
@app_commands.checks.has_permissions(manage_roles=True)
async def setup_roles(
    interaction: discord.Interaction,
    通生ロール: discord.Role,
    寮生ロール: discord.Role,
):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    # Botより上位のロールは付与できないため確認
    if 通生ロール >= guild.me.top_role or 寮生ロール >= guild.me.top_role:
        await interaction.followup.send(
            "ロールの順序を確認してください。Botの役職を、通生・寮生ロールより上に配置する必要があります。",
            ephemeral=True,
        )
        return

    embed = discord.Embed(
        title="通生 / 寮生 登録",
        description=(
            f"{EMOJI_COMMUTER} → 通生\n"
            f"{EMOJI_DORM} → 寮生\n\n"
            "どちらか当てはまる方にリアクションしてください。"
        ),
        color=discord.Color.teal(),
    )
    msg = await interaction.channel.send(embed=embed)
    await msg.add_reaction(EMOJI_COMMUTER)
    await msg.add_reaction(EMOJI_DORM)

    config = await async_load_config(guild.id)
    config["role_panel_message_id"] = msg.id
    config["role_panel_channel_id"] = msg.channel.id
    config["commuter_role_id"] = 通生ロール.id
    config["dorm_role_id"] = 寮生ロール.id
    await async_save_config(guild.id, config)

    await interaction.followup.send("パネルを投稿しました。", ephemeral=True)


@setup_roles.error
async def setup_roles_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "このコマンドには「ロールの管理」権限が必要です。", ephemeral=True
        )
    else:
        if interaction.response.is_done():
            await interaction.followup.send(f"エラー: {error}", ephemeral=True)
        else:
            await interaction.response.send_message(f"エラー: {error}", ephemeral=True)


async def _handle_role_reaction(payload: discord.RawReactionActionEvent, add: bool):
    if payload.guild_id is None:
        return

    config = await async_load_config(payload.guild_id)
    panel_message_id = config.get("role_panel_message_id")
    if not panel_message_id or payload.message_id != panel_message_id:
        return

    emoji = str(payload.emoji)
    if emoji not in (EMOJI_COMMUTER, EMOJI_DORM):
        return

    guild = bot.get_guild(payload.guild_id)
    if guild is None:
        return

    member = guild.get_member(payload.user_id)
    if member is None or member.bot:
        return

    commuter_role = guild.get_role(config.get("commuter_role_id"))
    dorm_role = guild.get_role(config.get("dorm_role_id"))
    channel_id = config.get("role_panel_channel_id")
    channel = guild.get_channel(channel_id) if channel_id else None

    try:
        if add:
            if emoji == EMOJI_COMMUTER and commuter_role:
                await member.add_roles(commuter_role, reason="通生登録")
                if dorm_role and dorm_role in member.roles:
                    await member.remove_roles(dorm_role, reason="通生に変更のため")
                    if channel:
                        msg = await channel.fetch_message(panel_message_id)
                        await msg.remove_reaction(EMOJI_DORM, member)
            elif emoji == EMOJI_DORM and dorm_role:
                await member.add_roles(dorm_role, reason="寮生登録")
                if commuter_role and commuter_role in member.roles:
                    await member.remove_roles(commuter_role, reason="寮生に変更のため")
                    if channel:
                        msg = await channel.fetch_message(panel_message_id)
                        await msg.remove_reaction(EMOJI_COMMUTER, member)
        else:
            if emoji == EMOJI_COMMUTER and commuter_role:
                await member.remove_roles(commuter_role, reason="通生リアクション解除")
            elif emoji == EMOJI_DORM and dorm_role:
                await member.remove_roles(dorm_role, reason="寮生リアクション解除")
    except discord.Forbidden:
        pass


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    await _handle_role_reaction(payload, add=True)


@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    await _handle_role_reaction(payload, add=False)


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
        "**/setchannel** — 通知チャンネルを設定する（通生／寮生を選択可）\n"
        "**/setup_roles** — 通生/寮生 振り分けパネルを投稿する\n"
    )
    await interaction.response.send_message(msg, ephemeral=True)

# ================================
#  自動通知
# ================================
TOMORROW_NOTIFY_CHANNEL_KEYS = ("remind_channel_id", "remind_channel_id_dorm")  # 通生・寮生 両方に送信

async def send_tomorrow_plans():
    # 実行日が金曜(4)・土曜(5) の場合は「金曜夜」「土曜夜」の通知にあたるため、
    # 予定が無ければ通知自体をスキップする
    now = datetime.now(JST)
    quiet_if_empty = now.weekday() in (4, 5)  # 4=金, 5=土
    for filename in list_all_configs():
        guild_id = int(filename.replace("config_", "").replace(".json", ""))
        config = load_config(guild_id)
        tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        plans = [p for p in load_plans(guild_id) if p["date"] == tomorrow]
        if plans:
            msg = "こんばんは！明日の予定です。\n"
            for p in plans:
                msg += f"・{p['subject']} {p['content']}\n"
        else:
            if quiet_if_empty:
                continue
            msg = "こんばんは！明日の予定はありません。\n"

        # 通生用・寮生用の両チャンネルへ、それぞれ設定されていれば送信
        for config_key in TOMORROW_NOTIFY_CHANNEL_KEYS:
            channel_id = config.get(config_key)
            if not channel_id:
                continue
            channel = bot.get_channel(channel_id)
            if not channel:
                continue
            await channel.send(msg + "@everyone")

async def send_today_plans_for(config_key: str):
    """
    朝の「今日の予定」通知を config_key で指定したチャンネル宛に送る。
    config_key: "remind_channel_id"（通生） または "remind_channel_id_dorm"（寮生）
    """
    now = datetime.now(JST)
    # 実行日が土曜(5)・日曜(6) の場合は「土曜朝」「日曜朝」の通知にあたるため、
    # 予定が無ければ通知自体をスキップする
    quiet_if_empty = now.weekday() in (5, 6)  # 5=土, 6=日
    for filename in list_all_configs():
        guild_id = int(filename.replace("config_", "").replace(".json", ""))
        config = load_config(guild_id)
        channel_id = config.get(config_key)
        if not channel_id:
            continue
        channel = bot.get_channel(channel_id)
        if not channel:
            continue
        today = now.strftime("%Y-%m-%d")
        plans = [p for p in load_plans(guild_id) if p["date"] == today]
        if plans:
            msg = "おはようございます！今日の予定です。\n"
            for p in plans:
                msg += f"・{p['subject']} {p['content']}\n"
        else:
            if quiet_if_empty:
                continue
            msg = "おはようございます！今日の予定はありません。\n"
        await channel.send(msg + "@everyone")

async def send_today_plans_commute():
    """通生向け：朝5:30の通知（既存のremind_channel_idを使用）"""
    await send_today_plans_for("remind_channel_id")

async def send_today_plans_dorm():
    """寮生向け：朝7:20の通知（remind_channel_id_dormを使用）"""
    await send_today_plans_for("remind_channel_id_dorm")

async def cleanup_past_plans():
    today = datetime.now(JST).date()
    threshold = today - timedelta(days=7)
    for filename in list_all_configs():
        guild_id = int(filename.replace("config_", "").replace(".json", ""))
        plans = load_plans(guild_id)
        deleted_dates = sorted({
            p["date"] for p in plans
            if datetime.strptime(p["date"], "%Y-%m-%d").date() < threshold
        })
        new_plans = [
            p for p in plans
            if datetime.strptime(p["date"], "%Y-%m-%d").date() >= threshold
        ]
        if deleted_dates:
            save_plans(guild_id, new_plans)
            write_log(guild_id, "cleanup", detail="削除した日付: " + ", ".join(deleted_dates))
            print(f"{guild_id} の過去予定を削除しました。")

# ================================
#  Flask API — 予定管理
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
    data     = request.json
    guild_id = data.get("guild_id")
    date     = data.get("date")
    subject  = data.get("subject")
    category = data.get("category")
    content  = data.get("content")
    points   = data.get("points")  # ★ 追加（提出・宿題のみ有効。省略時は5pt）

    if not all([guild_id, date, subject, category, content]):
        return jsonify({"ok": False, "error": "missing fields"})

    if points is not None:
        try:
            points = int(points)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "invalid points"})

    guild = bot.get_guild(int(guild_id))
    future = asyncio.run_coroutine_threadsafe(
        add_plan_internal(int(guild_id), subject, date, category, content, points),
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

@app.route("/add_study_log", methods=["POST"])
def add_study_log():
    data = request.json
    guild_id = int(data.get("guild_id"))

    entry = {
        "date": data.get("date"),
        "subject": data.get("subject"),
        "minutes": data.get("minutes"),
        "memo": data.get("memo"),
        "student_id": data.get("student_id"),
        "nickname": data.get("nickname")
    }

    logs = load_study_logs(guild_id)

    # 30日以上前のログを削除
    now = datetime.now(JST).date()
    logs = [
        l for l in logs
        if (now - datetime.strptime(l["date"], "%Y-%m-%d").date()).days <= 30
    ]

    logs.append(entry)
    save_study_logs(guild_id, logs)

    # --- ポイント加算（5分ごとに1pt） ---
    earned = entry["minutes"] // 5
    pts = load_points(guild_id)
    pts[entry["student_id"]] = pts.get(entry["student_id"], 0) + earned
    save_points(guild_id, pts)

    return jsonify({"ok": True, "earned": earned, "total": pts[entry["student_id"]]})


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
    new_points   = data.get("points")  # ★ 追加

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

    # ★ ポイント更新
    if new_points is not None:
        try:
            found["points"] = int(new_points)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "invalid points"})

    current_category = found["content"].split("】", 1)[0].lstrip("【") if "】" in found["content"] else ""
    if current_category not in POINT_CATEGORIES and "points" in found:
        # 提出・宿題以外に変更された場合はポイントを外す
        del found["points"]
    elif current_category in POINT_CATEGORIES and "points" not in found:
        found["points"] = DEFAULT_TASK_POINTS

    save_plans(guild_id, plans)
    after_str = f"{found['date']} / {found['subject']} / {found['content']}"
    write_log(guild_id, "edit", detail=f"{before_str} → {after_str}")
    if guild:
        target_channel = get_subject_channel_by_name(guild, found["subject"])
        if target_channel:
            msg = f"編集しました！\n\n【編集前】\n{before_str}\n\n【編集後】\n{after_str}"
            if "points" in found:
                msg += f"\n⭐ {found['points']}pt"
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

@app.route("/list_logs", methods=["GET"])
def list_logs():
    guild_id = request.args.get("guild_id")
    if not guild_id:
        return jsonify({"ok": False, "error": "missing guild_id"})
    logs, _ = github_get(f"logs_{guild_id}.json")
    logs = sorted(logs or [], key=lambda l: l["time"], reverse=True)
    return jsonify({"ok": True, "logs": logs})

# ================================
#  Flask API — 時間割
# ================================
def load_timetable(guild_id: int):
    data, _ = github_get(f"timetable_{guild_id}.json")
    return data or {}

def save_timetable(guild_id: int, data: dict):
    _, sha = github_get(f"timetable_{guild_id}.json")
    github_put(f"timetable_{guild_id}.json", data, sha)

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
    key      = data.get("key")
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
    key      = data.get("key")
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
#  Flask API — ユーザー認証
# ================================
@app.route("/get_users", methods=["GET"])
def get_users():
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
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

# ================================
#  Flask API — 勉強ログ
# ================================
@app.route("/list_study_logs", methods=["GET"])
def list_study_logs():
    guild_id = request.args.get("guild_id")
    if not guild_id:
        return jsonify({"ok": False, "error": "missing guild_id"})
    logs = load_study_logs(int(guild_id))
    return jsonify({"ok": True, "logs": logs})

# ================================
#  Flask API — ポイント
# ================================
@app.route("/get_points", methods=["GET"])
def get_points():
    """全ユーザーのポイント合計を返す"""
    guild_id = request.args.get("guild_id")
    if not guild_id:
        return jsonify({"ok": False, "error": "missing guild_id"})
    pts = load_points(int(guild_id))
    return jsonify({"ok": True, "points": pts})

# ================================
#  Flask API — 課題達成
# ================================
@app.route("/get_completed_tasks", methods=["GET"])
def get_completed_tasks():
    """
    student_id を指定: そのユーザーの達成済み課題リストを返す（達成日・ポイント・ニックネーム付き）
    student_id を省略: 全ユーザー分を { student_id: [...] } の形でまとめて返す
                        （週間ランキングで全員の課題達成ポイントを集計するために使用）
    """
    guild_id   = request.args.get("guild_id")
    student_id = request.args.get("student_id")  # 省略可
    if not guild_id:
        return jsonify({"ok": False, "error": "missing params"})

    tasks = load_completed_tasks(int(guild_id))

    if student_id:
        raw = tasks.get(student_id, [])
        normalized = [_normalize_task_entry(e) for e in raw]
        return jsonify({"ok": True, "done": normalized})

    # student_id 省略 → 全員分をまとめて返す
    all_normalized = {
        sid: [_normalize_task_entry(e) for e in raw]
        for sid, raw in tasks.items()
    }
    return jsonify({"ok": True, "done": all_normalized})


@app.route("/complete_task", methods=["POST"])
def complete_task():
    data       = request.json
    guild_id   = int(data.get("guild_id"))
    student_id = data.get("student_id")
    task_id    = data.get("task_id")
    points     = int(data.get("points"))
    nickname   = data.get("nickname")  # ★ ニックネームを受け取る

    # --- 達成済み課題保存（達成日・ポイント・ニックネーム付き） ---
    done = load_completed_tasks(guild_id)
    if student_id not in done:
        done[student_id] = []

    # 既存エントリを正規化したうえで重複チェック
    normalized = [_normalize_task_entry(e) for e in done[student_id]]
    existing_ids = [e["id"] for e in normalized]

    if task_id not in existing_ids:
        normalized.append({
            "id":       task_id,
            "date":     str(_date.today()),
            "points":   points,
            "nickname": nickname,  # ★ ニックネームを保存
        })

    done[student_id] = normalized
    save_completed_tasks(guild_id, done)

    # --- ポイント加算 ---
    pts = load_points(guild_id)
    pts[student_id] = pts.get(student_id, 0) + points
    save_points(guild_id, pts)

    return jsonify({"ok": True, "total": pts[student_id]})

# ================================
#  Flask API — 単語カード
# ================================
CARDS_DIR = "words"

def list_card_files():
    url     = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{CARDS_DIR}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}"}
    r = requests.get(url, headers=headers)
    if r.status_code == 404:
        return []
    files = r.json()
    return [f for f in files if isinstance(f, dict) and f["name"].endswith(".json")]

def get_card_file(filename):
    data, sha = github_get(f"{CARDS_DIR}/{filename}")
    return data, sha

def put_card_file(filename, content_obj, sha=None):
    github_put(f"{CARDS_DIR}/{filename}", content_obj, sha)

def generate_card_filename():
    import random, string
    now   = datetime.now(JST)
    date  = now.strftime("%Y%m%d")
    time_ = now.strftime("%H%M")
    rand  = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"set_{date}_{time_}_{rand}.json"

@app.route("/list_cards", methods=["GET"])
def list_cards():
    try:
        files  = list_card_files()
        result = []
        for f in files:
            data, _ = github_get(f"{CARDS_DIR}/{f['name']}")
            if data is None:
                continue
            cards = data.get("cards", [])
            result.append({
                "filename": f["name"],
                "name":     data.get("name", f["name"]),
                "cards":    cards,
                "count":    len(cards),
                "subject":  data.get("subject"),
                "published_by": (data.get("published_by") or {}).get("nickname"),
            })
        return jsonify({"ok": True, "sets": result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/save_cards", methods=["POST"])
def save_cards():
    data     = request.json
    name     = data.get("name")
    cards    = data.get("cards")
    filename = data.get("filename")
    guild_id = data.get("guild_id")
    subject  = data.get("subject")
    publisher_id       = data.get("publisher_id")
    publisher_nickname = data.get("publisher_nickname") or "匿名"
    silent   = data.get("silent", False)  # ★ 追加：trueなら通知しない

    if not name or not isinstance(cards, list):
        return jsonify({"ok": False, "error": "name と cards は必須です"})

    is_update = bool(filename)
    if not filename:
        filename = generate_card_filename()

    sha = None
    if is_update:
        _, sha = get_card_file(filename)

    put_card_file(filename, {
        "name": name,
        "cards": cards,
        "subject": subject,
        "published_by": {
            "id": publisher_id,
            "nickname": publisher_nickname,
        },
    }, sha)

    # --- Discord通知（silentがtrueならスキップ） ---
    if guild_id and not silent:
        try:
            guild_id_int = int(guild_id)
            guild = bot.get_guild(guild_id_int)
            if guild:
                action = "更新" if is_update else "公開"
                msg = f"📇 単語カード「{name}」が{publisher_nickname}さんによって{action}されました！（{len(cards)}問）"

                target_channel = get_subject_channel_by_name(guild, subject) if subject else None
                if not target_channel:
                    config = load_config(guild_id_int)
                    channel_id = config.get("remind_channel_id")
                    target_channel = bot.get_channel(channel_id) if channel_id else None

                if target_channel:
                    asyncio.run_coroutine_threadsafe(
                        target_channel.send(msg), bot.loop
                    ).result(timeout=10)
        except Exception as e:
            print(f"[WARN] save_cards notify failed: {e}")

    return jsonify({"ok": True, "filename": filename, "is_update": is_update})

@app.route("/delete_cards", methods=["POST"])
def delete_cards():
    data     = request.json
    filename = data.get("filename")
    if not filename:
        return jsonify({"ok": False, "error": "filename は必須です"})
    url     = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{CARDS_DIR}/{filename}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}"}
    r = requests.get(url, headers=headers)
    if r.status_code == 404:
        return jsonify({"ok": False, "error": "ファイルが見つかりません"})
    sha = r.json().get("sha")
    requests.delete(url, headers=headers, json={"message": f"delete {filename}", "sha": sha})
    return jsonify({"ok": True})

# ================================
#  スケジューラー & 起動
# ================================
scheduler.add_job(send_tomorrow_plans,     "cron", hour=20, minute=0)
scheduler.add_job(send_today_plans_commute, "cron", hour=5,  minute=30)  # 通生（現行時間）
scheduler.add_job(send_today_plans_dorm,    "cron", hour=7,  minute=20)  # 寮生
scheduler.add_job(cleanup_past_plans,       "cron", hour=0,  minute=0)

started = False

@bot.event
async def on_ready():
    global started
    print(f"Bot is ready! {bot.user}")
    synced = await bot.tree.sync()
    print(f"Synced {len(synced)} commands")
    if not started:
        scheduler.start()
        started = True
        print("Scheduler started!")

keep_alive()

print(f"[INFO] TOKEN set: {bool(TOKEN)}, length: {len(TOKEN) if TOKEN else 0}")
print(f"[INFO] Starting bot.run()...")

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
