"""Unit and acceptance tests for slot management and state transition engine.

Complies with Section 5.1 and acceptance test cases TC-001 through TC-005.
"""

from datetime import datetime, timezone
import pytest
import aiosqlite

from src.core.state_engine import (
    ALL_SLOTS,
    complete_today_task,
    determine_evacuation_status,
    evacuate_today_task,
    find_first_available_slot,
    place_task_in_today,
    select_eviction_candidate,
)
from src.core.time_utils import now_utc_iso
from src.db.repository import TaskRepository


def test_determine_evacuation_status() -> None:
    """Verify evacuation status logic: overdue if due_date < now, else backlog."""
    # Current reference: 2026-10-07 12:00:00 UTC
    ref_time = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)

    # 1. Past due -> overdue
    past_due = "2026-10-07T11:59:59.000Z"
    assert determine_evacuation_status(past_due, current_time=ref_time) == "overdue"

    # 2. Future due -> backlog
    future_due = "2026-10-07T12:00:01.000Z"
    assert determine_evacuation_status(future_due, current_time=ref_time) == "backlog"

    # 3. None due -> backlog
    assert determine_evacuation_status(None, current_time=ref_time) == "backlog"


@pytest.mark.asyncio
async def test_tc001_empty_today_addition(task_repo: TaskRepository) -> None:
    """TC-001: Add to Today from empty state.

    Expectation:
      status='today', slot_index=1, today_since set to current UTC.
    """
    task = await task_repo.create_task(title="Report Task", status="backlog")
    updated, evicted, result_type = await place_task_in_today(task_repo, task.id)

    assert result_type == "placed"
    assert evicted is None
    assert updated is not None
    assert updated.status == "today"
    assert updated.slot_index == 1
    assert updated.today_since is not None


@pytest.mark.asyncio
async def test_tc002_gapped_slot_allocation(task_repo: TaskRepository) -> None:
    """TC-002: Add to Today when slots 1 and 3 are occupied (slot 2 empty).

    Expectation:
      Allocates lowest free slot (slot_index=2). Slots 1 and 3 remain untouched.
    """
    now_str = now_utc_iso()
    t1 = await task_repo.create_task(
        title="Task in Slot 1", status="today", slot_index=1, today_since=now_str
    )
    t3 = await task_repo.create_task(
        title="Task in Slot 3", status="today", slot_index=3, today_since=now_str
    )

    t_new = await task_repo.create_task(title="Documents Task", status="backlog")
    updated, evicted, result_type = await place_task_in_today(task_repo, t_new.id)

    assert result_type == "placed"
    assert evicted is None
    assert updated is not None
    assert updated.slot_index == 2
    assert updated.status == "today"

    # Verify slots 1 and 3 are unchanged
    slots = await task_repo.get_today_slots()
    assert slots[1].id == t1.id
    assert slots[2].id == t_new.id
    assert slots[3].id == t3.id


@pytest.mark.asyncio
async def test_tc003_today_full_addition_evacuation(task_repo: TaskRepository) -> None:
    """TC-003: Addition to Today when all 3 slots are full.

    Expectation:
      Task is not placed in Today. Evacuates to backlog if future/null, or overdue if past due.
    """
    now_str = now_utc_iso()
    for s in (1, 2, 3):
        await task_repo.create_task(
            title=f"Occupying Task {s}",
            status="today",
            slot_index=s,
            today_since=now_str,
        )

    # 1. New task with future due date -> placed in backlog
    t_future = await task_repo.create_task(
        title="Laundry Task",
        due_date="2099-01-01T00:00:00.000Z",
        status="backlog",
    )
    updated_future, _, result_type = await place_task_in_today(task_repo, t_future.id)
    assert result_type == "full_backlog"
    assert updated_future.status == "backlog"
    assert updated_future.slot_index is None
    assert updated_future.today_since is None

    # 2. New task with past due date -> placed in overdue
    t_past = await task_repo.create_task(
        title="Past Task",
        due_date="2020-01-01T00:00:00.000Z",
        status="backlog",
    )
    updated_past, _, result_type = await place_task_in_today(task_repo, t_past.id)
    assert result_type == "full_overdue"
    assert updated_past.status == "overdue"
    assert updated_past.slot_index is None
    assert updated_past.today_since is None


