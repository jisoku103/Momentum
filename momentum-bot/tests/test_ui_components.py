"""Unit and acceptance tests for Discord UI components, views, message manager,

and slash commands (TC-022 through TC-024).
"""

from unittest.mock import AsyncMock, MagicMock, patch
import pytest
import aiosqlite
import discord

from src.bot.client import MomentumBot
from src.bot.cogs.slash_commands import SlashCommandsCog
from src.bot.renderers.embeds import (
    create_backlog_embed,
    create_overdue_embed,
    create_today_embed,
    format_done_log_message,
)
from src.bot.renderers.message_manager import MessageManager
from src.bot.views.backlog_view import handle_backlog_interaction
from src.bot.views.today_view import handle_today_interaction
from src.config import Config
from src.core.time_utils import now_utc_iso
from src.db.repository import BotMessageRepository, TaskRepository


@pytest.fixture
def mock_bot(mock_config: Config, temp_db_path) -> MomentumBot:
    """Fixture providing MomentumBot instance wired to test DB."""
    bot = MomentumBot(mock_config)
    bot.config.DB_PATH = str(temp_db_path)
    return bot


def _create_mock_interaction(
    user_id: int,
    channel_id: int,
    custom_id: str = "",
    selected_values: list = None,
) -> MagicMock:
    """Helper creating a mocked discord.Interaction."""
    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = MagicMock()
    interaction.user.id = user_id
    interaction.channel_id = channel_id

    interaction.response = MagicMock()
    interaction.response.defer_update = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()

    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()

    interaction.type = discord.InteractionType.component
    interaction.data = {
        "custom_id": custom_id,
        "values": selected_values or [],
    }
    return interaction


@pytest.mark.asyncio
async def test_tc022_parent_message_loss_and_recovery(
    mock_bot: MomentumBot,
    db_conn: aiosqlite.Connection,
) -> None:
    """TC-022: Auto self-healing when parent message is lost (discord.NotFound).

    Scenario:
      Parent message ID is stored in bot_messages, but channel.fetch_message raises discord.NotFound.
    Expectation:
      MessageManager detects NotFound, cleans history, posts new parent message,
      and updates bot_messages table.
    """
    bot_msg_repo = BotMessageRepository(db_conn)
    # 1. Existing ID stored in DB
    await bot_msg_repo.set_message("today_focus", "1001", "88888")

    # 2. Mock channel raising NotFound on fetch_message
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 1001
    channel.name = "today-focus"
    channel.fetch_message = AsyncMock(
        side_effect=discord.NotFound(MagicMock(), "Message not found")
    )
    # channel.send returns newly created message
    new_message = MagicMock()
    new_message.id = 99999
    channel.send = AsyncMock(return_value=new_message)

    # Empty history generator
    async def empty_history(*args, **kwargs):
        if False:
            yield None

    channel.history = empty_history

    mock_bot.get_channel = MagicMock(return_value=channel)

    # 3. Trigger rendering
    await mock_bot.message_manager.render_today_focus()

    # 4. Verify channel.send was called to recreate message
    channel.send.assert_awaited_once()

    # 5. Verify DB was updated with new message ID
    updated_rec = await bot_msg_repo.get_message("today_focus")
    assert updated_rec is not None
    assert updated_rec[1] == "99999"


