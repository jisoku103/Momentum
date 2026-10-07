"""Custom discord.py Bot client for Momentum.

Complies with Section 1.1, Section 8, and acceptance test cases TC-022 through TC-024.
Coordinates:
  - Privileged intents (Guilds, GuildMessages, MessageContent)
  - Persistent custom_id prefix routing in on_interaction (reboot resilience; TC-023)
  - Guild-scoped slash command synchronization
  - MessageManager and GeminiClient binding
"""

from __future__ import annotations

import logging
from typing import AsyncIterator, Optional
import aiosqlite
import discord
from discord.ext import commands

from src.bot.renderers.message_manager import MessageManager
from src.bot.views.backlog_view import handle_backlog_interaction
from src.bot.views.overdue_view import handle_overdue_interaction
from src.bot.views.today_view import handle_today_interaction
from src.config import Config
from src.db.connection import get_connection, init_db
from src.gemini.client import GeminiClient
from src.jobs.catchup import execute_catchup
from src.jobs.morning_brief import handle_brief_interaction
from src.jobs.scheduler import JobScheduler

logger = logging.getLogger(__name__)


class MomentumBot(commands.Bot):
    """Core Bot client for Momentum ToDo assistant."""

    def __init__(self, config: Config) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.guild_messages = True
        intents.message_content = True  # Required privileged intent (Section 1.1)

        super().__init__(
            command_prefix="!",
            intents=intents,
        )
        self.config = config
        self.gemini_client = GeminiClient(config)
        self.message_manager = MessageManager(self)
        self.scheduler = JobScheduler(self)

    def get_db(self) -> AsyncIterator[aiosqlite.Connection]:
        """Convenience helper to yield a configured DB connection."""
        return get_connection(self.config.DB_PATH)

    async def setup_hook(self) -> None:
        """Initialize database, load cogs, and sync guild slash commands."""
        # 1. Initialize SQLite database schema
        await init_db(self.config.DB_PATH)

        # 2. Load Cogs
        await self.load_extension("src.bot.cogs.inbox_cog")
        await self.load_extension("src.bot.cogs.slash_commands")

        # 3. Synchronize slash commands to target guild
        guild_obj = discord.Object(id=self.config.GUILD_ID)
        self.tree.copy_global_to(guild=guild_obj)
        await self.tree.sync(guild=guild_obj)
        logger.info(f"Slash command tree synchronized to guild {self.config.GUILD_ID}")

    async def on_ready(self) -> None:
        """Called when bot successfully connects and caches are ready."""
        logger.info(f"Logged in as {self.user.name} ({self.user.id})")
        # 1. Execute startup catchup and done-log recovery (Section 7.4)
        try:
            catchup_result = await execute_catchup(self)
            logger.info(f"Startup catchup completed: {catchup_result}")
        except Exception as e:
            logger.error(f"Failed to execute startup catchup: {e}")

        # 2. Render or self-heal pinned screens in fixed 3 channels on startup (Section 8.4)
        try:
            await self.message_manager.render_all_pinned()
        except Exception as e:
            logger.error(f"Failed to render pinned screens on ready: {e}")

        # 3. Start background periodic monitoring loop
        self.scheduler.start()

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        """Global interaction listener routing component interactions by custom_id prefix (Section 8).

        Ensures full reboot resilience (TC-023) without requiring re-creating Views in memory.
        """
        # Only process component interactions (buttons and select menus)
        if interaction.type == discord.InteractionType.component:
            custom_id = interaction.data.get("custom_id", "")

            # Route by prefix
            if custom_id.startswith("btn:today:") or custom_id == "btn:today:undo":
                await handle_today_interaction(self, interaction, custom_id)
                return

            if custom_id.startswith("btn:backlog:") or custom_id.startswith("select:backlog:"):
                await handle_backlog_interaction(self, interaction, custom_id)
                return

            if custom_id.startswith("btn:overdue:") or custom_id.startswith("select:overdue:"):
                await handle_overdue_interaction(self, interaction, custom_id)
                return

            if custom_id.startswith("btn:brief:promote:"):
                await handle_brief_interaction(self, interaction, custom_id)
                return

        # Let discord.py process slash commands and other interactions normally
        await super().on_interaction(interaction)
