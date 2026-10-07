"""State transition and slot management engine for Momentum Bot.

Complies with Section 5.1 and Section 6.3 of the Momentum specification (v17).
Handles:
  - First available slot detection (slots 1..3, no compaction/gaps preserved)
  - Evacuation destination determination (overdue vs backlog based on due_date)
  - Eviction candidate selection during Today overflows (excluding same-batch restored tasks)
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence, Set

from src.core.time_utils import now_utc, now_utc_iso, parse_utc_iso, to_utc
from src.db.repository import TaskRecord


ALL_SLOTS = (1, 2, 3)


def determine_evacuation_status(
    due_date: Optional[str],
    current_time: Optional[datetime] = None,
) -> str:
    """Determine the destination status when a task leaves Today (Section 5.1).

    Rule:
      - If due_date < current_time(UTC) -> 'overdue'
      - Otherwise (future or NULL) -> 'backlog'

    Note:
      `is_micro_completed` is maintained and not reset during evacuation.
      `slot_index` and `today_since` must become NULL.
    """
    if not due_date:
        return "backlog"

    ref_time = to_utc(current_time) if current_time is not None else now_utc()
    try:
        due_dt = parse_utc_iso(due_date)
    except Exception:
        # If due_date cannot be parsed as valid ISO, treat as backlog safely
        return "backlog"

    if due_dt < ref_time:
        return "overdue"
    return "backlog"


def find_first_available_slot(
    today_tasks: Sequence[TaskRecord],
) -> Optional[int]:
    """Find the lowest available slot number (1, 2, or 3) among Today tasks.

    Returns None if all 3 slots are occupied.
    Preserves gaps without compacting (Section 5.1, TC-001, TC-002, TC-004).
    """
    occupied_slots = {
        task.slot_index
        for task in today_tasks
        if task.status == "today" and task.slot_index in ALL_SLOTS
    }
    for slot in ALL_SLOTS:
        if slot not in occupied_slots:
            return slot
    return None


def select_eviction_candidate(
    today_tasks: Sequence[TaskRecord],
    excluded_task_ids: Optional[Set[str] | Iterable[str]] = None,
) -> Optional[TaskRecord]:
    """Select the task to be evicted from Today when all 3 slots are full (Section 6.3).

    Selection criteria:
      1. Exclude tasks in `excluded_task_ids` (e.g., tasks already restored in the same batch; TC-009).
      2. Choose the task with the most recent (newest) `today_since`.
      3. In case of identical `today_since`, choose the one with the larger `slot_index`.

    Returns:
      TaskRecord to evict, or None if no valid candidate exists (extreme corner case; TC-009).
    """
    excluded = set(excluded_task_ids or ())
    candidates = [
        task
        for task in today_tasks
        if task.status == "today"
        and task.slot_index in ALL_SLOTS
        and task.id not in excluded
    ]

    if not candidates:
        return None

    def sort_key(t: TaskRecord):
        # We want newest today_since first (descending), then largest slot_index first (descending).
        # today_since is UTC ISO string (e.g., '2026-10-07T10:00:00.000Z'), which compares lexicographically.
        ts_str = t.today_since or ""
        slot = t.slot_index or 0
        return (ts_str, slot)

    # Sort ascending and pick the last one (i.e. highest today_since, highest slot_index)
    sorted_candidates = sorted(candidates, key=sort_key)
    return sorted_candidates[-1]


async def place_task_in_today(
    task_repo: TaskRepository,
    task_id: str,
) -> tuple[Optional[TaskRecord], Optional[TaskRecord], str]:
    """Place an existing task into Today slots (Section 5.1).

    Behavior:
      - If Today has a free slot: places into the lowest available slot.
      - If Today is full (3 slots): does NOT place into Today; instead updates
        or keeps in destination status according to evacuation rule (overdue/backlog).

    Returns:
      (updated_task, evicted_task, result_type)
      where result_type is:
        - 'placed': Successfully placed in Today
        - 'full_backlog': Today full, assigned/kept in backlog
        - 'full_overdue': Today full, assigned/kept in overdue
    """
    task = await task_repo.get_task(task_id)
    if not task:
        raise ValueError(f"Task {task_id} not found")

    today_tasks = await task_repo.get_today_tasks()
    slot = find_first_available_slot(today_tasks)

    if slot is not None:
        # Place into lowest available slot
        updated = await task_repo.update_task(
            task_id,
            status="today",
            slot_index=slot,
            today_since=now_utc_iso(),
        )
        return updated, None, "placed"

    # Today is full (3 items)
    dest_status = determine_evacuation_status(task.due_date)
    updated = await task_repo.update_task(
        task_id,
        status=dest_status,
        slot_index=None,
        today_since=None,
    )
    result_type = "full_overdue" if dest_status == "overdue" else "full_backlog"
    return updated, None, result_type


async def complete_today_task(
    task_repo: TaskRepository,
    task_id: str,
    completed_at: Optional[str] = None,
) -> Optional[TaskRecord]:
    """Mark a Today task as completed, releasing the slot without compacting others (Section 5.1, TC-004)."""
    c_time = completed_at or now_utc_iso()
    return await task_repo.update_task(
        task_id,
        status="completed",
        completed_at=c_time,
        slot_index=None,
        today_since=None,
    )


async def evacuate_today_task(
    task_repo: TaskRepository,
    task_id: str,
) -> tuple[Optional[TaskRecord], str]:
    """Evacuate a Today task to backlog or overdue based on due_date (Section 5.1).

    Does not compact other slots. Maintains is_micro_completed.
    """
    task = await task_repo.get_task(task_id)
    if not task:
        raise ValueError(f"Task {task_id} not found")

    dest_status = determine_evacuation_status(task.due_date)
    updated = await task_repo.update_task(
        task_id,
        status=dest_status,
        slot_index=None,
        today_since=None,
    )
    return updated, dest_status

