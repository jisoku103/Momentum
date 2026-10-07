"""Inbox message listener Cog for #task-inbox.

Complies with Section 3.2, Section 4.1, and Section 10.2 of the Momentum specification (v17).
Handles:
  - Natural language message filtering (owner & channel restricted)
  - 500-char immediate cutoff check before queue enqueueing
  - Instant '⏳ 整理中…' feedback with fail_if_not_exists=False
  - Serial message queue processing
  - Message edit reflection with discord.NotFound fallback
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
import discord
from discord.ext import commands

from src.core.queue import InboxMessageItem, SerialMessageQueue, validate_message_length
from src.gemini.pipeline import execute_natural_language_pipeline

if TYPE_CHECKING:
    from src.bot.client import MomentumBot

logger = logging.getLogger(__name__)


class InboxCog(commands.Cog):
    """Cog monitoring natural language messages in #task-inbox."""

    def __init__(self, bot: "MomentumBot") -> None:
        self.bot = bot
        # Serial queue worker for #task-inbox
        self.queue = SerialMessageQueue(handler=self._process_queued_message)
        self.queue.start()

    async def cog_unload(self) -> None:
        """Gracefully stop queue worker when cog unloads."""
        await self.queue.stop()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Handle incoming messages in #task-inbox."""
        # 1. Ignore bot's own messages and system messages
        if message.author.bot or message.is_system():
            return

        # 2. Strict channel and owner check (Section 1.1 & 3.2)
        if message.channel.id != self.bot.config.CH_TASK_INBOX:
            return
        if message.author.id != self.bot.config.OWNER_USER_ID:
            # Completely ignore messages from other users in natural text (Section 10.2)
            return

        # 3. 500-character pre-check (Section 4.1 & TC-014)
        length_error = validate_message_length(message.content)
        if length_error:
            await message.reply(length_error, fail_if_not_exists=False)
            return

        # 4. Instant feedback reply
        feedback_msg = await message.reply("⏳ 整理中…", fail_if_not_exists=False)

        # 5. Enqueue for serial processing
        async def reply_callback(text: str) -> None:
            await message.reply(text, fail_if_not_exists=False)

        item = InboxMessageItem(
            content=message.content,
            message_id=message.id,
            channel_id=message.channel.id,
            reply_callback=reply_callback,
        )
        # Store feedback message reference on item for editing
        item.feedback_msg = feedback_msg  # type: ignore

        await self.queue.enqueue(item)

    async def _process_queued_message(self, item: InboxMessageItem) -> None:
        """Process a queued message sequentially."""
        feedback_msg: Optional[discord.Message] = getattr(item, "feedback_msg", None)

        try:
            async with self.bot.get_db() as conn:
                pipeline_result = await execute_natural_language_pipeline(
                    user_input=item.content,
                    conn=conn,
                    gemini_client=self.bot.gemini_client,
                )

            # Re-render pinned parent screens if operations were actionable and applied
            if pipeline_result.is_actionable and pipeline_result.success:
                await self.bot.message_manager.render_all_pinned()

            # Reflect result onto feedback message
            reply_text = pipeline_result.user_reply
            await self._update_feedback_message(item.channel_id, feedback_msg, reply_text)

        except Exception as e:
            logger.exception(f"Unexpected error processing natural language message: {e}")
            err_text = "⚠️ 予期しないエラーが発生したよ。時間をおいてもう一度試してみてね！"
            await self._update_feedback_message(item.channel_id, feedback_msg, err_text)

    async def _update_feedback_message(
        self,
        channel_id: int,
        feedback_msg: Optional[discord.Message],
        content: str,
    ) -> None:
        """Edit feedback message or fallback to new message if deleted (Section 4.1)."""
        if feedback_msg is not None:
            try:
                await feedback_msg.edit(content=content)
                return
            except discord.NotFound:
                # 404: original feedback message was deleted
                pass
            except Exception as e:
                logger.warning(f"Failed to edit feedback message: {e}")

        # Fallback to posting a new message in #task-inbox
        channel = self.bot.get_channel(channel_id)
        if channel:
            await channel.send(content)


async def setup(bot: "MomentumBot") -> None:
    await bot.add_cog(InboxCog(bot))
