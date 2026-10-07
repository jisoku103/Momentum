"""Data access repository implementations for Momentum Bot.

Handles CRUD operations for:
  - tasks
  - action_logs
  - bot_messages
  - meta
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional
import aiosqlite

from src.core.time_utils import now_utc_iso


@dataclass
class TaskRecord:
    """Represents a row in the tasks table."""
    id: str
    kind: str
    title: str
    raw_input: Optional[str]
    if_then_trigger: Optional[str]
    micro_step: Optional[str]
    is_micro_completed: int
    due_date: Optional[str]
    status: str
    slot_index: Optional[int]
    today_since: Optional[str]
    done_log_message_id: Optional[str]
    created_at: str
    completed_at: Optional[str]
    deleted_at: Optional[str]

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> TaskRecord:
        return cls(
            id=row["id"],
            kind=row["kind"],
            title=row["title"],
            raw_input=row["raw_input"],
            if_then_trigger=row["if_then_trigger"],
            micro_step=row["micro_step"],
            is_micro_completed=row["is_micro_completed"],
            due_date=row["due_date"],
            status=row["status"],
            slot_index=row["slot_index"],
            today_since=row["today_since"],
            done_log_message_id=row["done_log_message_id"],
            created_at=row["created_at"],
            completed_at=row["completed_at"],
            deleted_at=row["deleted_at"],
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


@dataclass
class ActionLogRecord:
    """Represents a row in the action_logs table."""
    id: int
    batch_id: str
    task_id: str
    action_type: str
    previous_state: Optional[str]
    created_at: str

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> ActionLogRecord:
        return cls(
            id=row["id"],
            batch_id=row["batch_id"],
            task_id=row["task_id"],
            action_type=row["action_type"],
            previous_state=row["previous_state"],
            created_at=row["created_at"],
        )


class TaskRepository:
    """Repository for tasks table CRUD operations."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self.conn = conn

    async def create_task(
        self,
        title: str,
        kind: str = "task",
        raw_input: Optional[str] = None,
        if_then_trigger: Optional[str] = None,
        micro_step: Optional[str] = None,
        is_micro_completed: int = 0,
        due_date: Optional[str] = None,
        status: str = "backlog",
        slot_index: Optional[int] = None,
        today_since: Optional[str] = None,
        done_log_message_id: Optional[str] = None,
        task_id: Optional[str] = None,
        created_at: Optional[str] = None,
        completed_at: Optional[str] = None,
        deleted_at: Optional[str] = None,
    ) -> TaskRecord:
        """Create and insert a new task row into the database."""
        t_id = task_id or str(uuid.uuid4())
        t_created_at = created_at or now_utc_iso()

        sql = """
        INSERT INTO tasks (
            id, kind, title, raw_input, if_then_trigger, micro_step,
            is_micro_completed, due_date, status, slot_index, today_since,
            done_log_message_id, created_at, completed_at, deleted_at
        ) VALUES (
            ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?,
            ?, ?, ?, ?
        );
        """
        params = (
            t_id, kind, title, raw_input, if_then_trigger, micro_step,
            is_micro_completed, due_date, status, slot_index, today_since,
            done_log_message_id, t_created_at, completed_at, deleted_at,
        )
        await self.conn.execute(sql, params)
        created = await self.get_task(t_id)
        if created is None:
            raise RuntimeError(f"Failed to retrieve created task with id: {t_id}")
        return created

    async def get_task(self, task_id: str) -> Optional[TaskRecord]:
        """Fetch a single task by its primary key ID."""
        sql = "SELECT * FROM tasks WHERE id = ?;"
        cursor = await self.conn.execute(sql, (task_id,))
        row = await cursor.fetchone()
        return TaskRecord.from_row(row) if row else None

    async def get_tasks_by_status(self, status: str) -> List[TaskRecord]:
        """Fetch tasks filtered by status."""
        sql = "SELECT * FROM tasks WHERE status = ? ORDER BY created_at ASC;"
        cursor = await self.conn.execute(sql, (status,))
        rows = await cursor.fetchall()
        return [TaskRecord.from_row(r) for r in rows]

    async def get_today_tasks(self) -> List[TaskRecord]:
        """Fetch tasks currently in Today slots, ordered by slot_index (1..3)."""
        sql = "SELECT * FROM tasks WHERE status = 'today' ORDER BY slot_index ASC;"
        cursor = await self.conn.execute(sql)
        rows = await cursor.fetchall()
        return [TaskRecord.from_row(r) for r in rows]

    async def get_today_slots(self) -> Dict[int, Optional[TaskRecord]]:
        """Return a mapping of slot numbers 1..3 to assigned TaskRecord or None."""
        tasks = await self.get_today_tasks()
        slots: Dict[int, Optional[TaskRecord]] = {1: None, 2: None, 3: None}
        for task in tasks:
            if task.slot_index in slots:
                slots[task.slot_index] = task
        return slots

    async def get_backlog_tasks(self, limit: Optional[int] = None) -> List[TaskRecord]:
        """Fetch backlog tasks sorted by due_date ascending (NULLs last), then created_at."""
        sql = """
        SELECT * FROM tasks
        WHERE status = 'backlog'
        ORDER BY
            CASE WHEN due_date IS NULL THEN 1 ELSE 0 END,
            due_date ASC,
            created_at ASC
        """
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        sql += ";"
        cursor = await self.conn.execute(sql)
        rows = await cursor.fetchall()
        return [TaskRecord.from_row(r) for r in rows]

    async def get_overdue_tasks(self, limit: Optional[int] = None) -> List[TaskRecord]:
        """Fetch overdue tasks sorted by due_date ascending, then created_at."""
        sql = """
        SELECT * FROM tasks
        WHERE status = 'overdue'
        ORDER BY due_date ASC, created_at ASC
        """
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        sql += ";"
        cursor = await self.conn.execute(sql)
        rows = await cursor.fetchall()
        return [TaskRecord.from_row(r) for r in rows]

    async def get_pending_done_logs(self) -> List[TaskRecord]:
        """Fetch completed tasks where done_log_message_id is NULL for At-Least Once recovery."""
        sql = """
        SELECT * FROM tasks
        WHERE status = 'completed' AND done_log_message_id IS NULL
        ORDER BY completed_at ASC;
        """
        cursor = await self.conn.execute(sql)
        rows = await cursor.fetchall()
        return [TaskRecord.from_row(r) for r in rows]

    async def update_task(self, task_id: str, **fields: Any) -> Optional[TaskRecord]:
        """Update arbitrary columns of a task. Returns updated TaskRecord."""
        if not fields:
            return await self.get_task(task_id)

        set_clauses = [f"{col} = ?" for col in fields.keys()]
        params = list(fields.values())
        params.append(task_id)

        sql = f"UPDATE tasks SET {', '.join(set_clauses)} WHERE id = ?;"
        await self.conn.execute(sql, tuple(params))
        return await self.get_task(task_id)

    async def soft_delete_task(
        self,
        task_id: str,
        deleted_at: Optional[str] = None,
    ) -> Optional[TaskRecord]:
        """Mark task as logically deleted, clearing slot_index and today_since."""
        t_deleted_at = deleted_at or now_utc_iso()
        return await self.update_task(
            task_id,
            status="deleted",
            slot_index=None,
            today_since=None,
            deleted_at=t_deleted_at,
        )

    async def physical_delete_old_tasks(self, retention_days: int = 30) -> int:
        """Permanently delete soft-deleted tasks older than retention_days,

        protecting tasks that are referenced in action_logs (Section 7.2 & TC-026).
        """
        sql = f"""
        DELETE FROM tasks
        WHERE status = 'deleted'
          AND deleted_at <= DATETIME('now', '-{int(retention_days)} days')
          AND id NOT IN (SELECT task_id FROM action_logs);
        """
        cursor = await self.conn.execute(sql)
        return cursor.rowcount

    async def get_completed_counts(
        self,
        day_boundary_hour: int = 4,
    ) -> tuple[int, int]:
        """Return (today_completed_count, total_completed_count) according to Section 8.1.

        - Today completed: status='completed' and completed_at >= start of current business day (in UTC)
        - Total completed: status='completed' all-time count
        """
        from datetime import datetime, time, timedelta
        from src.core.time_utils import TIMEZONE_JST, format_utc_iso, now_jst

        # Calculate start of current business day in JST
        now_j = now_jst()
        # If current hour < day_boundary_hour, business date started yesterday at day_boundary_hour
        if now_j.hour < day_boundary_hour:
            start_date = (now_j - timedelta(days=1)).date()
        else:
            start_date = now_j.date()

        day_start_jst = datetime.combine(
            start_date,
            time(hour=day_boundary_hour, minute=0, second=0),
            tzinfo=TIMEZONE_JST,
        )
        day_start_utc_iso = format_utc_iso(day_start_jst)

        # 1. Today completed count
        sql_today = """
        SELECT COUNT(*) FROM tasks
        WHERE status = 'completed' AND completed_at >= ?;
        """
        cursor_today = await self.conn.execute(sql_today, (day_start_utc_iso,))
        row_today = await cursor_today.fetchone()
        today_count = row_today[0] if row_today else 0

        # 2. Total completed count
        sql_total = "SELECT COUNT(*) FROM tasks WHERE status = 'completed';"
        cursor_total = await self.conn.execute(sql_total)
        row_total = await cursor_total.fetchone()
        total_count = row_total[0] if row_total else 0

        return today_count, total_count


