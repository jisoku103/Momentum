"""Acceptance and unit tests for background jobs and scheduler (TC-017 through TC-021, TC-025, TC-026)."""

from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock
import pytest
import aiosqlite
import discord

from src.bot.client import MomentumBot
from src.config import Config
from src.core.time_utils import get_business_date, now_utc, now_utc_iso
from src.db.repository import ActionLogRepository, MetaRepository, TaskRepository
from src.jobs.catchup import execute_catchup
from src.jobs.daily_reset import execute_daily_reset
from src.jobs.morning_brief import execute_morning_brief, handle_brief_interaction
from src.jobs.overdue_checker import check_overdue_and_recover


@pytest.fixture
def mock_bot_for_jobs(mock_config: Config, temp_db_path) -> MomentumBot:
    """Fixture providing a mock-backed MomentumBot instance for jobs."""
    bot = MomentumBot(mock_config)
    bot.config.DB_PATH = str(temp_db_path)

    # Mock channels
    channel_inbox = MagicMock(spec=discord.TextChannel)
    channel_inbox.id = mock_config.CH_TASK_INBOX
    channel_inbox.name = "task-inbox"
    channel_inbox.send = AsyncMock(return_value=MagicMock(id=5001))

    channel_done = MagicMock(spec=discord.TextChannel)
    channel_done.id = mock_config.CH_DONE_LOG
    channel_done.name = "done-log"
    channel_done.send = AsyncMock(return_value=MagicMock(id=6001))

    channel_today = MagicMock(spec=discord.TextChannel)
    channel_today.id = mock_config.CH_TODAY_FOCUS
    channel_today.name = "today-focus"
    channel_today.send = AsyncMock(return_value=MagicMock(id=7001))

    channel_backlog = MagicMock(spec=discord.TextChannel)
    channel_backlog.id = mock_config.CH_BACKLOG
    channel_backlog.name = "backlog"
    channel_backlog.send = AsyncMock(return_value=MagicMock(id=8001))

    channel_overdue = MagicMock(spec=discord.TextChannel)
    channel_overdue.id = mock_config.CH_OVERDUE_TASKS
    channel_overdue.name = "overdue-tasks"
    channel_overdue.send = AsyncMock(return_value=MagicMock(id=9001))

    channels_map = {
        mock_config.CH_TASK_INBOX: channel_inbox,
        mock_config.CH_DONE_LOG: channel_done,
        mock_config.CH_TODAY_FOCUS: channel_today,
        mock_config.CH_BACKLOG: channel_backlog,
        mock_config.CH_OVERDUE_TASKS: channel_overdue,
    }

    bot.get_channel = MagicMock(side_effect=lambda ch_id: channels_map.get(ch_id))
    return bot


@pytest.mark.asyncio
async def test_tc017_overdue_periodic_migration(
    mock_bot_for_jobs: MomentumBot,
    task_repo: TaskRepository,
    action_log_repo: ActionLogRepository,
) -> None:
    """TC-017: Automatic migration of past due backlog tasks to overdue (Undo exempt).

    Expectation:
      Backlog task with past due_date becomes status='overdue'.
      Aggregated notice sent to #task-inbox.
      NO action_logs batch created.
    """
    past_due = "2026-10-01T00:00:00.000Z"
    ref_time = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)

    # 1. Backlog task that is overdue
    t1 = await task_repo.create_task(
        title="Overdue Report",
        status="backlog",
        due_date=past_due,
    )

    # 2. Backlog task with future due date (should stay backlog)
    t2 = await task_repo.create_task(
        title="Future Task",
        status="backlog",
        due_date="2026-10-10T00:00:00.000Z",
    )

    # Run check
    result = await check_overdue_and_recover(mock_bot_for_jobs, current_utc=ref_time)

    assert len(result.evacuated_tasks) == 1
    assert result.evacuated_tasks[0].id == t1.id

    # Verify DB states
    t1_check = await task_repo.get_task(t1.id)
    assert t1_check.status == "overdue"

    t2_check = await task_repo.get_task(t2.id)
    assert t2_check.status == "backlog"

    # Notification sent
    assert result.notification_sent is not None
    assert "Overdue Report" in result.notification_sent

    # Undo logs must be EMPTY (auto migration is Undo-exempt; Section 7.1)
    logs = await action_log_repo.get_latest_batch_ids()
    assert len(logs) == 0


