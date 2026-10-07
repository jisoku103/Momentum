"""Unit and constraint tests for SQLite database layer.

Complies with Section 2.1, 2.2, 6.2, and Chapter 9 of the specification.
"""

import uuid
import pytest
import aiosqlite
import sqlite3

from src.core.time_utils import now_utc_iso
from src.db.connection import transaction
from src.db.repository import (
    ActionLogRepository,
    BotMessageRepository,
    MetaRepository,
    TaskRepository,
)


@pytest.mark.asyncio
async def test_pragma_settings(db_conn: aiosqlite.Connection) -> None:
    """Verify that mandatory PRAGMAs (WAL, foreign_keys=ON, busy_timeout=5000) are active."""
    # 1. foreign_keys
    cursor = await db_conn.execute("PRAGMA foreign_keys;")
    row = await cursor.fetchone()
    assert row[0] == 1, "PRAGMA foreign_keys must be 1 (ON)"

    # 2. busy_timeout
    cursor = await db_conn.execute("PRAGMA busy_timeout;")
    row = await cursor.fetchone()
    assert row[0] == 5000, "PRAGMA busy_timeout must be 5000"

    # 3. journal_mode
    cursor = await db_conn.execute("PRAGMA journal_mode;")
    row = await cursor.fetchone()
    assert str(row[0]).lower() == "wal", f"PRAGMA journal_mode should be WAL, got {row[0]}"


@pytest.mark.asyncio
async def test_tasks_check_constraint_today_slot(task_repo: TaskRepository) -> None:
    """Verify CHECK constraints for status='today'.

    - slot_index must be NOT NULL, between 1 and 3, and today_since must be NOT NULL.
    """
    now_str = now_utc_iso()

    # Valid today task
    valid_task = await task_repo.create_task(
        title="Valid Today Task 1",
        status="today",
        slot_index=1,
        today_since=now_str,
    )
    assert valid_task.slot_index == 1
    assert valid_task.today_since == now_str

    # 1. status='today' with slot_index=None -> Constraint error
    with pytest.raises(aiosqlite.IntegrityError):
        await task_repo.create_task(
            title="Invalid Today Task (slot None)",
            status="today",
            slot_index=None,
            today_since=now_str,
        )

    # 2. status='today' with slot_index=0 (< 1) -> Constraint error
    with pytest.raises(aiosqlite.IntegrityError):
        await task_repo.create_task(
            title="Invalid Today Task (slot 0)",
            status="today",
            slot_index=0,
            today_since=now_str,
        )

    # 3. status='today' with slot_index=4 (> 3) -> Constraint error
    with pytest.raises(aiosqlite.IntegrityError):
        await task_repo.create_task(
            title="Invalid Today Task (slot 4)",
            status="today",
            slot_index=4,
            today_since=now_str,
        )

    # 4. status='today' with today_since=None -> Constraint error
    with pytest.raises(aiosqlite.IntegrityError):
        await task_repo.create_task(
            title="Invalid Today Task (today_since None)",
            status="today",
            slot_index=2,
            today_since=None,
        )


@pytest.mark.asyncio
async def test_tasks_check_constraint_non_today_slot(task_repo: TaskRepository) -> None:
    """Verify non-today status must have slot_index=NULL and today_since=NULL."""
    now_str = now_utc_iso()

    # status='backlog' with slot_index!=None -> Constraint error
    with pytest.raises(aiosqlite.IntegrityError):
        await task_repo.create_task(
            title="Invalid Backlog Task (with slot)",
            status="backlog",
            slot_index=1,
            today_since=None,
        )

    # status='backlog' with today_since!=None -> Constraint error
    with pytest.raises(aiosqlite.IntegrityError):
        await task_repo.create_task(
            title="Invalid Backlog Task (with today_since)",
            status="backlog",
            slot_index=None,
            today_since=now_str,
        )


@pytest.mark.asyncio
async def test_tasks_check_constraint_status_timestamps(task_repo: TaskRepository) -> None:
    """Verify completed_at and deleted_at consistency checks."""
    now_str = now_utc_iso()

    # 1. status='completed' without completed_at -> Constraint error
    with pytest.raises(aiosqlite.IntegrityError):
        await task_repo.create_task(
            title="Invalid Completed (no completed_at)",
            status="completed",
            completed_at=None,
        )

    # Valid completed task
    valid_completed = await task_repo.create_task(
        title="Valid Completed",
        status="completed",
        completed_at=now_str,
    )
    assert valid_completed.status == "completed"

    # 2. status='deleted' without deleted_at -> Constraint error
    with pytest.raises(aiosqlite.IntegrityError):
        await task_repo.create_task(
            title="Invalid Deleted (no deleted_at)",
            status="deleted",
            deleted_at=None,
        )

    # 3. status!='deleted' with deleted_at set -> Constraint error
    with pytest.raises(aiosqlite.IntegrityError):
        await task_repo.create_task(
            title="Invalid Backlog with deleted_at",
            status="backlog",
            deleted_at=now_str,
        )

    # Valid deleted task
    valid_deleted = await task_repo.create_task(
        title="Valid Deleted",
        status="deleted",
        deleted_at=now_str,
    )
    assert valid_deleted.status == "deleted"


