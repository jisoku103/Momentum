"""Process-wide in-memory exclusive lock for DB transactions and UI rendering queue.

Complies with Section 2.1 of the Momentum specification (v17).
Limits serialization scope strictly to:
  1. SQLite transactions (free slot check through INSERT/UPDATE/DELETE)
  2. Discord pinned parent messages Embed re-rendering
  3. #done-log posting and deletion
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

# Singleton lock instance
_DB_LOCK: Optional[asyncio.Lock] = None


def get_db_lock() -> asyncio.Lock:
    """Get or create the process-wide asyncio.Lock for DB and UI serialization.

    Lazily initializes the lock to ensure compatibility with whichever event loop is running.
    """
    global _DB_LOCK
    if _DB_LOCK is None:
        _DB_LOCK = asyncio.Lock()
    return _DB_LOCK


def reset_db_lock() -> None:
    """Reset the singleton lock instance. Useful for test isolation across loops."""
    global _DB_LOCK
    _DB_LOCK = None


@asynccontextmanager
async def db_lock_context() -> AsyncIterator[None]:
    """Async context manager for acquiring the DB and UI serialization lock.

    Example:
        async with db_lock_context():
            # Perform DB transaction and UI updates safely
            ...
    """
    lock = get_db_lock()
    async with lock:
        yield
