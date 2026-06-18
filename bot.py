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

GITHUB_REPO = "yuichisana377/python.bot.1istudy"  # ←あなたのリポジトリ名
GITHUB_FILE = "plans.json"
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")  # Render の環境変数から読み込む


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

TOKEN = TOKEN = os.getenv("TOKEN")


intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


# ================================
#  ギルドごとの設定
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

    payload = {
        "message": f"update {filename}",
        "content": new_content
    }

    if sha:
        payload["sha"] = sha

    requests.put(url, headers=headers, json=payload)

def list_all_configs():
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents"
    headers = {"Authorization": f"token {GITHUB_TOKEN}"}

    r = requests.get(url, headers=headers)
    files = r.json()

    config_files = [
        f["name"] for f in files
        if f["name"].startswith("config_") and f["name"].endswith(".json")
    ]
    return config_files

# ================================
#  予定データ（ギルドごと）
# ================================
def load_plans(guild_id: int):
    filename = f"plans_{guild_id}.json"
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}"}

    r = requests.get(url, headers=headers)

    # ファイルが存在しない場合 → 空のリストを返す
    if r.status_code == 404:
        return []

    data = r.json()
    content = base64.b64decode(data["content"]).decode()
    return json.loads(content)


def save_plans(guild_id: int, plans: list):
    filename = f"plans_{guild_id}.json"
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}"}

    # まず現在のファイルの SHA を取得（新規の場合は None）
    r = requests.get(url, headers=headers)

    if r.status_code == 404:
        sha = None
    else:
        sha = r.json()["sha"]

    new_content = base64.b64encode(
        json.dumps(plans, ensure_ascii=False, indent=2).encode()
    ).decode()

    data = {
        "message": f"update {filename}",
        "content": new_content,
    }

    if sha:
        data["sha"] = sha

    requests.put(url, headers=headers, json=data)

# ================================
#  /add（category 自由入力 + 候補表示）
# ================================
@bot.tree.command(
    name="add",
    description="日にち・内容を登録する（科目名はチャンネル名）"
)
@app_commands.describe(
    date="日付（例: 6-20, 06/20, 2026-06-20）",
    category="分類（宿題・提出・持ち物など自由入力OK）",
    content="内容（宿題など）"
)
async def add_plan(interaction: discord.Interaction, date: str, category: str, content: str):

    # --- 日付処理（年なし対応） ---
    try:
        # 年あり（YYYY-MM-DD）
        if "-" in date and len(date.split("-")[0]) == 4:
            parsed = datetime.strptime(date, "%Y-%m-%d")

        # 年なし（MM-DD, M-D, MM/DD, M/D）
        else:
            date = date.replace("/", "-")
            month, day = date.split("-")
            year = datetime.now().year
            parsed = datetime.strptime(
                f"{year}-{int(month):02d}-{int(day):02d}",
                "%Y-%m-%d"
            )

    except ValueError:
        await interaction.response.send_message(
            "日付の形式が正しくありません（例: 6-20, 06/20, 2026-06-20）",
            ephemeral=True
        )
        return

    # YYYY-MM-DD に整形
    date = parsed.strftime("%Y-%m-%d")

    # --- 過去の日付は登録不可 ---
    today = datetime.now().date()
    input_date = datetime.strptime(date, "%Y-%m-%d").date()

    if input_date < today:
        await interaction.response.send_message(
            "過去の日付は登録できません！",
            ephemeral=True
        )
        return

    # --- 科目名はチャンネル名 ---
    subject = interaction.channel.name

    # --- content にカテゴリタグを付ける ---
    tagged_content = f"【{category}】{content}"

    # --- 保存 ---
    guild_id = interaction.guild.id
    plans = load_plans(guild_id)

    plans.append({
        "date": date,
        "subject": subject,
        "content": tagged_content
    })

    save_plans(guild_id, plans)

    await interaction.response.send_message(
        f"登録しました！\n**{date} / {subject} / {tagged_content}**"
    )


@add_plan.autocomplete("category")
async def category_autocomplete(interaction: discord.Interaction, current: str):

    candidates = ["宿題", "提出", "持ち物", "テスト", "その他"]

    return [
        app_commands.Choice(name=c, value=c)
        for c in candidates
        if current in c
    ][:25]



# ================================
#  /list
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
            msg += f"- {p['date']}：{p['subject']}{p['content']}\n"

        await interaction.response.send_message(msg, ephemeral=True)
        return

    # 日付指定
    try:
        if "/" in date:
            m, d = date.split("/")
            y = datetime.now().year
            date_str = f"{y}-{int(m):02d}-{int(d):02d}"
        else:
            datetime.strptime(date, "%Y-%m-%d")
            date_str = date
    except:
        await interaction.response.send_message("日付の形式が正しくありません！", ephemeral=True)
        return

    selected = [p for p in plans if p["date"] == date_str]

    if not selected:
        await interaction.response.send_message(f"{date} の予定はありません。", ephemeral=True)
        return

    msg = f"📘 **{date} の予定**\n"
    for p in selected:
        msg += f"- {p['subject']}{p['content']}\n"

    await interaction.response.send_message(msg, ephemeral=True)


