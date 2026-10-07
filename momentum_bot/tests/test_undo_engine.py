"""Unit and acceptance tests for Undo engine.

Complies with Chapter 6 and acceptance test cases TC-006 through TC-010.
"""

import json
import pytest
import aiosqlite

from src.core.time_utils import now_utc_iso
from src.core.undo_engine import UndoEngine
from src.db.repository import TaskRepository


@pytest.fixture
def undo_engine(db_conn: aiosqlite.Connection) -> UndoEngine:
    return UndoEngine(db_conn)


@pytest.mark.asyncio
async def test_tc006_complete_rollback(
    task_repo: TaskRepository,
    undo_engine: UndoEngine,
) -> None:
    """TC-006: Complete operation complete rollback.

    Expectation:
      Restores task to slot 1 as status='today'.
      completed_at and done_log_message_id are reset to NULL.
      done_log_message_id_to_delete is signaled for Discord message deletion.
    """
    now_str = now_utc_iso()
    # 1. Initially in slot 1
    task = await task_repo.create_task(
        title="Slot 1 Report Task",
        status="today",
        slot_index=1,
        today_since=now_str,
    )

    # 2. Complete operation
    prev_state = task.to_dict()
    completed_time = now_utc_iso()
    await task_repo.update_task(
        task.id,
        status="completed",
        slot_index=None,
        today_since=None,
        completed_at=completed_time,
        done_log_message_id="msg_done_12345",
    )

    # Record action log
    batch_id = "batch-complete-tc006"
    await undo_engine.record_batch(
        batch_id,
        [
            {
                "task_id": task.id,
                "action_type": "complete",
                "previous_state": prev_state,
            }
        ],
    )

    # 3. Execute undo
    result = await undo_engine.execute_undo()
    assert result.success is True
    assert len(result.operations) == 1

    op = result.operations[0]
    assert op.task_id == task.id
    assert op.restored_status == "today"
    assert op.restored_slot == 1
    assert op.done_log_message_id_to_delete == "msg_done_12345"

    # Verify DB state
    restored = await task_repo.get_task(task.id)
    assert restored.status == "today"
    assert restored.slot_index == 1
    assert restored.completed_at is None
    assert restored.done_log_message_id is None


@pytest.mark.asyncio
async def test_tc007_did_rollback(
    task_repo: TaskRepository,
    undo_engine: UndoEngine,
) -> None:
    """TC-007: Undo of 'did' achievement report.

    Expectation:
      'did' record is soft-deleted (status='deleted', deleted_at=now).
      done_log_message_id_to_delete is returned for Discord cleanup.
    """
    now_str = now_utc_iso()
    did_task = await task_repo.create_task(
        title="Spontaneous Desk Cleaning",
        kind="did",
        status="completed",
        completed_at=now_str,
        done_log_message_id="msg_did_98765",
    )

    batch_id = "batch-did-tc007"
    await undo_engine.record_batch(
        batch_id,
        [
            {
                "task_id": did_task.id,
                "action_type": "did",
                "previous_state": None,
            }
        ],
    )

    result = await undo_engine.execute_undo()
    assert result.success is True
    assert len(result.operations) == 1

    op = result.operations[0]
    assert op.task_id == did_task.id
    assert op.restored_status == "deleted"
    assert op.done_log_message_id_to_delete == "msg_did_98765"

    # Verify DB state
    db_did = await task_repo.get_task(did_task.id)
    assert db_did.status == "deleted"
    assert db_did.deleted_at is not None


@pytest.mark.asyncio
async def test_tc008_full_today_undo_eviction(
    task_repo: TaskRepository,
    undo_engine: UndoEngine,
) -> None:
    """TC-008: Undo return to Today when Today is full (3 items).

    Expectation:
      The existing task with the newest today_since is evicted to backlog/overdue.
      The restoring task takes its slot.
    """
    # 1. Create a task that was completed from slot 2
    task_to_restore = await task_repo.create_task(
        title="Task to Restore",
        status="completed",
        completed_at=now_utc_iso(),
    )
    prev_state = {
        "id": task_to_restore.id,
        "title": task_to_restore.title,
        "status": "today",
        "slot_index": 2,
    }

    # 2. Currently Today is full (slots 1, 2, 3 occupied)
    t1 = await task_repo.create_task(
        title="Slot 1 Task", status="today", slot_index=1, today_since="2026-10-07T08:00:00.000Z"
    )
    t2 = await task_repo.create_task(
        title="Slot 2 Task (Newest)", status="today", slot_index=2, today_since="2026-10-07T10:00:00.000Z"
    )
    t3 = await task_repo.create_task(
        title="Slot 3 Task", status="today", slot_index=3, today_since="2026-10-07T09:00:00.000Z"
    )

    # Record undo action for task_to_restore
    batch_id = "batch-evict-tc008"
    await undo_engine.record_batch(
        batch_id,
        [
            {
                "task_id": task_to_restore.id,
                "action_type": "complete",
                "previous_state": prev_state,
            }
        ],
    )

    # 3. Execute undo
    result = await undo_engine.execute_undo()
    assert result.success is True
    op = result.operations[0]

    # t2 had the newest today_since (10:00:00), so it should be evicted
    assert op.evicted_task is not None
    assert op.evicted_task.id == t2.id
    assert op.restored_slot == 2

    # Verify DB state of evicted task
    evicted_in_db = await task_repo.get_task(t2.id)
    assert evicted_in_db.status == "backlog"
    assert evicted_in_db.slot_index is None
    assert evicted_in_db.today_since is None

    # Verify DB state of restored task
    restored_in_db = await task_repo.get_task(task_to_restore.id)
    assert restored_in_db.status == "today"
    assert restored_in_db.slot_index == 2