class ActionLogRepository:
    """Repository for action_logs table operations."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self.conn = conn

    async def create_log_batch(
        self,
        batch_id: str,
        logs: List[Dict[str, Any]],
    ) -> None:
        """Insert a batch of action log entries and prune old generations."""
        if not logs:
            return

        sql = """
        INSERT INTO action_logs (batch_id, task_id, action_type, previous_state, created_at)
        VALUES (?, ?, ?, ?, ?);
        """
        for item in logs:
            created_at = item.get("created_at") or now_utc_iso()
            prev = item.get("previous_state")
            prev_str = prev if (prev is None or isinstance(prev, str)) else json.dumps(prev, ensure_ascii=False)
            await self.conn.execute(
                sql,
                (batch_id, item["task_id"], item["action_type"], prev_str, created_at),
            )

        # Retain latest 3 batches, pruning 4th and older (Section 6.2)
        await self.cleanup_old_batches(retention_count=3)

    async def get_latest_batch_ids(self, limit: Optional[int] = None) -> List[str]:
        """Fetch distinct batch_ids in reverse chronological order (newest first)."""
        sql = """
        SELECT batch_id
        FROM action_logs
        GROUP BY batch_id
        ORDER BY MAX(id) DESC
        """
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        sql += ";"
        cursor = await self.conn.execute(sql)
        rows = await cursor.fetchall()
        return [row[0] for row in rows]

    async def cleanup_old_batches(self, retention_count: int = 3) -> int:
        """Delete action_logs belonging to batches older than retention_count.

        Complies with Section 6.2.
        """
        valid_batch_ids = await self.get_latest_batch_ids(limit=retention_count)
        if not valid_batch_ids:
            return 0

        placeholders = ", ".join("?" for _ in valid_batch_ids)
        sql = f"DELETE FROM action_logs WHERE batch_id NOT IN ({placeholders});"
        cursor = await self.conn.execute(sql, tuple(valid_batch_ids))
        return cursor.rowcount

    async def get_logs_by_batch(self, batch_id: str) -> List[ActionLogRecord]:
        """Fetch action log records for a given batch_id in LIFO order (id DESC)."""
        sql = "SELECT * FROM action_logs WHERE batch_id = ? ORDER BY id DESC;"
        cursor = await self.conn.execute(sql, (batch_id,))
        rows = await cursor.fetchall()
        return [ActionLogRecord.from_row(r) for r in rows]

    async def delete_batch(self, batch_id: str) -> int:
        """Delete all action_logs for a specific batch_id (e.g., after rollback)."""
        sql = "DELETE FROM action_logs WHERE batch_id = ?;"
        cursor = await self.conn.execute(sql, (batch_id,))
        return cursor.rowcount


class MetaRepository:
    """Repository for meta key-value store operations."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self.conn = conn

    async def get_value(self, key: str) -> Optional[str]:
        """Get the value associated with a key."""
        sql = "SELECT value FROM meta WHERE key = ?;"
        cursor = await self.conn.execute(sql, (key,))
        row = await cursor.fetchone()
        return row[0] if row else None

    async def set_value(self, key: str, value: str) -> None:
        """Set a key-value pair, inserting or replacing if already exists."""
        sql = "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?);"
        await self.conn.execute(sql, (key, value))

    async def get_last_daily_job_date(self) -> Optional[str]:
        return await self.get_value("last_daily_job_date")

    async def set_last_daily_job_date(self, business_date: str) -> None:
        await self.set_value("last_daily_job_date", business_date)

    async def get_last_morning_brief_date(self) -> Optional[str]:
        return await self.get_value("last_morning_brief_date")

    async def set_last_morning_brief_date(self, business_date: str) -> None:
        await self.set_value("last_morning_brief_date", business_date)


class BotMessageRepository:
    """Repository for persistent bot parent message IDs."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self.conn = conn

    async def get_message(self, key: str) -> Optional[tuple[str, str]]:
        """Fetch (channel_id, message_id) for a key ('today_focus', 'backlog', etc.)."""
        sql = "SELECT channel_id, message_id FROM bot_messages WHERE key = ?;"
        cursor = await self.conn.execute(sql, (key,))
        row = await cursor.fetchone()
        return (row["channel_id"], row["message_id"]) if row else None

    async def set_message(self, key: str, channel_id: str, message_id: str) -> None:
        """Save or replace (channel_id, message_id) for a key."""
        sql = "INSERT OR REPLACE INTO bot_messages (key, channel_id, message_id) VALUES (?, ?, ?);"
        await self.conn.execute(sql, (key, str(channel_id), str(message_id)))

    async def delete_message(self, key: str) -> int:
        """Delete the message entry for a key."""
        sql = "DELETE FROM bot_messages WHERE key = ?;"
        cursor = await self.conn.execute(sql, (key,))
        return cursor.rowcount
