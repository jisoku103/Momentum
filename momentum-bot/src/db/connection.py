"""Asynchronous SQLite connection management for Momentum Bot.

Complies with Sections 2.1 and 2.2 of the Momentum specification (v17).
Ensures WAL mode, busy_timeout=5000, foreign_keys=ON, and BEGIN IMMEDIATE transactions.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Optional
import aiosqlite

SCHEMA_PATH = Path(__file__).parent / "schema.sql"
DEFAULT_DB_PATH = Path("data/momentum.db")


async def create_connection(
    db_path: str | Path = DEFAULT_DB_PATH,
    set_row_factory: bool = True,
) -> aiosqlite.Connection:
    """Create and configure an aiosqlite connection with mandatory PRAGMA settings.

    Mandatory PRAGMAs (Section 2.2):
      - PRAGMA journal_mode = WAL; (skipped/fallback gracefully for in-memory)
      - PRAGMA busy_timeout = 5000;
      - PRAGMA foreign_keys = ON;
    """
    db_path_str = str(db_path)

    # Ensure parent directory exists for file-based DB
    if db_path_str != ":memory:" and not db_path_str.startswith("file:"):
        parent_dir = Path(db_path_str).parent
        parent_dir.mkdir(parents=True, exist_ok=True)

    # Use isolation_level=None (autocommit mode) so we have full explicit control
    # over BEGIN IMMEDIATE transactions as mandated by Section 2.1.
    conn = await aiosqlite.connect(db_path_str, isolation_level=None)

    if set_row_factory:
        conn.row_factory = aiosqlite.Row

    # Execute mandatory PRAGMAs
    await conn.execute("PRAGMA foreign_keys = ON;")
    await conn.execute("PRAGMA busy_timeout = 5000;")

    # WAL mode is effective for file-based DBs (in-memory SQLite ignores or handles in memory)
    if db_path_str != ":memory:" and not db_path_str.startswith("file:"):
        await conn.execute("PRAGMA journal_mode = WAL;")

    return conn


@asynccontextmanager
async def get_connection(
    db_path: str | Path = DEFAULT_DB_PATH,
) -> AsyncIterator[aiosqlite.Connection]:
    """Async context manager that yields a configured aiosqlite connection and safely closes it."""
    conn = await create_connection(db_path=db_path)
    try:
        yield conn
    finally:
        await conn.close()


@asynccontextmanager
async def transaction(conn: aiosqlite.Connection) -> AsyncIterator[aiosqlite.Connection]:
    """Async context manager that wraps operations in a `BEGIN IMMEDIATE` transaction.

    Complies with Section 2.1 to prevent SQLITE_BUSY deadlocks during write operations.
    """
    await conn.execute("BEGIN IMMEDIATE;")
    try:
        yield conn
        await conn.execute("COMMIT;")
    except Exception:
        await conn.execute("ROLLBACK;")
        raise


async def init_db(
    db_path: str | Path = DEFAULT_DB_PATH,
    schema_file: Optional[str | Path] = None,
) -> None:
    """Initialize database by executing schema.sql.

    Safe to run repeatedly (uses CREATE TABLE IF NOT EXISTS).
    """
    if schema_file is None:
        schema_file = SCHEMA_PATH

    with open(schema_file, "r", encoding="utf-8") as f:
        schema_sql = f.read()

    async with get_connection(db_path) as conn:
        # PRAGMAs are already set by get_connection
        # Execute DDL statements
        await conn.executescript(schema_sql)