@pytest.mark.asyncio
async def test_tc018_daily_reset_day_boundary(
    mock_bot_for_jobs: MomentumBot,
    task_repo: TaskRepository,
    meta_repo: MetaRepository,
    action_log_repo: ActionLogRepository,
) -> None:
    """TC-018: 04:00 JST Day boundary automatic reset.

    Scenario:
      Today has 1 past-due task and 1 future task.
    Expectation:
      Past-due task moves to overdue, future task moves to backlog.
      Single batch_id recorded in action_logs.
      meta.last_daily_job_date is updated.
    """
    ref_time = datetime(2026, 10, 8, 4, 0, 0, tzinfo=timezone.utc)
    now_str = now_utc_iso()

    # Task 1: past due in slot 1
    t1 = await task_repo.create_task(
        title="Past Due Today Task",
        status="today",
        slot_index=1,
        today_since=now_str,
        due_date="2026-10-07T00:00:00.000Z",
    )

    # Task 2: future due in slot 2
    t2 = await task_repo.create_task(
        title="Future Today Task",
        status="today",
        slot_index=2,
        today_since=now_str,
        due_date="2026-10-09T00:00:00.000Z",
    )

    res = await execute_daily_reset(mock_bot_for_jobs, current_utc=ref_time)
    assert len(res.evacuated_tasks) == 2
    assert res.batch_id is not None

    # Check destinations
    t1_check = await task_repo.get_task(t1.id)
    assert t1_check.status == "overdue"
    assert t1_check.slot_index is None

    t2_check = await task_repo.get_task(t2.id)
    assert t2_check.status == "backlog"
    assert t2_check.slot_index is None

    # Check meta updated
    b_date = get_business_date(ref_time, day_boundary_hour=mock_bot_for_jobs.config.DAY_BOUNDARY_HOUR)
    assert await meta_repo.get_last_daily_job_date() == b_date

    # Undo batch exists
    batch_logs = await action_log_repo.get_logs_by_batch(res.batch_id)
    assert len(batch_logs) == 2
    assert all(l.action_type == "reset_day" for l in batch_logs)


@pytest.mark.asyncio
async def test_tc019_startup_catchup(
    mock_bot_for_jobs: MomentumBot,
    task_repo: TaskRepository,
    meta_repo: MetaRepository,
) -> None:
    """TC-019: Startup catchup after bot downtime.

    Scenario:
      Bot stopped at 23:00 yesterday (last_daily_job_date is yesterday).
      Bot starts up at 05:00 JST today.
    Expectation:
      execute_catchup detects last_daily_job_date < today_business_date,
      executes daily reset once, and updates meta date.
    """
    now_str = now_utc_iso()
    # Today task
    task = await task_repo.create_task(
        title="Yesterday Unfinished Task",
        status="today",
        slot_index=1,
        today_since=now_str,
    )

    # Seed meta with yesterday's business date
    yesterday_date = "2026-10-06"
    await meta_repo.set_last_daily_job_date(yesterday_date)
    await meta_repo.set_last_morning_brief_date(yesterday_date)

    # Current time: 2026-10-07 05:00 JST (2026-10-06 20:00 UTC) -> business date 2026-10-07
    startup_utc = datetime(2026, 10, 6, 20, 0, 0, tzinfo=timezone.utc)

    catchup_res = await execute_catchup(mock_bot_for_jobs, current_utc=startup_utc)
    assert catchup_res.daily_reset_executed is True

    # Task was evacuated
    check_task = await task_repo.get_task(task.id)
    assert check_task.status == "backlog"

    # Meta was caught up to 2026-10-07
    assert await meta_repo.get_last_daily_job_date() == "2026-10-07"


