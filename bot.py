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

@bot.event
async def on_ready():
    print(f"Bot is ready! Logged in as {bot.user}")

keep_alive()
bot.run(TOKEN)