@pytest.mark.asyncio
async def test_tasks_unique_index_today_slot(task_repo: TaskRepository) -> None:
    """Verify unique constraint ux_today_slot on slot_index for status='today'."""
    now_str = now_utc_iso()

    # Slot 1 task
    await task_repo.create_task(
        title="Task in Slot 1",
        status="today",
        slot_index=1,
        today_since=now_str,
    )

    # Another task attempting to take Slot 1 -> Unique constraint violation
    with pytest.raises(aiosqlite.IntegrityError):
        await task_repo.create_task(
            title="Duplicate Slot 1 Task",
            status="today",
            slot_index=1,
            today_since=now_str,
        )


@pytest.mark.asyncio
async def test_tasks_other_check_constraints(task_repo: TaskRepository) -> None:
    """Verify kind and is_micro_completed CHECK constraints."""
    # Invalid kind
    with pytest.raises(aiosqlite.IntegrityError):
        await task_repo.create_task(
            title="Invalid kind",
            kind="unknown",
            status="backlog",
        )

    # Invalid is_micro_completed
    with pytest.raises(aiosqlite.IntegrityError):
        await task_repo.create_task(
            title="Invalid is_micro_completed",
            is_micro_completed=2,
            status="backlog",
        )


@pytest.mark.asyncio
async def test_task_repository_crud(task_repo: TaskRepository) -> None:
    """Verify complete CRUD functionality of TaskRepository."""
    # 1. Create
    task = await task_repo.create_task(
        title="Initial Task",
        raw_input="initial input",
        if_then_trigger="When coffee is brewed",
        micro_step="Open IDE",
        status="backlog",
        due_date="2026-10-10T12:00:00.000Z",
    )
    assert task.title == "Initial Task"
    assert task.status == "backlog"

    # 2. Get by ID
    fetched = await task_repo.get_task(task.id)
    assert fetched is not None
    assert fetched.id == task.id
    assert fetched.if_then_trigger == "When coffee is brewed"

    # 3. Update
    updated = await task_repo.update_task(task.id, title="Updated Title", is_micro_completed=1)
    assert updated is not None
    assert updated.title == "Updated Title"
    assert updated.is_micro_completed == 1

    # 4. Soft Delete
    deleted_time = now_utc_iso()
    deleted = await task_repo.soft_delete_task(task.id, deleted_at=deleted_time)
    assert deleted is not None
    assert deleted.status == "deleted"
    assert deleted.deleted_at == deleted_time


@pytest.mark.asyncio
async def test_today_slots_mapping(task_repo: TaskRepository) -> None:
    """Verify get_today_slots returns dictionary with slots 1, 2, 3."""
    now_str = now_utc_iso()
    slots_empty = await task_repo.get_today_slots()
    assert slots_empty == {1: None, 2: None, 3: None}

    # Add slot 2 task
    task2 = await task_repo.create_task(
        title="Slot 2 Task",
        status="today",
        slot_index=2,
        today_since=now_str,
    )
    slots = await task_repo.get_today_slots()
    assert slots[1] is None
    assert slots[2] is not None
    assert slots[2].id == task2.id
    assert slots[3] is None


@pytest.mark.asyncio
async def test_action_logs_generation_cleanup(
    task_repo: TaskRepository,
    action_log_repo: ActionLogRepository,
) -> None:
    """Verify ActionLogRepository preserves latest 3 batches and prunes older ones (Section 6.2)."""
    task = await task_repo.create_task(title="Action Log Target Task")

    # Add 4 sequential batches
    for i in range(1, 5):
        batch_id = f"batch-{i}"
        await action_log_repo.create_log_batch(
            batch_id=batch_id,
            logs=[
                {
                    "task_id": task.id,
                    "action_type": "add",
                    "previous_state": {"slot": i},
                }
            ],
        )

    # Should retain exactly latest 3 batches (batch-4, batch-3, batch-2)
    latest_batches = await action_log_repo.get_latest_batch_ids()
    assert len(latest_batches) == 3
    assert latest_batches == ["batch-4", "batch-3", "batch-2"]

    # batch-1 must have been deleted
    batch1_logs = await action_log_repo.get_logs_by_batch("batch-1")
    assert len(batch1_logs) == 0

    # batch-4 logs are retrieved in LIFO order
    batch4_logs = await action_log_repo.get_logs_by_batch("batch-4")
    assert len(batch4_logs) == 1
    assert batch4_logs[0].batch_id == "batch-4"

    # Delete batch-4
    await action_log_repo.delete_batch("batch-4")
    assert len(await action_log_repo.get_logs_by_batch("batch-4")) == 0


