"""Acceptance and unit tests for Gemini pipeline, All-or-Nothing validation,

and serial queue processing (TC-011 through TC-016).
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
import aiosqlite

from src.config import Config
from src.core.queue import (
    InboxMessageItem,
    SerialMessageQueue,
    validate_message_length,
)
from src.core.time_utils import now_utc_iso
from src.db.repository import TaskRepository
from src.gemini.client import GeminiApiError, GeminiClient
from src.gemini.pipeline import execute_natural_language_pipeline
from src.gemini.prompts import build_task_snapshot, build_user_prompt
from src.gemini.schemas import GeminiResponseSchema, OperationSchema


@pytest.fixture
def mock_gemini_client(mock_config: Config) -> GeminiClient:
    """Fixture providing a mock-backed GeminiClient."""
    client = GeminiClient(mock_config)
    client.parse_natural_language = AsyncMock()
    return client


@pytest.mark.asyncio
async def test_tc011_all_or_nothing_atomic_abort(
    db_conn: aiosqlite.Connection,
    task_repo: TaskRepository,
    mock_gemini_client: GeminiClient,
) -> None:
    """TC-011: Multi-operation All-or-Nothing validation.

    Scenario:
      T1 exists. Input has T1 complete AND non-existent T99 edit.
    Expectation:
      T99 absence is caught during pre-validation under lock.
      All operations are aborted; T1 remains NOT completed (0 DB changes).
    """
    t1 = await task_repo.create_task(
        title="Report Task",
        status="today",
        slot_index=1,
        today_since=now_utc_iso(),
    )

    # Gemini mock returns complete T1 and edit T99
    mock_gemini_client.parse_natural_language.return_value = GeminiResponseSchema(
        is_actionable=True,
        operations=[
            OperationSchema(intent="complete", target_ref="T1"),
            OperationSchema(
                intent="edit",
                target_ref="T99",  # Does not exist!
                due_date="2026-10-10T23:59:59+09:00",
            ),
        ],
    )

    result = await execute_natural_language_pipeline(
        user_input="T1完了。あと存在しないT99の期限を明日にして",
        conn=db_conn,
        gemini_client=mock_gemini_client,
    )

    # Must fail All-or-Nothing validation
    assert result.success is False
    assert "T99" in result.user_reply or "一覧に存在しません" in result.user_reply

    # Crucial check: T1 must NOT be completed! Status must remain 'today'
    t1_check = await task_repo.get_task(t1.id)
    assert t1_check.status == "today"
    assert t1_check.completed_at is None


@pytest.mark.asyncio
async def test_tc012_ambiguity_defense_no_db_change(
    db_conn: aiosqlite.Connection,
    task_repo: TaskRepository,
    mock_gemini_client: GeminiClient,
) -> None:
    """TC-012: Ambiguity defense (guessing prohibited).

    Scenario:
      Two similar tasks exist: 'English Report' and 'Economics Report'.
      Gemini sets target_ref: null with clarification reply.
    Expectation:
      Zero DB changes. Bot responds with confirmation prompt.
    """
    t1 = await task_repo.create_task(title="English Report", status="backlog")
    t2 = await task_repo.create_task(title="Economics Report", status="backlog")

    clarification = "どのレポートのことかな？（T1: 英語レポート、T2: 経済レポート）"
    mock_gemini_client.parse_natural_language.return_value = GeminiResponseSchema(
        is_actionable=True,
        reply=clarification,
        operations=[
            OperationSchema(
                intent="complete",
                target_ref=None,  # Ambiguous!
            )
        ],
    )

    result = await execute_natural_language_pipeline(
        user_input="レポート終わった",
        conn=db_conn,
        gemini_client=mock_gemini_client,
    )

    assert result.success is False
    assert clarification in result.user_reply

    # Both tasks must remain backlog
    assert (await task_repo.get_task(t1.id)).status == "backlog"
    assert (await task_repo.get_task(t2.id)).status == "backlog"


@pytest.mark.asyncio
async def test_tc013_conflicting_operations_defense(
    db_conn: aiosqlite.Connection,
    task_repo: TaskRepository,
    mock_gemini_client: GeminiClient,
) -> None:
    """TC-013: Conflicting operations on the same task in one batch are blocked.

    Scenario:
      T1 exists. Input specifies complete T1 AND edit T1.
    Expectation:
      Pre-validation detects conflict on T1. Entire batch aborted. Zero DB changes.
    """
    t1 = await task_repo.create_task(
        title="Conflict Test Task",
        status="backlog",
    )

    mock_gemini_client.parse_natural_language.return_value = GeminiResponseSchema(
        is_actionable=True,
        operations=[
            OperationSchema(intent="complete", target_ref="T1"),
            OperationSchema(
                intent="edit",
                target_ref="T1",  # Same task target!
                due_date="2026-10-15T18:00:00+09:00",
            ),
        ],
    )

    result = await execute_natural_language_pipeline(
        user_input="T1を完了して、T1の期限を来週にして",
        conn=db_conn,
        gemini_client=mock_gemini_client,
    )

    assert result.success is False
    assert "重複" in result.user_reply or "競合" in result.user_reply

    # T1 remains unchanged in backlog
    t1_check = await task_repo.get_task(t1.id)
    assert t1_check.status == "backlog"
    assert t1_check.completed_at is None


@pytest.mark.asyncio
async def test_tc014_message_length_limit_immediate_cutoff() -> None:
    """TC-014: Messages exceeding 500 characters are blocked immediately before queue enqueueing.

    Expectation:
      501+ character input is rejected without worker processing, without Gemini call.
    """
    # 501 characters
    long_msg = "あ" * 501
    err = validate_message_length(long_msg)
    assert err is not None
    assert "メッセージが長すぎるよ" in err
    assert "501 文字" in err

    # 500 characters passes
    valid_500 = "あ" * 500
    assert validate_message_length(valid_500) is None

    # Test SerialMessageQueue rejection
    reply_mock = AsyncMock()
    handler_mock = AsyncMock()
    queue = SerialMessageQueue(handler=handler_mock)
    queue.start()

    item = InboxMessageItem(
        content=long_msg,
        message_id=1,
        channel_id=2,
        reply_callback=reply_mock,
    )

    enqueued = await queue.enqueue(item)
    assert enqueued is False

    # reply_callback called with error
    reply_mock.assert_awaited_once()
    assert "メッセージが長すぎるよ" in reply_mock.call_args[0][0]

    # Handler never called
    handler_mock.assert_not_called()
    await queue.stop()


def test_tc015_midnight_bucket_rules() -> None:
    """TC-015: Midnight hours (00:00〜04:00 JST) sorting rule in system prompt."""
    from src.gemini.prompts import SYSTEM_PROMPT

    # Confirm system prompt explicitly specifies midnight rules
    assert "深夜帯（00:00〜04:00 JST）" in SYSTEM_PROMPT
    assert "「今日」「今日中」「明日」などのタスク、および期限指定のないタスクは、起きた後の新運用日向けであるため \"backlog\" とする" in SYSTEM_PROMPT


@pytest.mark.asyncio
async def test_tc016_gemini_timeout_and_retry(mock_config: Config) -> None:
    """TC-016: Gemini API 10s timeout, 2s wait retry, and error handling.

    Scenario:
      1st attempt times out (or raises Exception).
      Client waits 2s, tries 2nd attempt.
      2nd attempt also fails -> raises GeminiApiError without making DB changes.
    """
    client = GeminiClient(mock_config)

    # Mock internal caller to raise TimeoutError twice
    with patch.object(client, "_call_generate_content", side_effect=asyncio.TimeoutError("Timeout")):
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            with pytest.raises(GeminiApiError) as exc_info:
                await client.parse_natural_language(
                    user_prompt="test prompt",
                    timeout_seconds=0.1,  # fast test timeout
                    retry_wait_seconds=0.01,
                )

            assert "Gemini API call failed after 1 retry" in str(exc_info.value)
            mock_sleep.assert_awaited_once_with(0.01)


@pytest.mark.asyncio
async def test_full_pipeline_success_scenarios(
    db_conn: aiosqlite.Connection,
    task_repo: TaskRepository,
    mock_gemini_client: GeminiClient,
) -> None:
    """Verify standard success scenarios: add, complete, and did."""
    # 1. Add task to today
    mock_gemini_client.parse_natural_language.return_value = GeminiResponseSchema(
        is_actionable=True,
        reply="応援してるよ！",
        operations=[
            OperationSchema(
                intent="add",
                target_bucket="today",
                title="英語レポートを完成させる",
                if_then_trigger="デスクに座ったら",
                micro_step="Wordを開いてタイトル入力",
                due_date="2026-10-09T18:00:00+09:00",
            )
        ],
    )

    res_add = await execute_natural_language_pipeline(
        user_input="今日英語のレポートやる",
        conn=db_conn,
        gemini_client=mock_gemini_client,
    )
    assert res_add.success is True
    assert "✨ 追加: 『英語レポートを完成させる』" in res_add.user_reply
    assert "応援してるよ！" in res_add.user_reply

    today_tasks = await task_repo.get_today_tasks()
    assert len(today_tasks) == 1
    assert today_tasks[0].title == "英語レポートを完成させる"
    assert today_tasks[0].slot_index == 1

    # 2. Complete task
    mock_gemini_client.parse_natural_language.return_value = GeminiResponseSchema(
        is_actionable=True,
        operations=[
            OperationSchema(
                intent="complete",
                target_ref="T1",
            )
        ],
    )
    res_comp = await execute_natural_language_pipeline(
        user_input="レポート終わった！",
        conn=db_conn,
        gemini_client=mock_gemini_client,
    )
    assert res_comp.success is True
    assert "🎉 完了: 『英語レポートを完成させる』" in res_comp.user_reply

    assert len(await task_repo.get_today_tasks()) == 0
    t_completed = await task_repo.get_task(today_tasks[0].id)
    assert t_completed.status == "completed"

    # 3. Did achievement report
    mock_gemini_client.parse_natural_language.return_value = GeminiResponseSchema(
        is_actionable=True,
        operations=[
            OperationSchema(
                intent="did",
                title="机の上を綺麗に片付けた",
            )
        ],
    )
    res_did = await execute_natural_language_pipeline(
        user_input="机の上片付けた",
        conn=db_conn,
        gemini_client=mock_gemini_client,
    )
    assert res_did.success is True
    assert "🔥 実績: 『机の上を綺麗に片付けた』" in res_did.user_reply
