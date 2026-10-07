"""Periodic overdue checker and done-log recovery job.

Complies with Section 7.1, TC-017, TC-021, and TC-025 of the Momentum specification (v17).
Executes under db_lock_context() every OVERDUE_CHECK_INTERVAL_SEC (default: 60s).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional
import aiosqlite

from src.core.lock import db_lock_context
from src.core.time_utils import format_utc_iso, now_utc, parse_utc_iso
from src.db.connection import transaction
from src.db.repository import TaskRecord, TaskRepository

if TYPE_CHECKING:
    from src.bot.client import MomentumBot

logger = logging.getLogger(__name__)


@dataclass
class OverdueCheckResult:
    """Result summary of overdue check and recovery run."""
    evacuated_tasks: List[TaskRecord] = field(default_factory=list)
    today_cross_detected: bool = False
    recovered_done_logs: List[str] = field(default_factory=list)
    notification_sent: Optional[str] = None


async def check_overdue_and_recover(
    bot: "MomentumBot",
    current_utc: Optional[datetime] = None,
) -> OverdueCheckResult:
    """Execute Section 7.1 overdue migration, Today cross-boundary check, and done-log recovery."""
    ref_utc = current_utc or now_utc()
    ref_utc_iso = format_utc_iso(ref_utc)
    result = OverdueCheckResult()

    async with db_lock_context():
        async with bot.get_db() as conn:
            task_repo = TaskRepository(conn)

            # -------------------------------------------------------------
            # 1. Backlog overdue auto-migration (Undo exempt)
            # -------------------------------------------------------------
            sql_backlog_overdue = """
            SELECT * FROM tasks
            WHERE status = 'backlog'
              AND due_date IS NOT NULL
              AND due_date < ?
            ORDER BY due_date ASC;
            """
            cursor = await conn.execute(sql_backlog_overdue, (ref_utc_iso,))
            rows = await cursor.fetchall()
            overdue_candidates = [TaskRecord.from_row(r) for r in rows]

            if overdue_candidates:
                candidate_ids = [t.id for t in overdue_candidates]
                placeholders = ", ".join("?" for _ in candidate_ids)
                async with transaction(conn):
                    # Batch update status to 'overdue', keeping slot_index=NULL, today_since=NULL
                    await conn.execute(
                        f"UPDATE tasks SET status = 'overdue' WHERE id IN ({placeholders});",
                        tuple(candidate_ids),
                    )

                result.evacuated_tasks = overdue_candidates

                # Re-render #backlog and #overdue-tasks
                await bot.message_manager.render_backlog()
                await bot.message_manager.render_overdue()

                # Post aggregated notification to #task-inbox (Section 7.1 Step 4)
                inbox_channel = bot.get_channel(bot.config.CH_TASK_INBOX)
                if inbox_channel:
                    n = len(overdue_candidates)
                    if 1 <= n <= 5:
                        titles = "』『".join(t.title for t in overdue_candidates)
                        notice = f"❗ 期限を過ぎたタスク『{titles}』を #overdue-tasks に移動しました"
                    else:
                        title1 = overdue_candidates[0].title
                        title2 = overdue_candidates[1].title
                        remaining = n - 2
                        notice = f"❗ 期限を過ぎたタスク {n}件（『{title1}』『{title2}』他{remaining}件）を #overdue-tasks に移動しました"

                    try:
                        await inbox_channel.send(notice)
                        result.notification_sent = notice
                    except Exception as e:
                        logger.warning(f"Failed to send overdue notification to #task-inbox: {e}")

            # -------------------------------------------------------------
            # 2. Today overdue boundary detection (Focus policy)
            # -------------------------------------------------------------
            today_tasks = await task_repo.get_today_tasks()
            has_today_overdue = False
            for t in today_tasks:
                if t.due_date:
                    try:
                        if parse_utc_iso(t.due_date) < ref_utc:
                            has_today_overdue = True
                            break
                    except Exception:
                        pass

            if has_today_overdue:
                result.today_cross_detected = True
                # Re-render #today-focus only to update ❗ markers without evacuating (Section 7.1 Step 5)
                await bot.message_manager.render_today_focus()

            # -------------------------------------------------------------
            # 3. #done-log unsent recovery (At-Least Once; Section 7.1 Step 6)
            # -------------------------------------------------------------
            pending_done = await task_repo.get_pending_done_logs()
            for done_task in pending_done:
                msg_id = await bot.message_manager.post_done_log(done_task)
                if msg_id:
                    result.recovered_done_logs.append(done_task.id)

    return result
