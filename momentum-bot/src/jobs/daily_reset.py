"""Daily reset job executed at day boundary (04:00 JST).

Complies with Section 7.2, TC-018, and TC-026 of the Momentum specification (v17).
Handles:
  - Today slot sorting evacuation into backlog/overdue (Undoable as 1 batch)
  - Physical deletion cleanup for 30-day soft-deleted tasks (protecting action_logs)
  - meta.last_daily_job_date update
  - Announcement in #task-inbox and re-rendering of all 3 pinned channels
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from src.core.lock import db_lock_context
from src.core.state_engine import determine_evacuation_status
from src.core.time_utils import get_business_date, now_utc
from src.core.undo_engine import UndoEngine
from src.db.connection import transaction
from src.db.repository import MetaRepository, TaskRecord, TaskRepository

if TYPE_CHECKING:
    from src.bot.client import MomentumBot

logger = logging.getLogger(__name__)


@dataclass
class DailyResetResult:
    """Summary outcome of daily reset execution."""
    evacuated_tasks: List[TaskRecord] = field(default_factory=list)
    physically_deleted_count: int = 0
    batch_id: Optional[str] = None
    business_date: str = ""


async def execute_daily_reset(
    bot: "MomentumBot",
    current_utc: Optional[datetime] = None,
) -> DailyResetResult:
    """Execute Section 7.2 daily reset protocol."""
    ref_utc = current_utc or now_utc()
    b_date = get_business_date(ref_utc, day_boundary_hour=bot.config.DAY_BOUNDARY_HOUR)
    result = DailyResetResult(business_date=b_date)

    async with db_lock_context():
        async with bot.get_db() as conn:
            task_repo = TaskRepository(conn)
            undo_engine = UndoEngine(conn)
            meta_repo = MetaRepository(conn)

            today_tasks = await task_repo.get_today_tasks()
            batch_id = str(uuid.uuid4()) if today_tasks else None
            action_logs = []

            # 1. Evacuate Today tasks (Section 7.2 Step 1)
            async with transaction(conn):
                if today_tasks:
                    for task in today_tasks:
                        dest_status = determine_evacuation_status(task.due_date, ref_utc)
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

                    # Record single batch for Undo
                    await undo_engine.record_batch(batch_id, action_logs)
                    result.batch_id = batch_id
                    result.evacuated_tasks = today_tasks

                # 2. Physical deletion cleanup with Undo protection (Section 7.2 Step 2 & TC-026)
                del_count = await task_repo.physical_delete_old_tasks(
                    retention_days=bot.config.DELETED_RETENTION_DAYS
                )
                result.physically_deleted_count = del_count

                # 3. Update execution flag in meta (Section 7.2 Step 3)
                await meta_repo.set_last_daily_job_date(b_date)

        # 4. Re-render all 3 pinned screens (Section 7.2 Step 4)
        await bot.message_manager.render_all_pinned()

        # 5. Post notification to #task-inbox
        inbox_channel = bot.get_channel(bot.config.CH_TASK_INBOX)
        if inbox_channel:
            try:
                await inbox_channel.send("🌅 新しい一日です。スロットをリセットしました！")
            except Exception as e:
                logger.warning(f"Failed to post daily reset message in #task-inbox: {e}")

    return result