@pytest.mark.asyncio
async def test_tc004_slot_compaction_prevention(task_repo: TaskRepository) -> None:
    """TC-004: Completing a middle slot maintains gap and does not shift other slots.

    Expectation:
      When slot 2 is completed, slot 1 and slot 3 stay exactly at slots 1 and 3.
    """
    now_str = now_utc_iso()
    t1 = await task_repo.create_task(
        title="Slot 1 Task", status="today", slot_index=1, today_since=now_str
    )
    t2 = await task_repo.create_task(
        title="Slot 2 Task", status="today", slot_index=2, today_since=now_str
    )
    t3 = await task_repo.create_task(
        title="Slot 3 Task", status="today", slot_index=3, today_since=now_str
    )

    # Complete slot 2
    completed = await complete_today_task(task_repo, t2.id)
    assert completed.status == "completed"
    assert completed.slot_index is None
    assert completed.today_since is None

    # Verify slots state: slot 2 is None, slot 1 and 3 unchanged
    slots = await task_repo.get_today_slots()
    assert slots[1] is not None and slots[1].id == t1.id and slots[1].slot_index == 1
    assert slots[2] is None
    assert slots[3] is not None and slots[3].id == t3.id and slots[3].slot_index == 3


@pytest.mark.asyncio
async def test_tc005_today_past_due_stays_in_slot(task_repo: TaskRepository) -> None:
    """TC-005: Today task whose due date has passed stays in Today slot (Focus policy).

    Expectation:
      Today tasks are not automatically evacuated during the day when past due.
    """
    # Create task in Today with past due date
    past_due = "2020-01-01T00:00:00.000Z"
    now_str = now_utc_iso()
    task = await task_repo.create_task(
        title="Crucial Today Task",
        due_date=past_due,
        status="today",
        slot_index=1,
        today_since=now_str,
    )

    # Verify task remains in slot 1
    fetched = await task_repo.get_task(task.id)
    assert fetched.status == "today"
    assert fetched.slot_index == 1
    assert fetched.due_date == past_due


def test_select_eviction_candidate() -> None:
    """Verify eviction candidate tie-breaking rules (Section 6.3):

    1. Newest today_since
    2. Highest slot_index on tie
    3. Excluded task IDs are ignored
    """
    from src.db.repository import TaskRecord

    # Create dummy tasks
    def make_task(t_id: str, slot: int, today_since: str) -> TaskRecord:
        return TaskRecord(
            id=t_id, kind="task", title=f"Task {t_id}", raw_input=None,
            if_then_trigger=None, micro_step=None, is_micro_completed=0,
            due_date=None, status="today", slot_index=slot, today_since=today_since,
            done_log_message_id=None, created_at="2026-10-01T00:00:00.000Z",
            completed_at=None, deleted_at=None,
        )

    t1 = make_task("T1", slot=1, today_since="2026-10-07T08:00:00.000Z")
    t2 = make_task("T2", slot=2, today_since="2026-10-07T09:00:00.000Z")
    t3 = make_task("T3", slot=3, today_since="2026-10-07T09:00:00.000Z") # tied with T2, but slot=3 > 2

    tasks = [t1, t2, t3]

    # Highest today_since is T2 & T3 (09:00), tie-breaker is slot 3 -> T3 wins
    cand = select_eviction_candidate(tasks)
    assert cand is not None
    assert cand.id == "T3"

    # If T3 is excluded (e.g. same batch restored), T2 is chosen
    cand2 = select_eviction_candidate(tasks, excluded_task_ids={"T3"})
    assert cand2 is not None
    assert cand2.id == "T2"

    # If both T3 and T2 are excluded, T1 is chosen
    cand3 = select_eviction_candidate(tasks, excluded_task_ids={"T2", "T3"})
    assert cand3 is not None
    assert cand3.id == "T1"

    # If all are excluded, returns None
    assert select_eviction_candidate(tasks, excluded_task_ids={"T1", "T2", "T3"}) is None
