"""Interactive Persistent View for #overdue-tasks.

Complies with Section 8.3 of the Momentum specification (v17).
Handles:
  - Emergency promotion select menu (select:overdue:promote)
  - Pagination buttons (btn:overdue:prev, btn:overdue:next)
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, List
import discord

from src.core.lock import db_lock_context
from src.core.state_engine import find_first_available_slot
from src.core.time_utils import format_jst_display, now_utc_iso
from src.core.undo_engine import UndoEngine
from src.db.connection import transaction
from src.db.repository import TaskRecord, TaskRepository

if TYPE_CHECKING:
    from src.bot.client import MomentumBot


class OverdueView(discord.ui.View):
    """View attached to the pinned parent message in #overdue-tasks."""

    def __init__(
        self,
        bot: "MomentumBot",
        page_tasks: List[TaskRecord],
        current_page: int,
        total_pages: int,
    ) -> None:
        super().__init__(timeout=None)
        self.bot = bot

        if page_tasks:
            select_options = []
            for t in page_tasks[:8]:
                due_desc = f"❗ 期限切れ: {format_jst_display(t.due_date)}" if t.due_date else "❗ 期限切れ"
                select_options.append(
                    discord.SelectOption(
                        label=t.title[:100],
                        description=due_desc[:100],
                        value=t.id,
                    )
                )

            select = discord.ui.Select(
                custom_id="select:overdue:promote",
                placeholder="Today に緊急追加するタスクを選択...",
                options=select_options,
                row=0,
            )
            self.add_item(select)

        # Row 2: Pagination buttons
        btn_prev = discord.ui.Button(
            label="◀️ 前へ",
            style=discord.ButtonStyle.secondary,
            custom_id="btn:overdue:prev",
            disabled=(current_page <= 1),
            row=1,
        )
        btn_next = discord.ui.Button(
            label="▶️ 次へ",
            style=discord.ButtonStyle.secondary,
            custom_id="btn:overdue:next",
            disabled=(current_page >= max(1, total_pages)),
            row=1,
        )
        self.add_item(btn_prev)
        self.add_item(btn_next)


async def handle_overdue_interaction(
    bot: "MomentumBot",
    interaction: discord.Interaction,
    custom_id: str,
) -> None:
    """Handle interactions from OverdueView."""
    # 1. Owner authorization guard (TC-024)
    if interaction.user.id != bot.config.OWNER_USER_ID:
        await interaction.response.send_message("このBotは個人用です", ephemeral=True)
        return

    # 2. Defer update
    await interaction.response.defer()

    if custom_id == "btn:overdue:prev":
        bot.message_manager.change_overdue_page(delta=-1)
        await bot.message_manager.render_overdue()
        return

    if custom_id == "btn:overdue:next":
        bot.message_manager.change_overdue_page(delta=1)
        await bot.message_manager.render_overdue()
        return

    if custom_id == "select:overdue:promote":
        selected_task_ids = interaction.data.get("values", [])
        if not selected_task_ids:
            return
        task_id = selected_task_ids[0]

        async with db_lock_context():
            async with bot.get_db() as conn:
                task_repo = TaskRepository(conn)
                undo_engine = UndoEngine(conn)
                task = await task_repo.get_task(task_id)

                if not task or task.status != "overdue":
                    await interaction.followup.send(
                        "⚠️ 対象タスクの状態がすでに変更されています。",
                        ephemeral=True,
                    )
                    await bot.message_manager.render_overdue()
                    return

                # Check Today slot capacity (Section 8.3)
                today_tasks = await task_repo.get_today_tasks()
                free_slot = find_first_available_slot(today_tasks)
                if free_slot is None:
                    await interaction.followup.send(
                        "Today が満杯（3件）です。スロットを空けてから追加してね",
                        ephemeral=True,
                    )
                    return

                # Promote to Today
                prev_state = task.to_dict()
                batch_id = str(uuid.uuid4())
                async with transaction(conn):
                    await task_repo.update_task(
                        task.id,
                        status="today",
                        slot_index=free_slot,
                        today_since=now_utc_iso(),
                    )
                    await undo_engine.record_batch(
                        batch_id,
                        [{
                            "task_id": task.id,
                            "action_type": "to_today",
                            "previous_state": prev_state,
                        }],
                    )

            await bot.message_manager.render_today_focus()
            await bot.message_manager.render_overdue()
