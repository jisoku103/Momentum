"""Pre-execution validation, UTC normalization, and execution ordering for Gemini operations.

Complies with Section 4.3 of the Momentum specification (v17).
Implements strict All-or-Nothing validation under lock.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Set

from src.core.time_utils import (
    format_utc_iso,
    is_valid_utc_iso,
    now_utc,
    parse_utc_iso,
    to_utc,
)
from src.db.repository import TaskRecord, TaskRepository
from src.gemini.schemas import OperationSchema


@dataclass
class ValidatedOperation:
    """An operation that has passed validation with resolved IDs and normalized values."""
    original_op: OperationSchema
    intent: str
    target_bucket: Optional[str]
    title: Optional[str]
    if_then_trigger: Optional[str]
    micro_step: Optional[str]
    normalized_due_date: Optional[str]
    target_ref: Optional[str]
    target_task_id: Optional[str]
    target_task: Optional[TaskRecord]
    clear_fields: List[str] = field(default_factory=list)


@dataclass
class ValidationResult:
    """Outcome of All-or-Nothing validation."""
    is_valid: bool
    error_message: Optional[str] = None
    suggested_reply: Optional[str] = None
    sorted_operations: List[ValidatedOperation] = field(default_factory=list)


def normalize_iso_due_date(due_str: Optional[str]) -> Optional[str]:
    """Parse and normalize an ISO datetime string into UTC ISO 8601 3-digit millis.

    Returns None if due_str is None or empty.
    Raises ValueError on parse failure.
    """
    if not due_str:
        return None

    cleaned = due_str.strip()
    try:
        if cleaned.endswith("Z"):
            cleaned = cleaned[:-1] + "+00:00"
        dt = datetime.fromisoformat(cleaned)
    except Exception as e:
        raise ValueError(f"Invalid date format '{due_str}': {e}") from e

    return format_utc_iso(dt)


async def validate_operations_all_or_nothing(
    operations: List[OperationSchema],
    t_map: Dict[str, str],
    task_repo: TaskRepository,
    gemini_reply: Optional[str] = None,
) -> ValidationResult:
    """Validate all operations under DB lock with All-or-Nothing semantics (Section 4.3).

    Rules:
      1. complete/edit/delete require unique, valid target_ref (null -> ambiguity error).
      2. Referenced task must exist and have status IN ('today', 'backlog', 'overdue').
      3. No duplicate operations on the same target task in the same batch (TC-013).
      4. Chain operations prohibited (target_ref must be in initial t_map).
      5. due_date must parse and normalize into UTC ISO 8601 millis.
      6. Required fields (title for add/did) must be present.
      7. Any single validation error aborts ALL operations (TC-011).
    """
    if not operations:
        return ValidationResult(
            is_valid=True,
            suggested_reply=gemini_reply,
            sorted_operations=[],
        )

    validated_list: List[ValidatedOperation] = []
    seen_target_ids: Set[str] = set()

    for op in operations:
        intent = op.intent

        # 1. Title validation for add and did
        if intent in ("add", "did"):
            if not op.title or not op.title.strip():
                return ValidationResult(
                    is_valid=False,
                    error_message=f"{intent} 操作にはタイトルが必要です。",
                    suggested_reply=gemini_reply or "タスクのタイトルを教えてね！",
                )

        # 2. Target reference validation for complete, edit, delete
        target_task_id: Optional[str] = None
        target_task: Optional[TaskRecord] = None

        if intent in ("complete", "edit", "delete"):
            if not op.target_ref:
                # Ambiguity or target could not be identified
                ambiguity_msg = gemini_reply or "どのタスクのことか分からなかったよ。タイトルの一部を入れて送ってみてね"
                return ValidationResult(
                    is_valid=False,
                    error_message="対象タスクを特定できませんでした（曖昧性）。",
                    suggested_reply=ambiguity_msg,
                )

            target_ref = op.target_ref.strip()
            if target_ref not in t_map:
                return ValidationResult(
                    is_valid=False,
                    error_message=f"指定されたタスク番号 '{target_ref}' は一覧に存在しません。",
                    suggested_reply=f"'{target_ref}' は現在のタスク一覧に見当たらないよ。",
                )

            target_task_id = t_map[target_ref]

            # 3. Conflict prevention: No duplicate operations on the same task (TC-013)
            if target_task_id in seen_target_ids:
                return ValidationResult(
                    is_valid=False,
                    error_message=f"同一タスク '{target_ref}' に対する競合操作を検知しました。",
                    suggested_reply=f"'{target_ref}' に対する複数の操作が重複しているため、安全のため中断したよ。",
                )
            seen_target_ids.add(target_task_id)

            # Check task existence and status in DB
            target_task = await task_repo.get_task(target_task_id)
            if not target_task:
                return ValidationResult(
                    is_valid=False,
                    error_message=f"タスク '{target_ref}' は見つかりませんでした。",
                    suggested_reply=f"タスク '{target_ref}' は存在しないみたいだよ。",
                )

            valid_statuses = ("today", "backlog", "overdue")
            if target_task.status not in valid_statuses:
                return ValidationResult(
                    is_valid=False,
                    error_message=f"タスク '{target_ref}' の状態({target_task.status})は操作できません。",
                    suggested_reply=f"タスク '{target_ref}' はすでに完了または削除されているよ。",
                )

        # 4. Normalize due_date
        normalized_due: Optional[str] = None
        if op.due_date:
            try:
                normalized_due = normalize_iso_due_date(op.due_date)
            except ValueError as e:
                return ValidationResult(
                    is_valid=False,
                    error_message=f"期限日時の書式が不正です: {e}",
                    suggested_reply="期限の日時がうまく読み取れなかったよ。別の表現で教えてね！",
                )

        validated_list.append(
            ValidatedOperation(
                original_op=op,
                intent=intent,
                target_bucket=op.target_bucket,
                title=op.title.strip() if op.title else None,
                if_then_trigger=op.if_then_trigger.strip() if op.if_then_trigger else None,
                micro_step=op.micro_step.strip() if op.micro_step else None,
                normalized_due_date=normalized_due,
                target_ref=op.target_ref,
                target_task_id=target_task_id,
                target_task=target_task,
                clear_fields=list(op.clear_fields),
            )
        )

    # 5. Sort operations according to Section 4.3 execution order
    # Group 1: Releases slot (complete/delete on Today, edit evacuating from Today)
    # Group 2: No impact on slot (did, other edit, complete/delete on non-Today)
    # Group 3: Consumes slot (add to Today, edit moving to Today)
    def order_key(vop: ValidatedOperation) -> int:
        is_today_target = vop.target_task is not None and vop.target_task.status == "today"

        # Group 1: Releases slot
        if is_today_target and vop.intent in ("complete", "delete"):
            return 1
        if is_today_target and vop.intent == "edit" and vop.target_bucket == "backlog":
            return 1

        # Group 3: Consumes slot
        if vop.intent == "add" and vop.target_bucket == "today":
            return 3
        if vop.intent == "edit" and vop.target_bucket == "today" and not is_today_target:
            return 3

        # Group 2: Neutral
        return 2

    # Stable sort preserving original relative order within same group
    sorted_ops = sorted(validated_list, key=order_key)

    return ValidationResult(
        is_valid=True,
        suggested_reply=gemini_reply,
        sorted_operations=sorted_ops,
    )
