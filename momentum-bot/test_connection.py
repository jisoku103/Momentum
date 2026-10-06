import os
import discord
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID"))
CHANNELS = {
    "today_focus": int(os.getenv("CH_TODAY_FOCUS")),
    "task_inbox": int(os.getenv("CH_TASK_INBOX")),
    "backlog": int(os.getenv("CH_BACKLOG")),
    "overdue_tasks": int(os.getenv("CH_OVERDUE_TASKS")),
    "done_log": int(os.getenv("CH_DONE_LOG")),
}

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    print(f"Logged in as {client.user.name} ({client.user.id})")
    guild = client.get_guild(GUILD_ID)
    if not guild:
        print(f"Error: Guild ID {GUILD_ID} not found.")
        await client.close()
        return

    print(f"Guild found: {guild.name}")
    for name, ch_id in CHANNELS.items():
        channel = guild.get_channel(ch_id)
        status = "OK" if channel else "NOT FOUND"
        print(f"  Channel [{name}]: {status} (ID: {ch_id})")

    await client.close()

if __name__ == "__main__":
    client.run(TOKEN)