@pytest.mark.asyncio
async def test_tc020_morning_brief_recommendation_and_promotion(
    mock_bot_for_jobs: MomentumBot,
    task_repo: TaskRepository,
) -> None:
    """TC-020: Morning brief (08:00 JST) candidate selection and interactive promotion.

    Scenario:
      1 Overdue task, 1 task due in 24h, 1 task with no due date.
      Today has 2 free slots.
    Expectation:
      Selects top 2 candidates in priority order (Overdue, then 24h Backlog).
      Button press promotes task to Today and disables button with '✨ 追加済み'.
    """
    ref_time = datetime(2026, 10, 7, 23, 0, 0, tzinfo=timezone.utc)  # 08:00 JST

    # 1. Overdue task (priority 1)
    t_overdue = await task_repo.create_task(
        title="Urgent Overdue Task",
        status="overdue",
        due_date="2026-10-05T00:00:00.000Z",
    )

    # 2. Backlog within 48h (priority 2)
    due_24h = (ref_time + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    t_backlog_48h = await task_repo.create_task(
        title="Near Deadline Task",
        status="backlog",
        due_date=due_24h,
    )

    # 3. Backlog no due date (priority 3)
    t_backlog_nodue = await task_repo.create_task(
        title="Far Future Idea",
        status="backlog",
    )

    # Run morning brief (Today currently has 3 free slots, max 3 candidates)
    brief_res = await execute_morning_brief(mock_bot_for_jobs, current_utc=ref_time)
    assert brief_res.posted is True
    assert len(brief_res.candidates) == 3
    assert brief_res.candidates[0].id == t_overdue.id
    assert brief_res.candidates[1].id == t_backlog_48h.id
    assert brief_res.candidates[2].id == t_backlog_nodue.id

    # Test button promotion interaction
    mock_interaction = MagicMock(spec=discord.Interaction)
    mock_interaction.user.id = mock_bot_for_jobs.config.OWNER_USER_ID
    mock_interaction.response.defer = AsyncMock()
    mock_interaction.message = MagicMock()
    mock_interaction.message.edit = AsyncMock()

    custom_id = f"btn:brief:promote:{t_overdue.id}"
    await handle_brief_interaction(mock_bot_for_jobs, mock_interaction, custom_id)

    # Verify task was promoted to Today
    promoted = await task_repo.get_task(t_overdue.id)
    assert promoted.status == "today"
    assert promoted.slot_index is not None
    assert promoted.today_since is not None


@pytest.mark.asyncio
async def test_tc021_and_tc025_done_log_at_least_once_recovery(
    mock_bot_for_jobs: MomentumBot,
    task_repo: TaskRepository,
) -> None:
    """TC-021 & TC-025: At-Least Once recovery of unposted #done-log messages.

    Scenario:
      Task is marked completed in DB, but done_log_message_id is NULL (e.g. crash or Discord error).
    Expectation:
      Recovery job detects pending log, successfully posts to #done-log,
      and updates done_log_message_id. Task status remains 'completed'.
    """
    now_str = now_utc_iso()
    task = await task_repo.create_task(
        title="Unlogged Completed Task",
        status="completed",
        completed_at=now_str,
        done_log_message_id=None,  # Pending recovery!
    )

    # Run overdue check & recovery
    res = await check_overdue_and_recover(mock_bot_for_jobs)
    assert task.id in res.recovered_done_logs

    # Verify DB now has saved message ID
    recovered_task = await task_repo.get_task(task.id)
    assert recovered_task.done_log_message_id == "6001"
    assert recovered_task.status == "completed"


@pytest.mark.asyncio
async def test_tc026_physical_cleanup_undo_history_protection(
    mock_bot_for_jobs: MomentumBot,
    task_repo: TaskRepository,
    action_log_repo: ActionLogRepository,
) -> None:
    """TC-026: 30-day deleted tasks referenced in action_logs are protected from physical deletion.

    Expectation:
      Tasks older than 30 days are permanently deleted EXCEPT if they are referenced
      in action_logs.
    """
    old_time = "2020-01-01T00:00:00.000Z"

    # Task 1: 30 days old, NO action_logs (must be deleted)
    t1 = await task_repo.create_task(
        title="Unprotected Old Task",
        status="deleted",
        deleted_at=old_time,
    )

    # Task 2: 30 days old, HAS action_logs (must be protected by TC-026)
    t2 = await task_repo.create_task(
        title="Protected Old Task",
        status="deleted",
        deleted_at=old_time,
    )
    await action_log_repo.create_log_batch(
        batch_id="batch-protect-tc026",
        logs=[{"task_id": t2.id, "action_type": "delete"}],
    )

    # Trigger daily reset
    res = await execute_daily_reset(mock_bot_for_jobs)
    assert res.physically_deleted_count == 1

    # Verify t1 is gone, t2 is preserved
    assert await task_repo.get_task(t1.id) is None
    assert await task_repo.get_task(t2.id) is not None