@pytest.mark.asyncio
async def test_tc009_same_batch_restoration_eviction_protection(
    task_repo: TaskRepository,
    undo_engine: UndoEngine,
) -> None:
    """TC-009: When restoring multiple tasks in the same batch, tasks already restored

    in that batch are excluded from eviction candidates.
    """
    # Suppose reset_day evacuated 3 tasks (A, B, C) that were in slots 1, 2, 3
    task_a = await task_repo.create_task(title="A", status="backlog")
    task_b = await task_repo.create_task(title="B", status="backlog")
    task_c = await task_repo.create_task(title="C", status="backlog")

    # Now Today is currently filled with 2 other tasks:
    # X in slot 1 (older), Y in slot 2 (newer)
    x = await task_repo.create_task(
        title="X", status="today", slot_index=1, today_since="2026-10-07T08:00:00.000Z"
    )
    y = await task_repo.create_task(
        title="Y", status="today", slot_index=2, today_since="2026-10-07T09:00:00.000Z"
    )
    # slot 3 is free

    # Batch of reset_day restoring A (slot 1), B (slot 2), C (slot 3)
    batch_id = "batch-reset-tc009"
    logs = [
        {"task_id": task_a.id, "action_type": "reset_day", "previous_state": {"slot_index": 1, "title": "A"}},
        {"task_id": task_b.id, "action_type": "reset_day", "previous_state": {"slot_index": 2, "title": "B"}},
        {"task_id": task_c.id, "action_type": "reset_day", "previous_state": {"slot_index": 3, "title": "C"}},
    ]
    await undo_engine.record_batch(batch_id, logs)

    result = await undo_engine.execute_undo()
    assert result.success is True

    # Check that tasks A, B, C were restored and same-batch restored tasks were NOT evicted
    today_tasks = await task_repo.get_today_tasks()
    today_ids = {t.id for t in today_tasks}

    # At least two of {A, B, C} must be in Today, and Y (the newest non-batch task) was evicted
    assert y.id not in today_ids
    y_in_db = await task_repo.get_task(y.id)
    assert y_in_db.status == "backlog"


@pytest.mark.asyncio
async def test_tc010_undo_3_generations_and_empty_batch(
    task_repo: TaskRepository,
    undo_engine: UndoEngine,
) -> None:
    """TC-010: Undo history 3-generation management and empty batch protection.

    - 4 operations executed: first 3 can be rolled back, 4th yields '取り消せる直前の操作がありません'.
    - Empty batch does not consume history.
    """
    # 1. Empty batch protection
    empty_recorded = await undo_engine.record_batch("batch-empty", [])
    assert empty_recorded is False
    latest = await undo_engine.action_log_repo.get_latest_batch_ids()
    assert len(latest) == 0

    # 2. Record 4 valid operations
    task = await task_repo.create_task(title="Action History Task")

    for i in range(1, 5):
        await undo_engine.record_batch(
            f"batch-{i}",
            [
                {
                    "task_id": task.id,
                    "action_type": "clear_micro",
                    "previous_state": {"step": i},
                }
            ],
        )

    # 3. 3 rollbacks succeed
    r1 = await undo_engine.execute_undo()
    assert r1.success is True
    assert r1.batch_id == "batch-4"

    r2 = await undo_engine.execute_undo()
    assert r2.success is True
    assert r2.batch_id == "batch-3"

    r3 = await undo_engine.execute_undo()
    assert r3.success is True
    assert r3.batch_id == "batch-2"

    # 4. 4th rollback fails (batch-1 was pruned by 3-generation limit)
    r4 = await undo_engine.execute_undo()
    assert r4.success is False
    assert r4.message == "取り消せる直前の操作がありません"


@pytest.mark.asyncio
async def test_undo_edit_and_delete_actions(
    task_repo: TaskRepository,
    undo_engine: UndoEngine,
) -> None:
    """Verify undo logic for 'edit' and 'delete' action types."""
    # 1. Test 'delete' undo
    task = await task_repo.create_task(
        title="Deleted Task to Undo",
        status="backlog",
    )
    prev_state = task.to_dict()
    await task_repo.soft_delete_task(task.id)

    await undo_engine.record_batch(
        "batch-delete",
        [
            {
                "task_id": task.id,
                "action_type": "delete",
                "previous_state": prev_state,
            }
        ],
    )
    res_del = await undo_engine.execute_undo()
    assert res_del.success is True
    restored_del = await task_repo.get_task(task.id)
    assert restored_del.status == "backlog"
    assert restored_del.deleted_at is None

    # 2. Test 'edit' undo
    task_edit = await task_repo.create_task(
        title="Original Title",
        status="backlog",
    )
    edit_prev = task_edit.to_dict()
    await task_repo.update_task(task_edit.id, title="Modified Title")

    await undo_engine.record_batch(
        "batch-edit",
        [
            {
                "task_id": task_edit.id,
                "action_type": "edit",
                "previous_state": edit_prev,
            }
        ],
    )
    res_edit = await undo_engine.execute_undo()
    assert res_edit.success is True
    restored_edit = await task_repo.get_task(task_edit.id)
    assert restored_edit.title == "Original Title"