@pytest.mark.asyncio
async def test_tc023_reboot_component_resilience_and_routing(
    mock_bot: MomentumBot,
    task_repo: TaskRepository,
) -> None:
    """TC-023: Reboot resilience through on_interaction prefix routing.

    Scenario:
      A task exists in slot 1. Bot process restarts (no View in memory).
      User clicks [✅ 完了] with custom_id 'btn:today:done:{task_id}'.
    Expectation:
      on_interaction routes to handle_today_interaction; task is completed in DB;
      screen is re-rendered.
    """
    # Create task in Today slot 1
    task = await task_repo.create_task(
        title="Reboot Test Task",
        status="today",
        slot_index=1,
        today_since=now_utc_iso(),
    )

    # Mock channels
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = mock_bot.config.CH_TODAY_FOCUS
    channel.send = AsyncMock(return_value=MagicMock(id=111))
    async def empty_history(*args, **kwargs):
        if False:
            yield None
    channel.history = empty_history
    mock_bot.get_channel = MagicMock(return_value=channel)

    # Simulate interaction event
    interaction = _create_mock_interaction(
        user_id=mock_bot.config.OWNER_USER_ID,
        channel_id=mock_bot.config.CH_TODAY_FOCUS,
        custom_id=f"btn:today:done:{task.id}",
    )

    # Dispatch to on_interaction
    await mock_bot.on_interaction(interaction)

    # Verify defer_update was called
    interaction.response.defer_update.assert_awaited_once()

    # Verify task was completed in DB
    updated_task = await task_repo.get_task(task.id)
    assert updated_task.status == "completed"
    assert updated_task.slot_index is None


@pytest.mark.asyncio
async def test_tc024_unauthorized_user_rejection(
    mock_bot: MomentumBot,
    task_repo: TaskRepository,
) -> None:
    """TC-024: Unauthorized users are strictly rejected with ephemeral message.

    Expectation:
      User ID != OWNER_USER_ID is rejected with 'このBotは個人用です'.
      No DB changes occur.
    """
    task = await task_repo.create_task(
        title="Protected Task",
        status="today",
        slot_index=1,
        today_since=now_utc_iso(),
    )

    other_user_id = 999999999999  # NOT owner!
    interaction = _create_mock_interaction(
        user_id=other_user_id,
        channel_id=mock_bot.config.CH_TODAY_FOCUS,
        custom_id=f"btn:today:done:{task.id}",
    )

    await handle_today_interaction(mock_bot, interaction, f"btn:today:done:{task.id}")

    # Ephemeral rejection
    interaction.response.send_message.assert_awaited_once_with(
        "このBotは個人用です", ephemeral=True
    )

    # Task in DB MUST remain 'today'
    check_task = await task_repo.get_task(task.id)
    assert check_task.status == "today"


@pytest.mark.asyncio
async def test_backlog_promotion_full_rejection(
    mock_bot: MomentumBot,
    task_repo: TaskRepository,
) -> None:
    """Verify backlog promotion is rejected when Today has 3 items."""
    # Fill Today with 3 tasks
    now_str = now_utc_iso()
    for s in (1, 2, 3):
        await task_repo.create_task(
            title=f"Today Task {s}", status="today", slot_index=s, today_since=now_str
        )

    # Backlog candidate
    backlog_task = await task_repo.create_task(title="Candidate", status="backlog")

    interaction = _create_mock_interaction(
        user_id=mock_bot.config.OWNER_USER_ID,
        channel_id=mock_bot.config.CH_BACKLOG,
        custom_id="select:backlog:promote",
        selected_values=[backlog_task.id],
    )

    await handle_backlog_interaction(mock_bot, interaction, "select:backlog:promote")

    # Should reply with Today full warning
    interaction.followup.send.assert_awaited_once()
    reply_msg = interaction.followup.send.call_args[0][0]
    assert "Today が満杯（3件）です" in reply_msg

    # Backlog task status unchanged
    assert (await task_repo.get_task(backlog_task.id)).status == "backlog"


@pytest.mark.asyncio
async def test_slash_command_channel_restrictions(mock_bot: MomentumBot) -> None:
    """Verify slash commands enforce channel restrictions."""
    cog = SlashCommandsCog(mock_bot)

    # /undo in #backlog (not allowed)
    interaction_undo = _create_mock_interaction(
        user_id=mock_bot.config.OWNER_USER_ID,
        channel_id=mock_bot.config.CH_BACKLOG,  # Disallowed
    )
    await cog.cmd_undo.callback(cog, interaction_undo)
    interaction_undo.followup.send.assert_awaited_once()
    assert "#task-inbox または #today-focus" in interaction_undo.followup.send.call_args[0][0]

    # /reset-day in #today-focus (not allowed, only #task-inbox)
    interaction_reset = _create_mock_interaction(
        user_id=mock_bot.config.OWNER_USER_ID,
        channel_id=mock_bot.config.CH_TODAY_FOCUS,  # Disallowed
    )
    await cog.cmd_reset_day.callback(cog, interaction_reset)
    interaction_reset.followup.send.assert_awaited_once()
    assert "#task-inbox で実行してね" in interaction_reset.followup.send.call_args[0][0]


