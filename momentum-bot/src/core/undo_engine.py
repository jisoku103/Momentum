"""Undo (rollback) engine for Momentum Bot.

Complies with Chapter 6 and acceptance test cases TC-006 through TC-010 of the Momentum specification (v17).
Handles:
  - 3-generation action history management with empty batch protection (Section 6.2, TC-010)
  - LIFO rollback order for standard operations
  - slot_index ascending rollback order for reset_day batches
  - Single-UPDATE Today restoration with slot conflict resolution & eviction (Section 6.3, TC-008, TC-009)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set
import aiosqlite

from src.core.state_engine import (
    ALL_SLOTS,
    determine_evacuation_status,
    find_first_available_slot,
    select_eviction_candidate,
)
from src.core.time_utils import now_utc_iso
from src.db.repository import ActionLogRepository, TaskRecord, TaskRepository


@dataclass
class UndoOperationResult:
    """Details of a single restored task within an undo batch."""
    task_id: str
    title: str
    action_type: str
    restored_status: str
    restored_slot: Optional[int] = None
    evicted_task: Optional[TaskRecord] = None
    done_log_message_id_to_delete: Optional[str] = None
    message: str = ""


@dataclass
class UndoResult:
    """Overall outcome of an undo execution."""
    success: bool
    batch_id: Optional[str] = None
    message: str = ""
    operations: List[UndoOperationResult] = field(default_factory=list)


class UndoEngine:
    """Engine responsible for recording action batches and executing atomic rollbacks."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self.conn = conn
        self.task_repo = TaskRepository(conn)
        self.action_log_repo = ActionLogRepository(conn)

    async def record_batch(
        self,
        batch_id: str,
        logs: List[Dict[str, Any]],
    ) -> bool:
        """Record an action batch into action_logs with empty batch protection (Section 6.2, TC-010).

        Returns:
            bool: True if batch was recorded, False if skipped because logs was empty.
        """
        if not logs:
            # Empty batch prohibition: do not issue batch_id or consume history generations
            return False

        await self.action_log_repo.create_log_batch(batch_id, logs)
        return True

    async def execute_undo(self) -> UndoResult:
        """Execute a rollback of the most recent action batch (Chapter 6).

        Returns:
            UndoResult describing the outcome.
        """
        # 1. Fetch latest batch_id
        latest_batch_ids = await self.action_log_repo.get_latest_batch_ids(limit=1)
        if not latest_batch_ids:
            return UndoResult(
                success=False,
                message="取り消せる直前の操作がありません",
            )

        batch_id = latest_batch_ids[0]
        logs = await self.action_log_repo.get_logs_by_batch(batch_id)
        if not logs:
            # If for any reason no logs exist for this batch_id, clean it up
            await self.action_log_repo.delete_batch(batch_id)
            return UndoResult(
                success=False,
                message="取り消せる直前の操作がありません",
            )

        # 2. Determine ordering (Section 6.2)
        # For reset_day batches: restore in ascending slot_index order (1 -> 2 -> 3)
        # For other batches: standard LIFO order (action_logs.id DESC, which is already returned)
        is_reset_day_batch = any(log.action_type == "reset_day" for log in logs)
        if is_reset_day_batch:
            def sort_by_previous_slot(log_rec):
                try:
                    state = json.loads(log_rec.previous_state or "{}")
                    return state.get("slot_index") or 99
                except Exception:
                    return 99

            ordered_logs = sorted(logs, key=sort_by_previous_slot)
        else:
            ordered_logs = logs

        restored_in_this_batch: Set[str] = set()
        op_results: List[UndoOperationResult] = []

        # 3. Rollback each log entry
        for log in ordered_logs:
            task = await self.task_repo.get_task(log.task_id)
            prev_state: Dict[str, Any] = {}
            if log.previous_state:
                try:
                    prev_state = json.loads(log.previous_state)
                except Exception:
                    prev_state = {}

            task_title = task.title if task else (prev_state.get("title") or "タスク")
            action_type = log.action_type
            done_log_msg_id = task.done_log_message_id if task else None

            # Dispatch based on action_type (Section 6.1)
            if action_type == "add":
                # Soft delete the newly added task
                deleted_time = now_utc_iso()
                await self.task_repo.soft_delete_task(log.task_id, deleted_at=deleted_time)
                op_results.append(
                    UndoOperationResult(
                        task_id=log.task_id,
                        title=task_title,
                        action_type=action_type,
                        restored_status="deleted",
                        message=f"『{task_title}』の追加を取り消しました",
                    )
                )

            elif action_type == "did":
                # Soft delete the did record and signal done-log message deletion
                deleted_time = now_utc_iso()
                await self.task_repo.soft_delete_task(log.task_id, deleted_at=deleted_time)
                op_results.append(
                    UndoOperationResult(
                        task_id=log.task_id,
                        title=task_title,
                        action_type=action_type,
                        restored_status="deleted",
                        done_log_message_id_to_delete=done_log_msg_id,
                        message=f"『{task_title}』の実績報告を取り消しました",
                    )
                )

            elif action_type == "clear_micro":
                # Restore is_micro_completed to 0
                await self.task_repo.update_task(log.task_id, is_micro_completed=0)
                op_results.append(
                    UndoOperationResult(
                        task_id=log.task_id,
                        title=task_title,
                        action_type=action_type,
                        restored_status=task.status if task else "today",
                        message=f"『{task_title}』の初手クリアを取り消しました",
                    )
                )

            elif action_type == "complete":
                # Restore from completed state
                original_status = prev_state.get("status", "backlog")
                evicted_task: Optional[TaskRecord] = None
                restored_slot: Optional[int] = None

                if original_status == "today":
                    pref_slot = prev_state.get("slot_index")
                    fields = {
                        "completed_at": None,
                        "done_log_message_id": None,
                    }
                    updated_task, evicted_task = await self._restore_to_today(
                        task_id=log.task_id,
                        target_fields=fields,
                        preferred_slot=pref_slot,
                        restored_in_this_batch=restored_in_this_batch,
                    )
                    restored_slot = updated_task.slot_index
                    restored_status = updated_task.status
                else:
                    # Restore to backlog or overdue without slot
                    await self.task_repo.update_task(
                        log.task_id,
                        status=original_status,
                        slot_index=None,
                        today_since=None,
                        completed_at=None,
                        done_log_message_id=None,
                    )
                    restored_status = original_status

                op_results.append(
                    UndoOperationResult(
                        task_id=log.task_id,
                        title=task_title,
                        action_type=action_type,
                        restored_status=restored_status,
                        restored_slot=restored_slot,
                        evicted_task=evicted_task,
                        done_log_message_id_to_delete=done_log_msg_id,
                        message=f"『{task_title}』の完了を取り消しました",
                    )
                )

            elif action_type in ("to_backlog", "reset_day"):
                # Task was evacuated from Today; restore back to Today
                pref_slot = prev_state.get("slot_index")
                fields: Dict[str, Any] = {}
                updated_task, evicted_task = await self._restore_to_today(
                    task_id=log.task_id,
                    target_fields=fields,
                    preferred_slot=pref_slot,
                    restored_in_this_batch=restored_in_this_batch,
                )
                op_results.append(
                    UndoOperationResult(
                        task_id=log.task_id,
                        title=task_title,
                        action_type=action_type,
                        restored_status=updated_task.status,
                        restored_slot=updated_task.slot_index,
                        evicted_task=evicted_task,
                        message=f"『{task_title}』のToday復帰を行いました",
                    )
                )

            elif action_type == "to_today":
                # Task was promoted to Today; restore back to original status (backlog or overdue)
                original_status = prev_state.get("status", "backlog")
                await self.task_repo.update_task(
                    log.task_id,
                    status=original_status,
                    slot_index=None,
                    today_since=None,
                )
                op_results.append(
                    UndoOperationResult(
                        task_id=log.task_id,
                        title=task_title,
                        action_type=action_type,
                        restored_status=original_status,
                        message=f"『{task_title}』のToday昇格を取り消しました",
                    )
                )

            elif action_type == "delete":
                # Restore soft-deleted task
                original_status = prev_state.get("status", "backlog")
                evicted_task = None
                restored_slot = None

                if original_status == "today":
                    pref_slot = prev_state.get("slot_index")
                    fields = {"deleted_at": None}
                    # Copy over other fields from prev_state if needed
                    for col in ("title", "if_then_trigger", "micro_step", "due_date"):
                        if col in prev_state:
                            fields[col] = prev_state[col]

                    updated_task, evicted_task = await self._restore_to_today(
                        task_id=log.task_id,
                        target_fields=fields,
                        preferred_slot=pref_slot,
                        restored_in_this_batch=restored_in_this_batch,
                    )
                    restored_status = updated_task.status
                    restored_slot = updated_task.slot_index
                else:
                    fields = {
                        "status": original_status,
                        "slot_index": None,
                        "today_since": None,
                        "deleted_at": None,
                    }
                    for col in ("title", "if_then_trigger", "micro_step", "due_date"):
                        if col in prev_state:
                            fields[col] = prev_state[col]
                    await self.task_repo.update_task(log.task_id, **fields)
                    restored_status = original_status

                op_results.append(
                    UndoOperationResult(
                        task_id=log.task_id,
                        title=task_title,
                        action_type=action_type,
                        restored_status=restored_status,
                        restored_slot=restored_slot,
                        evicted_task=evicted_task,
                        message=f"『{task_title}』の削除を取り消しました",
                    )
                )

            elif action_type == "edit":
                # Restore previous values
                original_status = prev_state.get("status", task.status if task else "backlog")
                evicted_task = None
                restored_slot = None

                # Build restore fields
                restore_fields = {}
                for col in (
                    "title", "raw_input", "if_then_trigger", "micro_step",
                    "is_micro_completed", "due_date"
                ):
                    if col in prev_state:
                        restore_fields[col] = prev_state[col]

                if original_status == "today":
                    pref_slot = prev_state.get("slot_index")
                    updated_task, evicted_task = await self._restore_to_today(
                        task_id=log.task_id,
                        target_fields=restore_fields,
                        preferred_slot=pref_slot,
                        restored_in_this_batch=restored_in_this_batch,
                    )
                    restored_status = updated_task.status
                    restored_slot = updated_task.slot_index
                else:
                    restore_fields["status"] = original_status
                    restore_fields["slot_index"] = None
                    restore_fields["today_since"] = None
                    await self.task_repo.update_task(log.task_id, **restore_fields)
                    restored_status = original_status

                op_results.append(
                    UndoOperationResult(
                        task_id=log.task_id,
                        title=task_title,
                        action_type=action_type,
                        restored_status=restored_status,
                        restored_slot=restored_slot,
                        evicted_task=evicted_task,
                        message=f"『{task_title}』の変更を取り消しました",
                    )
                )

        # 4. Remove the rolled-back action_logs batch record
        await self.action_log_repo.delete_batch(batch_id)

        # 5. Build summary message
        first_op_msg = op_results[0].message if op_results else "直前の操作を取り消しました"
        summary_msg = f"↩️ {first_op_msg}"
        if len(op_results) > 1:
            summary_msg += f"（他 {len(op_results) - 1} 件の操作を含む）"

        # Note any eviction in summary
        evicted_names = [op.evicted_task.title for op in op_results if op.evicted_task]
        if evicted_names:
            summary_msg += f" (スロット満杯のため『{'』『'.join(evicted_names)}』が控え室へ退避しました)"

        return UndoResult(
            success=True,
            batch_id=batch_id,
            message=summary_msg,
            operations=op_results,
        )

    async def _restore_to_today(
        self,
        task_id: str,
        target_fields: Dict[str, Any],
        preferred_slot: Optional[int],
        restored_in_this_batch: Set[str],
    ) -> tuple[TaskRecord, Optional[TaskRecord]]:
        """Restore a task to Today adhering to Section 6.3 slot resolution rules.

        Rule 6.3:
          1. If preferred_slot is empty, restore there.
          2. If preferred_slot is taken and another slot is empty, restore to lowest empty slot.
          3. If all 3 slots are occupied:
             - Evict candidate (newest today_since, descending slot_index tie-breaker; excluding same-batch restored).
             - Evacuate candidate using Section 5.1 rules (overdue if past due, else backlog).
             - If no valid candidate exists (extreme corner case where all 3 are restored in same batch),
               fallback and place this task safely into Backlog or Overdue.
          4. Update target task in a SINGLE UPDATE with status='today', slot_index=chosen_slot, today_since=now.
        """
        today_tasks = await self.task_repo.get_today_tasks()
        occupied_slots = {t.slot_index: t for t in today_tasks if t.slot_index in ALL_SLOTS}

        chosen_slot: Optional[int] = None
        evicted_task: Optional[TaskRecord] = None

        # 1. Preferred slot is empty
        if preferred_slot in ALL_SLOTS and preferred_slot not in occupied_slots:
            chosen_slot = preferred_slot

        # 2. Other slot is empty
        elif len(occupied_slots) < len(ALL_SLOTS):
            chosen_slot = find_first_available_slot(today_tasks)

        # 3. All 3 slots are full: eviction
        else:
            candidate = select_eviction_candidate(
                today_tasks,
                excluded_task_ids=restored_in_this_batch,
            )
            if candidate is not None:
                # Evacuate candidate adhering to 5.1 rules
                dest_status = determine_evacuation_status(candidate.due_date)
                await self.task_repo.update_task(
                    candidate.id,
                    status=dest_status,
                    slot_index=None,
                    today_since=None,
                )
                chosen_slot = candidate.slot_index
                evicted_task = candidate
            else:
                # Extreme corner case: all 3 slots occupied by same-batch restored tasks
                target_task = await self.task_repo.get_task(task_id)
                due_date = target_fields.get("due_date", target_task.due_date if target_task else None)
                dest_status = determine_evacuation_status(due_date)
                target_fields["status"] = dest_status
                target_fields["slot_index"] = None
                target_fields["today_since"] = None
                updated = await self.task_repo.update_task(task_id, **target_fields)
                if updated is None:
                    raise RuntimeError(f"Failed to update task {task_id}")
                return updated, None

        # 4. Single-UPDATE restoration into Today
        target_fields["status"] = "today"
        target_fields["slot_index"] = chosen_slot
        target_fields["today_since"] = now_utc_iso()

        updated = await self.task_repo.update_task(task_id, **target_fields)
        if updated is None:
            raise RuntimeError(f"Failed to update task {task_id}")

        restored_in_this_batch.add(task_id)
        return updated, evicted_task
