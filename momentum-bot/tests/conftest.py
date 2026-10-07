"""Pytest configuration and fixtures for Momentum Bot testing."""

import sys
from pathlib import Path
from typing import AsyncIterator, Generator
import pytest
import pytest_asyncio
import aiosqlite

# Ensure project root is in python path
project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from src.config import Config
from src.core.lock import reset_db_lock
from src.db.connection import create_connection, init_db
from src.db.repository import (
    ActionLogRepository,
    BotMessageRepository,
    MetaRepository,
    TaskRepository,
)


@pytest.fixture(autouse=True)
def reset_lock() -> Generator[None, None, None]:
    """Ensure asyncio.Lock singleton is reset before and after each test."""
    reset_db_lock()
    yield
    reset_db_lock()


@pytest.fixture
def mock_config() -> Config:
    """Fixture providing a valid test Config."""
    return Config(
        DISCORD_TOKEN="test_token_12345",
        GUILD_ID=123456789012345678,
        OWNER_USER_ID=987654321098765432,
        CH_TODAY_FOCUS=1001,
        CH_TASK_INBOX=1002,
        CH_BACKLOG=1003,
        CH_OVERDUE_TASKS=1004,
        CH_DONE_LOG=1005,
        GEMINI_API_KEY="test_gemini_key",
        GEMINI_MODEL="gemini-2.5-flash",
        DAY_BOUNDARY_HOUR=4,
        MORNING_BRIEF_HOUR=8,
        OVERDUE_CHECK_INTERVAL_SEC=60,
        DELETED_RETENTION_DAYS=30,
        TZ="Asia/Tokyo",
        DB_PATH="data/test_momentum.db",
    )


@pytest_asyncio.fixture
async def temp_db_path(tmp_path: Path) -> Path:
    """Provide a path to a temporary database file."""
    db_file = tmp_path / "test_momentum.db"
    await init_db(db_file)
    return db_file


@pytest_asyncio.fixture
async def db_conn(temp_db_path: Path) -> AsyncIterator[aiosqlite.Connection]:
    """Provide an open aiosqlite connection to an initialized temporary DB."""
    conn = await create_connection(temp_db_path)
    try:
        yield conn
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def task_repo(db_conn: aiosqlite.Connection) -> TaskRepository:
    """Provide a TaskRepository bound to the test DB connection."""
    return TaskRepository(db_conn)


@pytest_asyncio.fixture
async def action_log_repo(db_conn: aiosqlite.Connection) -> ActionLogRepository:
    """Provide an ActionLogRepository bound to the test DB connection."""
    return ActionLogRepository(db_conn)


@pytest_asyncio.fixture
async def meta_repo(db_conn: aiosqlite.Connection) -> MetaRepository:
    """Provide a MetaRepository bound to the test DB connection."""
    return MetaRepository(db_conn)


@pytest_asyncio.fixture
async def bot_message_repo(db_conn: aiosqlite.Connection) -> BotMessageRepository:
    """Provide a BotMessageRepository bound to the test DB connection."""
    return BotMessageRepository(db_conn)
