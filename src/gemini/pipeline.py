"""Natural language processing pipeline for Momentum Bot.

Orchestrates prompt generation, Gemini API analysis (outside lock),
All-or-Nothing validation, state transitions, and Undo batch recording under DB lock.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional
import aiosqlite

from src.core.lock import db_lock_context
from src.core.state_engine import (
    determine_evacuation_status,
    find_first_available_slot,
)
from src.core.time_utils import now_jst, now_utc, now_utc_iso
from src.core.undo_engine import UndoEngine
from src.db.connection import transaction
from src.db.repository import TaskRecord, TaskRepository
from src.gemini.client import GeminiApiError, GeminiClient
from src.gemini.prompts import build_task_snapshot, build_user_prompt
from src.gemini.schemas import GeminiResponseSchema
from src.gemini.validator import (
    ValidatedOperation,
    ValidationResult,
    validate_operations_all_or_nothing,
)


@dataclass
class PipelineResult:
    """Outcome of processing a user message."""
    success: bool
    user_reply: str
    is_actionable: bool = False
    batch_id: Optional[str] = None
    applied_operations: List[Dict[str, Any]] = field(default_factory=list)


async def execute_natural_language_pipeline(
    user_input: str,
    conn: aiosqlite.Connection,
    gemini_client: GeminiClient,
    current_time_utc: Optional[datetime] = None,
) -> PipelineResult:
    """Execute the end-to-end natural language pipeline.

    Steps:
      1. Build snapshot & prompt outside lock.
      2. Call Gemini API outside lock with timeout/retry.
      3. Handle non-actionable input (no DB changes).
      4. Acquire db_lock_context():
         - All-or-Nothing pre-validation.
         - Atomic BEGIN IMMEDIATE transaction.
         - Apply sorted operations and gather previous_state for undo.
         - Record single batch_id into action_logs.
      5. Construct summary reply message.
    """
    ref_utc = current_time_utc or now_utc()
    ref_jst = now_jst()

    task_repo = TaskRepository(conn)
    undo_engine = UndoEngine(conn)

    # 1. Build prompt outside lock
    snapshot_text, t_map = await build_task_snapshot(task_repo, current_utc=ref_utc)
    prompt = build_user_prompt(user_input, snapshot_text, current_time_jst=ref_jst)

    # 2. Call Gemini API outside lock
    try:
        gemini_resp: GeminiResponseSchema = await gemini_client.parse_natural_language(prompt)
    except GeminiApiError as e:
        return PipelineResult(
            success=False,
            user_reply="うまく解析できなかったよ。言い方を変えて送ってみてね！",
            is_actionable=False,
        )

    # 3. Handle non-actionable input (or actionable with empty operations)
    if not gemini_resp.is_actionable or not gemini_resp.operations:
        reply = gemini_resp.reply or "うまく解析できなかったよ。言い方を変えて送ってみてね！"
        return PipelineResult(
            success=True,
            user_reply=reply,
            is_actionable=False,
        )

    # 4. Under DB lock: validate and execute
    async with db_lock_context():
        # Validate under lock with latest DB state
        val_result: ValidationResult = await validate_operations_all_or_nothing(
            operations=gemini_resp.operations,
            t_map=t_map,
            task_repo=task_repo,
            gemini_reply=gemini_resp.reply,
        )

        if not val_result.is_valid:
            # All-or-Nothing abort: no DB changes
            abort_reply = val_result.suggested_reply or (
                f"⚠️ {val_result.error_message}\n（何も変更していないよ）"
            )
            return PipelineResult(
                success=False,
                user_reply=abort_reply,
                is_actionable=True,
            )

        # Execute atomically in transaction
        batch_id = str(uuid.uuid4())
        action_log_entries: List[Dict[str, Any]] = []
        summary_lines: List[str] = []

        async with transaction(conn):
            for vop in val_result.sorted_operations:
                intent = vop.intent

                if intent == "add":
                    dest_bucket = vop.target_bucket or "backlog"
                    assigned_status = "backlog"
                    slot_index = None
                    today_since = None
                    extra_note = ""

                    if dest_bucket == "today":
                        today_tasks = await task_repo.get_today_tasks()
                        free_slot = find_first_available_slot(today_tasks)
                        if free_slot is not None:
                            assigned_status = "today"
                            slot_index = free_slot
                            today_since = now_utc_iso()
                        else:
                            # Full: evacuate using 5.1 rule
                            evac_status = determine_evacuation_status(vop.normalized_due_date, ref_utc)
                            assigned_status = evac_status
                            if evac_status == "overdue":
                                extra_note = "（Today が満杯だったため #overdue-tasks に追加したよ）"
                            else:
                                extra_note = "（Today が満杯だったため控え室に追加したよ）"
                    else:
                        # Backlog target: if due is past -> overdue
                        assigned_status = determine_evacuation_status(vop.normalized_due_date, ref_utc)

                    created_task = await task_repo.create_task(
                        title=vop.title or "タスク",
                        raw_input=user_input,
                        if_then_trigger=vop.if_then_trigger,
                        micro_step=vop.micro_step,
                        due_date=vop.normalized_due_date,
                        status=assigned_status,
                        slot_index=slot_index,
                        today_since=today_since,
                    )

                    action_log_entries.append({
                        "task_id": created_task.id,
                        "action_type": "add",
                        "previous_state": None,
                    })
                    summary_lines.append(f"✨ 追加: 『{created_task.title}』{extra_note}")

                elif intent == "complete":
                    target = vop.target_task
                    assert target is not None
                    prev_state = target.to_dict()

                    await task_repo.update_task(
                        target.id,
                        status="completed",
                        slot_index=None,
                        today_since=None,
                        completed_at=now_utc_iso(),
                        done_log_message_id=None,
                    )
                    action_log_entries.append({
                        "task_id": target.id,
                        "action_type": "complete",
                        "previous_state": prev_state,
                    })
                    summary_lines.append(f"🎉 完了: 『{target.title}』")

                elif intent == "did":
                    did_task = await task_repo.create_task(
                        kind="did",
                        title=vop.title or "実績",
                        raw_input=user_input,
                        status="completed",
                        completed_at=now_utc_iso(),
                    )
                    action_log_entries.append({
                        "task_id": did_task.id,
                        "action_type": "did",
                        "previous_state": None,
                    })
                    summary_lines.append(f"🔥 実績: 『{did_task.title}』")

                elif intent == "delete":
                    target = vop.target_task
                    assert target is not None
                    prev_state = target.to_dict()

                    await task_repo.soft_delete_task(target.id, deleted_at=now_utc_iso())
                    action_log_entries.append({
                        "task_id": target.id,
                        "action_type": "delete",
                        "previous_state": prev_state,
                    })
                    summary_lines.append(f"🗑️ 削除: 『{target.title}』")

                elif intent == "edit":
                    target = vop.target_task
                    assert target is not None
                    prev_state = target.to_dict()

                    update_fields: Dict[str, Any] = {}
                    if vop.title:
                        update_fields["title"] = vop.title
                    if vop.if_then_trigger:
                        update_fields["if_then_trigger"] = vop.if_then_trigger
                    if vop.micro_step:
                        update_fields["micro_step"] = vop.micro_step
                    if vop.normalized_due_date:
                        update_fields["due_date"] = vop.normalized_due_date

                    # Handle clear_fields
                    for cf in vop.clear_fields:
                        update_fields[cf] = None

                    effective_due = update_fields.get("due_date", target.due_date)
                    extra_note = ""

                    # Due date status adjustments (Section 4.3)
                    if "due_date" in update_fields:
                        if target.status == "backlog":
                            if determine_evacuation_status(effective_due, ref_utc) == "overdue":
                                update_fields["status"] = "overdue"
                        elif target.status == "overdue":
                            if determine_evacuation_status(effective_due, ref_utc) == "backlog":
                                update_fields["status"] = "backlog"

                    # Target bucket moving
                    if vop.target_bucket == "today" and target.status != "today":
                        today_tasks = await task_repo.get_today_tasks()
                        free_slot = find_first_available_slot(today_tasks)
                        if free_slot is not None:
                            update_fields["status"] = "today"
                            update_fields["slot_index"] = free_slot
                            update_fields["today_since"] = now_utc_iso()
                        else:
                            # Full: skip movement only
                            extra_note = "（Today が満杯だったため移動はしなかったよ）"

                    elif vop.target_bucket == "backlog" and target.status == "today":
                        evac_status = determine_evacuation_status(effective_due, ref_utc)
                        update_fields["status"] = evac_status
                        update_fields["slot_index"] = None
                        update_fields["today_since"] = None

                    elif vop.target_bucket == "backlog" and target.status == "overdue":
                        if determine_evacuation_status(effective_due, ref_utc) == "overdue":
                            extra_note = "（期限が過ぎているので #overdue-tasks に残しているよ）"

                    await task_repo.update_task(target.id, **update_fields)
                    action_log_entries.append({
                        "task_id": target.id,
                        "action_type": "edit",
                        "previous_state": prev_state,
                    })
                    summary_lines.append(f"✏️ 変更: 『{target.title}』{extra_note}")

            # Record batch into action_logs
            await undo_engine.record_batch(batch_id, action_log_entries)

        # 5. Construct final user response
        response_body = "\n".join(summary_lines)
        if gemini_resp.reply:
            response_body += f"\n\n{gemini_resp.reply}"

        return PipelineResult(
            success=True,
            user_reply=response_body,
            is_actionable=True,
            batch_id=batch_id,
        )
