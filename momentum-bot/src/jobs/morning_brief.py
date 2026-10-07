"""Morning brief recommendation job executed daily at 08:00 JST.

Complies with Section 7.3 and TC-020 of the Momentum specification (v17).
Handles:
  - Priority candidate selection (Overdue -> 48h Backlog -> other Backlog; max 3 and <= free slots)
  - Interactive buttons with promotion (btn:brief:promote:{task_id})
  - Disabled '✨ 追加済み' button state update upon promotion
  - meta.last_morning_brief_date update
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, List, Optional
import discord

from src.core.lock import db_lock_context
from src.core.state_engine import find_first_available_slot
from src.core.time_utils import (
    format_jst_display,
    get_business_date,
    now_utc,
    now_utc_iso,
    parse_utc_iso,
    to_utc,
)
from src.core.undo_engine import UndoEngine
from src.db.connection import transaction
from src.db.repository import MetaRepository, TaskRecord, TaskRepository

if TYPE_CHECKING:
    from src.bot.client import MomentumBot

logger = logging.getLogger(__name__)


@dataclass
class MorningBriefResult:
    """Outcome of morning brief execution."""
    posted: bool = False
    candidates: List[TaskRecord] = field(default_factory=list)
    business_date: str = ""
    message_id: Optional[str] = None


class MorningBriefView(discord.ui.View):
    """Persistent view attached to the morning brief message."""

    def __init__(self, bot: "MomentumBot", candidates: List[TaskRecord]) -> None:
        super().__init__(timeout=None)
        self.bot = bot

        for idx, task in enumerate(candidates):
            title_truncated = task.title[:20]
            btn = discord.ui.Button(
                label=f"⬆️ {title_truncated}",
                style=discord.ButtonStyle.primary,
                custom_id=f"btn:brief:promote:{task.id}",
                row=idx,
            )
            self.add_item(btn)


async def select_brief_candidates(
    task_repo: TaskRepository,
    free_slots: int,
    ref_utc: datetime,
) -> List[TaskRecord]:
    """Select up to min(3, free_slots) candidate tasks according to Section 7.3 priority rules:

    1. Overdue tasks (oldest due_date first)
    2. Backlog tasks due within 48 hours (nearest due_date first)
    3. Other Backlog tasks (oldest created_at first)
    """
    max_candidates = min(3, free_slots)
    if max_candidates <= 0:
        return []

    chosen: List[TaskRecord] = []
    chosen_ids = set()

    # 1. Overdue tasks
    overdue_tasks = await task_repo.get_overdue_tasks()
    for t in overdue_tasks:
        if len(chosen) >= max_candidates:
            break
        chosen.append(t)
        chosen_ids.add(t.id)

    # 2. Backlog tasks within 48 hours
    if len(chosen) < max_candidates:
        all_backlog = await task_repo.get_backlog_tasks()
        within_48h: List[TaskRecord] = []
        for t in all_backlog:
            if t.id not in chosen_ids and t.due_date:
                try:
                    due_dt = parse_utc_iso(t.due_date)
                    if ref_utc <= due_dt <= ref_utc + timedelta(hours=48):
                        within_48h.append(t)
                except Exception:
                    pass

        # Sort nearest due first
        within_48h.sort(key=lambda t: t.due_date or "")
        for t in within_48h:
            if len(chosen) >= max_candidates:
                break
            chosen.append(t)
            chosen_ids.add(t.id)

    # 3. Other backlog tasks (created_at ascending)
    if len(chosen) < max_candidates:
        all_backlog = await task_repo.get_backlog_tasks()
        for t in all_backlog:
            if len(chosen) >= max_candidates:
                break
            if t.id not in chosen_ids:
                chosen.append(t)
                chosen_ids.add(t.id)

    return chosen


async def execute_morning_brief(
    bot: "MomentumBot",
    current_utc: Optional[datetime] = None,
) -> MorningBriefResult:
    """Execute Section 7.3 morning brief recommendation post."""
    ref_utc = current_utc or now_utc()
    b_date = get_business_date(ref_utc, day_boundary_hour=bot.config.DAY_BOUNDARY_HOUR)
    result = MorningBriefResult(business_date=b_date)

    async with db_lock_context():
        async with bot.get_db() as conn:
            task_repo = TaskRepository(conn)
            meta_repo = MetaRepository(conn)

            today_tasks = await task_repo.get_today_tasks()
            free_slots = 3 - len(today_tasks)

            candidates = await select_brief_candidates(task_repo, free_slots, ref_utc)
            result.candidates = candidates

            # Update meta flag regardless of whether message is posted (Section 7.3)
            await meta_repo.set_last_morning_brief_date(b_date)

        if not candidates or free_slots <= 0:
            return result

        # Construct message content
        candidate_lines = []
        for t in candidates:
            line = f"・ {t.title}"
            if t.due_date:
                due_display = format_jst_display(t.due_date, include_weekday=False)
                warn_prefix = "❗ " if t.status == "overdue" else ""
                line += f"（{warn_prefix}〆 {due_display}）"
            candidate_lines.append(line)

        msg_content = (
            "☀️ おはよう！今日の候補はこちら。気になるものをタップで Today に追加できるよ\n\n"
            + "\n".join(candidate_lines)
        )

        inbox_channel = bot.get_channel(bot.config.CH_TASK_INBOX)
        if inbox_channel:
            view = MorningBriefView(bot, candidates)
            try:
                sent = await inbox_channel.send(msg_content, view=view)
                result.posted = True
                result.message_id = str(sent.id)
            except Exception as e:
                logger.error(f"Failed to post morning brief to #task-inbox: {e}")

    return result


async def handle_brief_interaction(
    bot: "MomentumBot",
    interaction: discord.Interaction,
    custom_id: str,
) -> None:
    """Handle interactions from MorningBriefView buttons (btn:brief:promote:{task_id})."""
    # 1. Owner authorization guard (TC-024)
    if interaction.user.id != bot.config.OWNER_USER_ID:
        await interaction.response.send_message("このBotは個人用です", ephemeral=True)
        return

    # 2. Defer update
    await interaction.response.defer_update()

    task_id = custom_id.replace("btn:brief:promote:", "")
    if not task_id:
        return

    async with db_lock_context():
        async with bot.get_db() as conn:
            task_repo = TaskRepository(conn)
            undo_engine = UndoEngine(conn)
            task = await task_repo.get_task(task_id)

            # Verification: exists and status in ('backlog', 'overdue')
            if not task or task.status not in ("backlog", "overdue"):
                await interaction.followup.send(
                    "⚠️ 対象タスクはすでに移動または完了しています。",
                    ephemeral=True,
                )
                await _disable_brief_button(interaction, task_id)
                return

            # Check capacity
            today_tasks = await task_repo.get_today_tasks()
            free_slot = find_first_available_slot(today_tasks)
            if free_slot is None:
                await interaction.followup.send(
                    "Today が満杯（3件）です。スロットを空けてから追加してね",
                    ephemeral=True,
                )
                return

            # Promote to Today (recorded as to_today in Undo)
            prev_state = task.to_dict()
            batch_id = str(uuid.uuid4())
            orig_status = task.status

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

            # Update clicked button: mark as disabled with '✨ 追加済み' (Section 7.3 & TC-020)
            await _mark_brief_button_added(interaction, task_id, task.title)

            # Re-render Today and origin channel
            await bot.message_manager.render_today_focus()
            if orig_status == "overdue":
                await bot.message_manager.render_overdue()
            else:
                await bot.message_manager.render_backlog()


async def _mark_brief_button_added(
    interaction: discord.Interaction,
    task_id: str,
    title: str,
) -> None:
    """Disable button and change label to include '✨ 追加済み'."""
    message = interaction.message
    if not message:
        return

    view = discord.ui.View.from_message(message, timeout=None)
    for child in view.children:
        if isinstance(child, discord.ui.Button) and child.custom_id == f"btn:brief:promote:{task_id}":
            child.disabled = True
            child.label = f"✨ 追加済み ({title[:10]})"
            child.style = discord.ButtonStyle.secondary

    try:
        await interaction.message.edit(view=view)
    except Exception as e:
        logger.warning(f"Failed to edit morning brief view: {e}")


async def _disable_brief_button(
    interaction: discord.Interaction,
    task_id: str,
) -> None:
    """Disable button without marking added."""
    message = interaction.message
    if not message:
        return

    view = discord.ui.View.from_message(message, timeout=None)
    for child in view.children:
        if isinstance(child, discord.ui.Button) and child.custom_id == f"btn:brief:promote:{task_id}":
            child.disabled = True

    try:
        await interaction.message.edit(view=view)
    except Exception as e:
        logger.warning(f"Failed to edit morning brief view: {e}")
