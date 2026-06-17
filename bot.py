import discord
from discord import app_commands
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import datetime, timedelta
import json
import os
from flask import Flask
from threading import Thread

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

scheduler = AsyncIOScheduler()


# ================================
#  ギルドごとの設定
# ================================
def load_config(guild_id: int):
    path = f"config_{guild_id}.json"
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_config(guild_id: int, data: dict):
    path = f"config_{guild_id}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)


# ================================
#  予定データ（ギルドごと）
# ================================
def load_plans(guild_id: int):
    filename = f"plans_{guild_id}.json"
    if not os.path.exists(filename):
        return []
    with open(filename, "r", encoding="utf-8") as f:
        return json.load(f)

def save_plans(guild_id: int, plans: list):
    filename = f"plans_{guild_id}.json"
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(plans, f, ensure_ascii=False, indent=4)


# ================================
#  起動時
# ================================
@bot.event
async def on_ready():
    print("Bot is ready!")
    await bot.tree.sync()
    print("Slash commands synced.")

    scheduler.start()
    scheduler.add_job(send_tomorrow_plans, "cron", hour=20, minute=0)
    scheduler.add_job(send_today_plans, "cron", hour=6, minute=30)
    scheduler.add_job(cleanup_past_plans, "cron", hour=0, minute=0)


# ================================
#  /add
# ================================
@bot.tree.command(name="add", description="日にち・内容を登録する（科目名はチャンネル名）")
@app_commands.describe(
    date="日付（例: 6-20, 06/20, 2026-06-20）",
    category="分類を選んでね（宿題・提出・持ち物など）",
    content="内容（宿題など）"
)
@app_commands.choices(
    category=[
        app_commands.Choice(name="宿題", value="宿題"),
        app_commands.Choice(name="提出", value="提出"),
        app_commands.Choice(name="持ち物", value="持ち物"),
        app_commands.Choice(name="テスト", value="テスト"),
        app_commands.Choice(name="その他", value="その他"),
    ]
)
async def add_plan(interaction: discord.Interaction, date: str, category: app_commands.Choice[str], content: str):

    # 日付処理
    try:
        if "-" in date and len(date.split("-")[0]) == 4:
            parsed = datetime.strptime(date, "%Y-%m-%d")
        else:
            date = date.replace("/", "-")
            month, day = date.split("-")
            year = datetime.now().year
            parsed = datetime.strptime(f"{year}-{int(month):02d}-{int(day):02d}", "%Y-%m-%d")
    except:
        await interaction.response.send_message("日付の形式が正しくないよ！", ephemeral=True)
        return

    date = parsed.strftime("%Y-%m-%d")

    # 過去日付禁止
    if datetime.strptime(date, "%Y-%m-%d").date() < datetime.now().date():
        await interaction.response.send_message("過去の日付は登録できないよ！", ephemeral=True)
        return

    subject = interaction.channel.name
    tagged_content = f"【{category.value}】{content}"

    guild_id = interaction.guild.id
    plans = load_plans(guild_id)

    plans.append({
        "date": date,
        "subject": subject,
        "content": tagged_content
    })

    save_plans(guild_id, plans)

    await interaction.response.send_message(
        f"登録したよ！\n**{date} / {subject} / {tagged_content}**"
    )


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
        await interaction.response.send_message("日付の形式が正しくないよ！", ephemeral=True)
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
@app_commands.describe(target="削除したい予定を選んでね")
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
    for filename in os.listdir():
        if filename.startswith("config_") and filename.endswith(".json"):
            guild_id = int(filename.replace("config_", "").replace(".json", ""))

            config = load_config(guild_id)
            channel_id = config.get("remind_channel_id")
            if not channel_id:
                continue

            channel = bot.get_channel(channel_id)
            if not channel:
                continue

            plans = load_plans(guild_id)
            tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
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
    for filename in os.listdir():
        if filename.startswith("config_") and filename.endswith(".json"):
            guild_id = int(filename.replace("config_", "").replace(".json", ""))

            config = load_config(guild_id)
            channel_id = config.get("remind_channel_id")
            if not channel_id:
                continue

            channel = bot.get_channel(channel_id)
            if not channel:
                continue

            plans = load_plans(guild_id)
            today = datetime.now().strftime("%Y-%m-%d")
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
#  自動 cleanup（全サーバー）
# ================================
async def cleanup_past_plans():
    for filename in os.listdir():
        if filename.startswith("plans_") and filename.endswith(".json"):
            guild_id = int(filename.replace("plans_", "").replace(".json", ""))

            plans = load_plans(guild_id)
            today = datetime.now().strftime("%Y-%m-%d")

            new_plans = [p for p in plans if p["date"] >= today]

            if len(new_plans) != len(plans):
                save_plans(guild_id, new_plans)
                print(f"{guild_id} の過去予定を削除しました。")


# ================================
#  /setchannel
# ================================
@bot.tree.command(name="setchannel", description="通知を送るチャンネルを設定する")
@app_commands.describe(channel="通知を送るチャンネル")
async def setchannel(interaction: discord.Interaction, channel: discord.TextChannel):

    guild_id = interaction.guild.id
    config = load_config(guild_id)

    config["remind_channel_id"] = channel.id
    save_config(guild_id, config)

    await interaction.response.send_message(
        f"通知チャンネルを **#{channel.name}** に設定したよ！"
    )


# ================================
#  /help
# ================================
@bot.tree.command(name="help", description="使えるコマンド一覧を表示するよ")
async def help_command(interaction: discord.Interaction):

    msg = (
        "📘 **使えるコマンド一覧**\n\n"
        "**/add** — 予定を登録する\n"
        "**/list** — 予定を表示する\n"
        "**/delete** — 予定を削除する\n"
        "**/cleanup** — 過去の予定を削除する\n"
        "**/setchannel** — 通知チャンネルを設定する\n"
    )

    await interaction.response.send_message(msg, ephemeral=True)
keep_alive()

bot.run(TOKEN)