@pytest.mark.asyncio
async def test_action_logs_foreign_key_and_cascade(
    task_repo: TaskRepository,
    action_log_repo: ActionLogRepository,
    db_conn: aiosqlite.Connection,
) -> None:
    """Verify foreign key constraint and ON DELETE CASCADE on tasks(id)."""
    # Inserting action_log with non-existent task_id fails
    with pytest.raises(aiosqlite.IntegrityError):
        await action_log_repo.create_log_batch(
            batch_id="batch-invalid",
            logs=[
                {
                    "task_id": "non-existent-task-id",
                    "action_type": "add",
                }
            ],
        )

    # Physical deletion of task cascades to action_logs
    task = await task_repo.create_task(title="Cascade Test Task")
    await action_log_repo.create_log_batch(
        batch_id="batch-cascade",
        logs=[{"task_id": task.id, "action_type": "add"}],
    )
    logs = await action_log_repo.get_logs_by_batch("batch-cascade")
    assert len(logs) == 1

    # Manually delete task row
    await db_conn.execute("DELETE FROM tasks WHERE id = ?;", (task.id,))
    # Logs should be cascaded and deleted
    logs_after = await action_log_repo.get_logs_by_batch("batch-cascade")
    assert len(logs_after) == 0


@pytest.mark.asyncio
async def test_meta_repository(meta_repo: MetaRepository) -> None:
    """Verify MetaRepository get and set with INSERT OR REPLACE behavior."""
    assert await meta_repo.get_last_daily_job_date() is None

    await meta_repo.set_last_daily_job_date("2026-10-07")
    assert await meta_repo.get_last_daily_job_date() == "2026-10-07"

    # Overwrite (REPLACE)
    await meta_repo.set_last_daily_job_date("2026-10-08")
    assert await meta_repo.get_last_daily_job_date() == "2026-10-08"

    # Morning brief date
    assert await meta_repo.get_last_morning_brief_date() is None
    await meta_repo.set_last_morning_brief_date("2026-10-08")
    assert await meta_repo.get_last_morning_brief_date() == "2026-10-08"


@pytest.mark.asyncio
async def test_physical_delete_old_tasks_undo_protection(
    task_repo: TaskRepository,
    action_log_repo: ActionLogRepository,
    db_conn: aiosqlite.Connection,
) -> None:
    """Verify physical_delete_old_tasks deletes tasks older than 30 days but protects tasks referenced in action_logs (TC-026)."""
    # 1. Old deleted task WITHOUT action_logs (should be physically deleted)
    t1 = await task_repo.create_task(
        title="Old Deleted Task 1 (No Logs)",
        status="deleted",
        deleted_at="2020-01-01T00:00:00.000Z",
    )

    # 2. Old deleted task WITH action_logs (protected by TC-026)
    t2 = await task_repo.create_task(
        title="Old Deleted Task 2 (Has Logs)",
        status="deleted",
        deleted_at="2020-01-01T00:00:00.000Z",
    )
    await action_log_repo.create_log_batch(
        batch_id="batch-protect-tc026",
        logs=[{"task_id": t2.id, "action_type": "delete"}],
    )

    # 3. Recently deleted task (within 30 days, should NOT be deleted)
    now_str = now_utc_iso()
    t3 = await task_repo.create_task(
        title="Recently Deleted Task",
        status="deleted",
        deleted_at=now_str,
    )

    # Run physical cleanup (retention 30 days)
    deleted_count = await task_repo.physical_delete_old_tasks(retention_days=30)
    assert deleted_count == 1

    # Verify statuses in DB
    assert await task_repo.get_task(t1.id) is None  # Permanently deleted
    assert await task_repo.get_task(t2.id) is not None  # Protected!
    assert await task_repo.get_task(t3.id) is not None  # Recent, not deleted


@pytest.mark.asyncio
async def test_bot_message_repository(bot_message_repo: BotMessageRepository) -> None:
    """Verify BotMessageRepository set, get, replace, and delete."""
    assert await bot_message_repo.get_message("today_focus") is None

    await bot_message_repo.set_message("today_focus", "channel_101", "msg_901")
    msg = await bot_message_repo.get_message("today_focus")
    assert msg == ("channel_101", "msg_901")

    # Replace with new message ID
    await bot_message_repo.set_message("today_focus", "channel_101", "msg_902")
    msg_updated = await bot_message_repo.get_message("today_focus")
    assert msg_updated == ("channel_101", "msg_902")

    # Delete
    await bot_message_repo.delete_message("today_focus")
    assert await bot_message_repo.get_message("today_focus") is None


@pytest.mark.asyncio
async def test_transaction_rollback(
    db_conn: aiosqlite.Connection,
    task_repo: TaskRepository,
) -> None:
    """Verify transaction wrapper rolls back on exception."""
    task_id = str(uuid.uuid4())

    with pytest.raises(ValueError):
        async with transaction(db_conn):
            await task_repo.create_task(
                task_id=task_id,
                title="Should Roll Back",
                status="backlog",
            )
            # Raise exception before committing
            raise ValueError("Intentional rollback")

    # The task should not exist in DB
    assert await task_repo.get_task(task_id) is None
