"""Pydantic schemas for Gemini structured JSON output.

Complies with Section 4.2 ② of the Momentum specification (v17).
"""

from __future__ import annotations

from typing import List, Literal, Optional
from pydantic import BaseModel, Field

IntentType = Literal["add", "complete", "did", "edit", "delete"]
TargetBucketType = Literal["today", "backlog"]
ClearFieldType = Literal["if_then_trigger", "micro_step", "due_date"]


class OperationSchema(BaseModel):
    """Represents a single parsed action operation."""
    intent: IntentType = Field(..., description="Action intent")
    target_bucket: Optional[TargetBucketType] = Field(
        None, description="Target destination bucket ('today' | 'backlog' | null)"
    )
    title: Optional[str] = Field(None, description="Cleaned actionable or praised title")
    if_then_trigger: Optional[str] = Field(
        None, description="Trigger for starting the task (e.g. 'When seated at desk')"
    )
    micro_step: Optional[str] = Field(
        None, description="Very small 2-minute first step"
    )
    due_date: Optional[str] = Field(
        None, description="Due date string in ISO 8601 (e.g. YYYY-MM-DDTHH:MM:SS+09:00)"
    )
    target_ref: Optional[str] = Field(
        None, description="Target task reference code (e.g. 'T1', 'T2' or null if ambiguous)"
    )
    clear_fields: List[ClearFieldType] = Field(
        default_factory=list,
        description="Fields to clear to null during edit (e.g. ['due_date'])"
    )


class GeminiResponseSchema(BaseModel):
    """Root structured response schema returned by Gemini API."""
    is_actionable: bool = Field(
        ..., description="Whether input contains actionable tasks or accomplishments"
    )
    reply: Optional[str] = Field(
        None, description="Empathic response, question for ambiguity, or motivational encouragement"
    )
    operations: List[OperationSchema] = Field(
        default_factory=list,
        max_length=5,
        description="List of parsed operations (max 5)"
    )
