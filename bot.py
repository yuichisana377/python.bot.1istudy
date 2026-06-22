import discord
from discord.ext import commands
from flask import Flask
from threading import Thread
import os

TOKEN = os.getenv("TOKEN")

# Flask（Render用）
app = Flask("")

@app.route("/")
def home():
    return "Bot is running!"

def run_flask():
    app.run(host="0.0.0.0", port=10000)

def keep_alive():
    t = Thread(target=run_flask)
    t.start()

# Discord Bot
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ---- ここが重要（Slash コマンド同期） ----
@bot.event
async def on_ready():
    print(f"Bot is ready! Logged in as {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} commands")
    except Exception as e:
        print(f"Sync error: {e}")

# ---- テスト用 Slash コマンド ----
@bot.tree.command(name="test", description="テストコマンド")
async def test(interaction: discord.Interaction):
    await interaction.response.send_message("Slash command is working!")

# ------------------------------------

keep_alive()
bot.run(TOKEN)
