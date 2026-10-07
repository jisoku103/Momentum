"""Interactive Persistent View for #today-focus.

Complies with Section 8.1 and Section 10.2 of the Momentum specification (v17).
Handles:
  - 3s timeout avoidance (defer_update)
  - Owner authorization guard (ephemeral rejection)
  - Slot actions: [🌱 初手クリア] [✅ 完了] [📦 控え室へ] [↩️ 直前の操作を取り消す]
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any, Dict, Optional
import discord

from src.core.lock import db_lock_context
from src.core.state_engine import determine_evacuation_status
from src.core.time_utils import now_utc_iso
from src.core.undo_engine import UndoEngine
from src.db.connection import transaction
from src.db.repository import TaskRecord, TaskRepository

if TYPE_CHECKING:
    from src.bot.client import MomentumBot


class TodayFocusView(discord.ui.View):
    """View attached to the pinned parent message in #today-focus."""

    def __init__(
        self,
        bot: "MomentumBot",
        today_slots: Dict[int, Optional[TaskRecord]],
    ) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self._build_components(today_slots)

    def _build_components(self, today_slots: Dict[int, Optional[TaskRecord]]) -> None:
        """Dynamically assemble buttons per occupied slot, plus the undo button on the last row."""
        current_row = 0
        for slot_idx in (1, 2, 3):
            task = today_slots.get(slot_idx)
            if task is not None:
                # 1. Micro-step button
                is_micro_disabled = task.micro_step is None or task.is_micro_completed == 1
                micro_label = "✨ 初手達成済" if task.is_micro_completed == 1 else "🌱 初手クリア"
                btn_micro = discord.ui.Button(
                    label=micro_label,
                    style=discord.ButtonStyle.secondary,
                    custom_id=f"btn:today:micro:{task.id}",
                    disabled=is_micro_disabled,
                    row=current_row,
                )
                self.add_item(btn_micro)

                # 2. Done button
                btn_done = discord.ui.Button(
                    label="✅ 完了",
                    style=discord.ButtonStyle.success,
                    custom_id=f"btn:today:done:{task.id}",
                    row=current_row,
                )
                self.add_item(btn_done)

                # 3. Evacuate to backlog button
                btn_backlog = discord.ui.Button(
                    label="📦 控え室へ",
                    style=discord.ButtonStyle.secondary,
                    custom_id=f"btn:today:backlog:{task.id}",
                    row=current_row,
                )
                self.add_item(btn_backlog)

                current_row += 1

        # Final row: Undo button
        btn_undo = discord.ui.Button(
            label="↩️ 直前の操作を取り消す",
            style=discord.ButtonStyle.danger,
            custom_id="btn:today:undo",
            row=min(current_row, 4),
        )
        self.add_item(btn_undo)


async def handle_today_interaction(
    bot: "MomentumBot",
    interaction: discord.Interaction,
    custom_id: str,
) -> None:
    """Handle interactions originating from TodayFocusView components."""
    # 1. Owner authorization guard (TC-024)
    if interaction.user.id != bot.config.OWNER_USER_ID:
        await interaction.response.send_message("このBotは個人用です", ephemeral=True)
        return

    # 2. Prevent 3s timeout (Section 2.1 & 8.1)
    await interaction.response.defer_update()

    # 3. Route by custom_id
    parts = custom_id.split(":")
    action = parts[2] if len(parts) > 2 else ""

    if action == "undo":
        await _handle_today_undo(bot, interaction)
        return

    task_id = parts[3] if len(parts) > 3 else ""
    if not task_id:
        return

    async with db_lock_context():
        async with bot.get_db() as conn:
            task_repo = TaskRepository(conn)
            undo_engine = UndoEngine(conn)
            task = await task_repo.get_task(task_id)

            # Verification: task must exist and have status == 'today'
            if not task or task.status != "today":
                await interaction.followup.send(
                    "⚠️ 対象タスクの状態がすでに変更されています。",
                    ephemeral=True,
                )
                await bot.message_manager.render_today_focus()
                return

            prev_state = task.to_dict()
            batch_id = str(uuid.uuid4())

            if action == "micro":
                # Clear micro step
                async with transaction(conn):
                    await task_repo.update_task(task.id, is_micro_completed=1)
                    await undo_engine.record_batch(
                        batch_id,
                        [{
                            "task_id": task.id,
                            "action_type": "clear_micro",
                            "previous_state": prev_state,
                        }],
                    )
                await bot.message_manager.render_today_focus()

            elif action == "done":
                # Complete task
                async with transaction(conn):
                    completed_task = await task_repo.update_task(
                        task.id,
                        status="completed",
                        slot_index=None,
                        today_since=None,
                        completed_at=now_utc_iso(),
                        done_log_message_id=None,
                    )
                    await undo_engine.record_batch(
                        batch_id,
                        [{
                            "task_id": task.id,
                            "action_type": "complete",
                            "previous_state": prev_state,
                        }],
                    )

                # Post to #done-log (At-Least Once)
                await bot.message_manager.post_done_log(completed_task)
                await bot.message_manager.render_today_focus()

            elif action == "backlog":
                # Evacuate using 5.1 rule
                dest_status = determine_evacuation_status(task.due_date)
                async with transaction(conn):
                    await task_repo.update_task(
                        task.id,
                        status=dest_status,
                        slot_index=None,
                        today_since=None,
                    )
                    await undo_engine.record_batch(
                        batch_id,
                        [{
                            "task_id": task.id,
                            "action_type": "to_backlog",
                            "previous_state": prev_state,
                        }],
                    )

                await bot.message_manager.render_today_focus()
                if dest_status == "overdue":
                    await bot.message_manager.render_overdue()
                else:
                    await bot.message_manager.render_backlog()


async def _handle_today_undo(
    bot: "MomentumBot",
    interaction: discord.Interaction,
) -> None:
    """Handle Undo button press in #today-focus."""
    async with db_lock_context():
        async with bot.get_db() as conn:
            undo_engine = UndoEngine(conn)
            async with transaction(conn):
                result = await undo_engine.execute_undo()

        # Try to delete Discord #done-log messages if any were rolled back
        for op in result.operations:
            if op.done_log_message_id_to_delete:
                await bot.message_manager.delete_done_log_message(op.done_log_message_id_to_delete)

        # Re-render all pinned screens
        await bot.message_manager.render_all_pinned()

    # Inform user via ephemeral message
    await interaction.followup.send(result.message, ephemeral=True)
