"""Parent message renderer, history cleanup, and recovery manager.

Complies with Section 8.4 and Section 8.5 of the Momentum specification (v17).
Handles:
  - Pinned parent message rendering via message.edit()
  - Automatic self-healing upon discord.NotFound (404)
  - 14-day history cleanup (purge within 14 days, max 5 single deletes beyond 14 days)
  - #done-log posting with At-Least Once error tolerance
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING, Dict, List, Optional
import discord

from src.bot.renderers.embeds import (
    create_backlog_embed,
    create_overdue_embed,
    create_today_embed,
    format_done_log_message,
)
from src.bot.views.backlog_view import BacklogView
from src.bot.views.overdue_view import OverdueView
from src.bot.views.today_view import TodayFocusView
from src.core.lock import db_lock_context
from src.db.repository import BotMessageRepository, TaskRecord, TaskRepository

if TYPE_CHECKING:
    from src.bot.client import MomentumBot

logger = logging.getLogger(__name__)


class MessageManager:
    """Manages rendering, cleanup, and self-healing of pinned messages in the 3 fixed channels."""

    def __init__(self, bot: "MomentumBot") -> None:
        self.bot = bot
        # Page tracking in memory (Section 8.2 & 8.3)
        self._backlog_page = 1
        self._overdue_page = 1

    def change_backlog_page(self, delta: int) -> None:
        self._backlog_page += delta

    def change_overdue_page(self, delta: int) -> None:
        self._overdue_page += delta

    async def render_all_pinned(self, force_cleanup: bool = False) -> None:
        """Re-render all 3 pinned channels with individual error isolation."""
        for name, render_func in (
            ("today_focus", self.render_today_focus),
            ("backlog", self.render_backlog),
            ("overdue", self.render_overdue),
        ):
            try:
                await render_func(force_cleanup=force_cleanup)
            except discord.Forbidden as e:
                logger.error(
                    f"Permission denied rendering {name} screen (code: {e.code}): "
                    f"Please ensure Bot has 'Send Messages' and 'Embed Links' in the channel."
                )
            except Exception as e:
                logger.error(f"Failed to render {name} screen: {e}", exc_info=True)

    async def render_today_focus(self, force_cleanup: bool = False) -> None:
        """Render the pinned parent message in #today-focus."""
        channel_id = self.bot.config.CH_TODAY_FOCUS
        channel = self.bot.get_channel(channel_id)
        if not channel:
            return

        async with self.bot.get_db() as conn:
            task_repo = TaskRepository(conn)
            today_slots = await task_repo.get_today_slots()
            today_done, total_done = await task_repo.get_completed_counts(
                day_boundary_hour=self.bot.config.DAY_BOUNDARY_HOUR
            )

        embed = create_today_embed(
            today_slots=today_slots,
            today_done_count=today_done,
            total_done_count=total_done,
        )
        view = TodayFocusView(self.bot, today_slots)

        await self._update_or_recreate(
            key="today_focus",
            channel=channel,
            embed=embed,
            view=view,
            force_cleanup=force_cleanup,
        )

    async def render_backlog(self, force_cleanup: bool = False) -> None:
        """Render the pinned parent message in #backlog with pagination."""
        channel_id = self.bot.config.CH_BACKLOG
        channel = self.bot.get_channel(channel_id)
        if not channel:
            return

        async with self.bot.get_db() as conn:
            task_repo = TaskRepository(conn)
            all_backlog = await task_repo.get_backlog_tasks()

        page_size = 8
        total_tasks = len(all_backlog)
        total_pages = max(1, (total_tasks + page_size - 1) // page_size)

        # Page correction: min(current_page, max(1, total_pages)) (Section 8.2)
        self._backlog_page = min(self._backlog_page, total_pages)
        self._backlog_page = max(1, self._backlog_page)

        start_idx = (self._backlog_page - 1) * page_size
        page_tasks = all_backlog[start_idx : start_idx + page_size]

        embed = create_backlog_embed(page_tasks, self._backlog_page, total_pages)
        view = BacklogView(self.bot, page_tasks, self._backlog_page, total_pages) if all_backlog else None

        await self._update_or_recreate(
            key="backlog",
            channel=channel,
            embed=embed,
            view=view,
            force_cleanup=force_cleanup,
        )

    async def render_overdue(self, force_cleanup: bool = False) -> None:
        """Render the pinned parent message in #overdue-tasks with pagination."""
        channel_id = self.bot.config.CH_OVERDUE_TASKS
        channel = self.bot.get_channel(channel_id)
        if not channel:
            return

        async with self.bot.get_db() as conn:
            task_repo = TaskRepository(conn)
            all_overdue = await task_repo.get_overdue_tasks()

        page_size = 8
        total_tasks = len(all_overdue)
        total_pages = max(1, (total_tasks + page_size - 1) // page_size)

        self._overdue_page = min(self._overdue_page, total_pages)
        self._overdue_page = max(1, self._overdue_page)

        start_idx = (self._overdue_page - 1) * page_size
        page_tasks = all_overdue[start_idx : start_idx + page_size]

        embed = create_overdue_embed(page_tasks, self._overdue_page, total_pages)
        view = OverdueView(self.bot, page_tasks, self._overdue_page, total_pages) if all_overdue else None

        await self._update_or_recreate(
            key="overdue_tasks",
            channel=channel,
            embed=embed,
            view=view,
            force_cleanup=force_cleanup,
        )

    async def _update_or_recreate(
        self,
        key: str,
        channel: discord.TextChannel,
        embed: discord.Embed,
        view: Optional[discord.ui.View],
        force_cleanup: bool = False,
    ) -> None:
        """Update existing parent message or self-heal by cleaning history and reposting (TC-022)."""
        async with self.bot.get_db() as conn:
            bot_msg_repo = BotMessageRepository(conn)
            record = await bot_msg_repo.get_message(key)

        existing_msg: Optional[discord.Message] = None
        if record and not force_cleanup:
            _, msg_id = record
            try:
                existing_msg = await channel.fetch_message(int(msg_id))
            except (discord.NotFound, discord.HTTPException, ValueError):
                existing_msg = None

        if existing_msg is not None and not force_cleanup:
            # Edit existing message
            try:
                await existing_msg.edit(embed=embed, view=view)
                return
            except discord.NotFound:
                pass  # Fall through to recreate

        # Self-healing / Recreation flow (Section 8.4 & TC-022)
        logger.info(f"Self-healing parent message for key '{key}' in #{channel.name}")
        await self.cleanup_channel_history(channel)

        new_msg = await channel.send(embed=embed, view=view)
        async with self.bot.get_db() as conn:
            bot_msg_repo = BotMessageRepository(conn)
            await bot_msg_repo.set_message(key, str(channel.id), str(new_msg.id))

    async def cleanup_channel_history(self, channel: discord.TextChannel) -> None:
        """Execute history cleanup protocol (Section 8.4):

        - Messages within 14 days: bulk delete / purge
        - Messages beyond 14 days: delete up to 5 oldest/newest single deletes, ignore rest
        """
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=14)

        recent_msgs: List[discord.Message] = []
        old_msgs: List[discord.Message] = []

        try:
            async for msg in channel.history(limit=100):
                msg_created = msg.created_at
                if msg_created.tzinfo is None:
                    msg_created = msg_created.replace(tzinfo=timezone.utc)

                if msg_created >= cutoff:
                    recent_msgs.append(msg)
                else:
                    old_msgs.append(msg)

            # 1. Purge / delete messages within 14 days
            if recent_msgs:
                if hasattr(channel, "delete_messages") and len(recent_msgs) > 1:
                    try:
                        await channel.delete_messages(recent_msgs)
                    except discord.HTTPException:
                        for m in recent_msgs:
                            try:
                                await m.delete()
                            except discord.HTTPException:
                                pass
                else:
                    for m in recent_msgs:
                        try:
                            await m.delete()
                        except discord.HTTPException:
                            pass

            # 2. Beyond 14 days: delete at most 5 messages individually
            for m in old_msgs[:5]:
                try:
                    await m.delete()
                except discord.HTTPException:
                    pass

        except Exception as e:
            logger.warning(f"Error during channel history cleanup in #{channel.name}: {e}")

    async def post_done_log(self, task: TaskRecord) -> Optional[str]:
        """Post achievement message to #done-log (Section 8.5 At-Least Once)."""
        channel_id = self.bot.config.CH_DONE_LOG
        channel = self.bot.get_channel(channel_id)
        if not channel:
            return None

        content = format_done_log_message(task)
        try:
            sent = await channel.send(content)
            # Save message.id to tasks table
            async with self.bot.get_db() as conn:
                task_repo = TaskRepository(conn)
                await task_repo.update_task(task.id, done_log_message_id=str(sent.id))
            return str(sent.id)
        except Exception as e:
            logger.error(f"Failed to post to #done-log: {e}. Will be retried by catchup/overdue job.")
            return None

    async def delete_done_log_message(self, message_id: str) -> None:
        """Attempt to delete a message in #done-log on Undo (suppressing NotFound errors)."""
        channel_id = self.bot.config.CH_DONE_LOG
        channel = self.bot.get_channel(channel_id)
        if not channel:
            return

        try:
            msg = await channel.fetch_message(int(message_id))
            await msg.delete()
        except Exception:
            pass