def test_embed_builders_output() -> None:
    """Verify embed contents for Today, Backlog, Overdue, and DoneLog."""
    from src.db.repository import TaskRecord

    # 1. Today Embed with 1 task
    t1 = TaskRecord(
        id="id1", kind="task", title="Title 1", raw_input=None,
        if_then_trigger="When desk", micro_step="Open IDE", is_micro_completed=1,
        due_date="2026-10-09T09:00:00.000Z", status="today", slot_index=1,
        today_since="2026-10-07T00:00:00.000Z", done_log_message_id=None,
        created_at="2026-10-01T00:00:00.000Z", completed_at=None, deleted_at=None,
    )
    slots = {1: t1, 2: None, 3: None}
    embed_today = create_today_embed(slots, today_done_count=3, total_done_count=10)
    assert "TODAY FOCUS (1/3)" in embed_today.title
    assert "初手クリア済み！" in embed_today.description
    assert "[スロット2] （空きスロット" in embed_today.description

    # 2. Backlog Embed
    embed_backlog = create_backlog_embed([t1], current_page=1, total_pages=1)
    assert "BACKLOG" in embed_backlog.title
    assert "Title 1" in embed_backlog.description

    # 3. Done Log format
    done_msg = format_done_log_message(t1)
    assert "🎉 **達成!** Title 1" in done_msg

    t_did = TaskRecord(
        id="id2", kind="did", title="Did Action", raw_input=None,
        if_then_trigger=None, micro_step=None, is_micro_completed=0,
        due_date=None, status="completed", slot_index=None, today_since=None,
        done_log_message_id=None, created_at="2026-10-01T00:00:00.000Z",
        completed_at=None, deleted_at=None,
    )
    did_msg = format_done_log_message(t_did)
    assert "⚡ **アクション記録!** Did Action" in did_msg


@pytest.mark.asyncio
async def test_inbox_cog_safe_reply_and_on_message(mock_bot: MomentumBot) -> None:
    """Verify InboxCog uses safe_reply with fail_if_not_exists=False without TypeError."""
    from src.bot.cogs.inbox_cog import InboxCog

    cog = InboxCog(mock_bot)

    mock_msg = MagicMock(spec=discord.Message)
    mock_msg.author = MagicMock()
    mock_msg.author.bot = False
    mock_msg.author.id = mock_bot.config.OWNER_USER_ID
    mock_msg.channel = MagicMock()
    mock_msg.channel.id = mock_bot.config.CH_TASK_INBOX
    mock_msg.channel.send = AsyncMock()
    mock_msg.is_system.return_value = False
    mock_msg.content = "タスクを追加して"
    mock_msg.id = 123456789

    mock_ref = MagicMock()
    mock_msg.to_reference.return_value = mock_ref

    # 1. Test _safe_reply
    await cog._safe_reply(mock_msg, "⏳ 整理中…")
    mock_msg.to_reference.assert_called_with(fail_if_not_exists=False)
    mock_msg.channel.send.assert_awaited_with("⏳ 整理中…", reference=mock_ref)

    # 2. Test on_message triggers safe_reply for feedback
    mock_msg.channel.send.reset_mock()
    mock_msg.to_reference.reset_mock()
    with patch.object(cog.queue, "enqueue", new_callable=AsyncMock) as mock_enqueue:
        await cog.on_message(mock_msg)
        mock_msg.to_reference.assert_called_with(fail_if_not_exists=False)
        mock_msg.channel.send.assert_awaited_with("⏳ 整理中…", reference=mock_ref)
        mock_enqueue.assert_awaited_once()

    await cog.cog_unload()

