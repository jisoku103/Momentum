"""Slash commands Cog for Momentum Bot.

Complies with Section 10.1 and Section 10.2 of the Momentum specification (v17).
Implements:
  - /undo
  - /reset-day
  - /refresh
All commands follow strict ephemeral-first convention: defer(ephemeral=True) -> followup.send().
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING
import discord
from discord import app_commands
from discord.ext import commands

from src.core.lock import db_lock_context
from src.core.state_engine import determine_evacuation_status
from src.core.undo_engine import UndoEngine
from src.db.connection import transaction
from src.db.repository import TaskRepository

if TYPE_CHECKING:
    from src.bot.client import MomentumBot


class SlashCommandsCog(commands.Cog):
    """Cog handling guild slash commands."""

    def __init__(self, bot: "MomentumBot") -> None:
        self.bot = bot

    async def _check_owner(self, interaction: discord.Interaction) -> bool:
        """Verify that user is the authorized single owner (TC-024)."""
        if interaction.user.id != self.bot.config.OWNER_USER_ID:
            await interaction.followup.send("このBotは個人用です", ephemeral=True)
            return False
        return True

    @app_commands.command(name="undo", description="直前の操作または日次リセットを取り消します")
    async def cmd_undo(self, interaction: discord.Interaction) -> None:
        """Rollback latest operation batch (Section 10.1). Allowed in #task-inbox and #today-focus."""
        await interaction.response.defer(ephemeral=True)
        if not await self._check_owner(interaction):
            return

        # Channel check
        allowed_channels = (self.bot.config.CH_TASK_INBOX, self.bot.config.CH_TODAY_FOCUS)
        if interaction.channel_id not in allowed_channels:
            await interaction.followup.send(
                "このコマンドは #task-inbox または #today-focus で実行してね！",
                ephemeral=True,
            )
            return

        async with db_lock_context():
            async with self.bot.get_db() as conn:
                undo_engine = UndoEngine(conn)
                async with transaction(conn):
                    result = await undo_engine.execute_undo()

            if result.success:
                # Cleanup any rolled back done-log messages
                for op in result.operations:
                    if op.done_log_message_id_to_delete:
                        await self.bot.message_manager.delete_done_log_message(
                            op.done_log_message_id_to_delete
                        )
                # Re-render screens
                await self.bot.message_manager.render_all_pinned()

        await interaction.followup.send(result.message, ephemeral=True)

    @app_commands.command(name="reset-day", description="Today のスロットを手動リセットします")
    async def cmd_reset_day(self, interaction: discord.Interaction) -> None:
        """Evacuate incomplete Today tasks and reset slots (Section 10.1). Allowed in #task-inbox."""
        await interaction.response.defer(ephemeral=True)
        if not await self._check_owner(interaction):
            return

        # Channel check
        if interaction.channel_id != self.bot.config.CH_TASK_INBOX:
            await interaction.followup.send(
                "このコマンドは #task-inbox で実行してね！",
                ephemeral=True,
            )
            return

        async with db_lock_context():
            async with self.bot.get_db() as conn:
                task_repo = TaskRepository(conn)
                undo_engine = UndoEngine(conn)
                today_tasks = await task_repo.get_today_tasks()

                # If Today is empty: no Undo history created
                if not today_tasks:
                    await interaction.followup.send("Today はすでに空だよ", ephemeral=True)
                    return

                batch_id = str(uuid.uuid4())
                action_logs = []

                async with transaction(conn):
                    for task in today_tasks:
                        dest_status = determine_evacuation_status(task.due_date)
                        prev_state = task.to_dict()
                        await task_repo.update_task(
                            task.id,
                            status=dest_status,
                            slot_index=None,
                            today_since=None,
                        )
                        action_logs.append({
                            "task_id": task.id,
                            "action_type": "reset_day",
                            "previous_state": prev_state,
                        })

                    await undo_engine.record_batch(batch_id, action_logs)

            # Re-render pinned screens
            await self.bot.message_manager.render_all_pinned()

        # Send public announcement in #task-inbox
        inbox_channel = self.bot.get_channel(self.bot.config.CH_TASK_INBOX)
        if inbox_channel:
            await inbox_channel.send("スロットを手動リセットしたよ！未完了タスクを控え室に退避しました。")

        # Ephemeral completion confirmation to command invoker
        await interaction.followup.send("スロットのリセットが完了したよ ✨", ephemeral=True)

    @app_commands.command(name="refresh", description="固定親メッセージと画面を最新の状態に更新します")
    async def cmd_refresh(self, interaction: discord.Interaction) -> None:
        """Clean 14-day history and refresh pinned screens (Section 10.1). Allowed in all 5 channels."""
        await interaction.response.defer(ephemeral=True)
        if not await self._check_owner(interaction):
            return

        async with db_lock_context():
            # Force recreate & clean history for all 3 pinned channels
            await self.bot.message_manager.render_all_pinned(force_cleanup=True)

        await interaction.followup.send("画面を最新の状態に更新したよ ✨", ephemeral=True)


async def setup(bot: "MomentumBot") -> None:
    await bot.add_cog(SlashCommandsCog(bot))