# ================================
#  /delete
# ================================
@bot.tree.command(name="delete", description="予定を削除する")
@app_commands.describe(target="削除したい予定を選んでください")
async def delete(interaction: discord.Interaction, target: str):

    guild_id = interaction.guild.id
    plans = load_plans(guild_id)

    new_plans = [
        p for p in plans
        if f"{p['date']}/{p['subject']}{p['content']}" != target
    ]

    if len(new_plans) == len(plans):
        await interaction.response.send_message("その予定は見つかりませんでした。", ephemeral=True)
        return

    save_plans(guild_id, new_plans)

    await interaction.response.send_message(f"削除しました！\n{target}")


@delete.autocomplete("target")
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
#  /cleanup
# ================================
@bot.tree.command(name="cleanup", description="過去の予定を削除する")
async def cleanup_command(interaction: discord.Interaction):

    guild_id = interaction.guild.id
    plans = load_plans(guild_id)

    today = datetime.now().strftime("%Y-%m-%d")
    new_plans = [p for p in plans if p["date"] >= today]

    deleted = len(plans) - len(new_plans)
    save_plans(guild_id, new_plans)

    if deleted > 0:
        await interaction.response.send_message(f"🧹 {deleted} 件削除しました！", ephemeral=True)
    else:
        await interaction.response.send_message("削除する予定はありませんでした！", ephemeral=True)


# ================================
#  通知（全サーバー対応）
# ================================
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
#  /edit
# ================================
@bot.tree.command(name="edit", description="登録済みの予定を編集する（変更したい部分だけ入力）")
@app_commands.describe(
    target="編集したい予定を選んでください",
    date="新しい日付（変更したい場合だけ）",
    category="新しい分類（宿題・提出・持ち物など自由入力OK）",
    content="新しい内容（変更したい場合だけ）"
)
async def edit_plan(
    interaction: discord.Interaction,
    target: str,
    date: str = None,
    category: str = None,
    content: str = None
):

    guild_id = interaction.guild.id
    plans = load_plans(guild_id)

    # 対象を検索
    found = None
    for p in plans:
        label = f"{p['date']}/{p['subject']}{p['content']}"
        if label == target:
            found = p
            break

    if not found:
        await interaction.response.send_message("その予定が見つかりませんでした。", ephemeral=True)
        return

    
    #  日付変更
    
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

    
    #  category + content 変更
    
    if category and content:
        found["content"] = f"【{category}】{content}"

    
    #  category だけ変更
    
    elif category and not content:
        old = found["content"]
        body = old.split("】", 1)[1] if "】" in old else old
        found["content"] = f"【{category}】{body}"

    
    #  content だけ変更
    
    elif content and not category:
        old = found["content"]
        tag = old.split("】", 1)[0] + "】" if "】" in old else ""
        found["content"] = f"{tag}{content}"

    save_plans(guild_id, plans)

    await interaction.response.send_message(
        f"編集したよ！\n**{found['date']} / {found['subject']} / {found['content']}**"
    )



#  autocomplete（予定選択）

@edit_plan.autocomplete("target")
async def edit_autocomplete(interaction: discord.Interaction, current: str):

    guild_id = interaction.guild.id
    plans = load_plans(guild_id)

    choices = []
    for p in plans:
        label = f"{p['date']}/{p['subject']}{p['content']}"
        if current in label:
            choices.append(app_commands.Choice(name=label, value=label))

    return choices[:25]



#  category の autocomplete（選択式 + 自由入力OK）

@edit_plan.autocomplete("category")
async def category_autocomplete(interaction: discord.Interaction, current: str):

    candidates = ["宿題", "提出", "持ち物", "テスト", "その他"]

    return [
        app_commands.Choice(name=c, value=c)
        for c in candidates
        if current in c
    ][:25]





# ================================
#  自動 cleanup（全サーバー）
# ================================
async def cleanup_past_plans():
    config_files = list_all_configs()

    for filename in config_files:
        guild_id = int(filename.replace("config_", "").replace(".json", ""))

        plans = load_plans(guild_id)
        today = datetime.now().strftime("%Y-%m-%d")

        new_plans = [p for p in plans if p["date"] >= today]

        if len(new_plans) != len(plans):
            save_plans(guild_id, new_plans)
            print(f"{guild_id} の過去予定を削除しました。")
ko

# ================================
#  /setchannel
# ================================
@bot.tree.command(name="setchannel", description="通知を送るチャンネルを設定する")
async def setchannel(interaction: discord.Interaction):

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
@bot.tree.command(name="help", description="使えるコマンド一覧を表示します。")
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
