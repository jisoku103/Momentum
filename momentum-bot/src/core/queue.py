"""Serial message input queue and worker for #task-inbox.

Complies with Section 2.1, Section 4.1, and TC-014 of the Momentum specification (v17).
Serializes incoming natural language messages sequentially, enforcing a strict 500-character limit
before queue enqueueing.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Coroutine, Optional
import logging

logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 500


@dataclass
class InboxMessageItem:
    """Item queued for serial processing."""
    content: str
    message_id: Any
    channel_id: Any
    reply_callback: Callable[[str], Coroutine[Any, Any, None]]


def validate_message_length(content: str) -> Optional[str]:
    """Validate that message length does not exceed 500 characters (TC-014).

    Returns error message if invalid, or None if valid.
    """
    length = len(content)
    if length > MAX_MESSAGE_LENGTH:
        return f"メッセージが長すぎるよ（500文字以内にしてね。今回は {length} 文字）"
    return None


class SerialMessageQueue:
    """Asynchronous sequential message processor for #task-inbox."""

    def __init__(
        self,
        handler: Callable[[InboxMessageItem], Coroutine[Any, Any, None]],
    ) -> None:
        self._queue: asyncio.Queue[InboxMessageItem] = asyncio.Queue()
        self._handler = handler
        self._worker_task: Optional[asyncio.Task] = None
        self._is_running = False

    def start(self) -> None:
        """Start the background worker task."""
        if not self._is_running:
            self._is_running = True
            self._worker_task = asyncio.create_task(self._worker_loop())

    async def stop(self) -> None:
        """Stop the background worker task gracefully."""
        self._is_running = False
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None

    async def enqueue(self, item: InboxMessageItem) -> bool:
        """Enqueue a message if it passes length validation.

        If message exceeds 500 characters, calls reply_callback immediately and does NOT enqueue.
        """
        error_msg = validate_message_length(item.content)
        if error_msg:
            # Immediate feedback without queueing (Section 4.1 & TC-014)
            await item.reply_callback(error_msg)
            return False

        await self._queue.put(item)
        return True

    async def _worker_loop(self) -> None:
        """Process messages sequentially one by one in arrival order."""
        while self._is_running:
            try:
                item = await self._queue.get()
                try:
                    await self._handler(item)
                except Exception as e:
                    logger.exception(f"Error handling queued message: {e}")
                finally:
                    self._queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"Unexpected error in queue worker: {e}")
