"""Background periodic loop scheduler for Momentum Bot.

Complies with Chapter 7 of the Momentum specification (v17).
Uses discord.ext.tasks.loop to run:
  - 60-second overdue check and done-log recovery (Section 7.1)
  - 04:00 JST day boundary reset trigger (Section 7.2)
  - 08:00 JST morning brief trigger (Section 7.3)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional
from discord.ext import tasks

from src.core.time_utils import get_business_date, now_jst, now_utc
from src.db.repository import MetaRepository
from src.jobs.daily_reset import execute_daily_reset
from src.jobs.morning_brief import execute_morning_brief
from src.jobs.overdue_checker import check_overdue_and_recover

if TYPE_CHECKING:
    from src.bot.client import MomentumBot

logger = logging.getLogger(__name__)


class JobScheduler:
    """Manages periodic execution loop for background monitoring and scheduled events."""

    def __init__(self, bot: "MomentumBot") -> None:
        self.bot = bot
        self._loop_task: Optional[tasks.Loop] = None

    def start(self) -> None:
        """Start the background task loop."""
        interval_sec = self.bot.config.OVERDUE_CHECK_INTERVAL_SEC

        @tasks.loop(seconds=interval_sec)
        async def _periodic_job() -> None:
            try:
                # 1. 60-second periodic overdue check & done-log recovery (Section 7.1)
                await check_overdue_and_recover(self.bot)

                # 2. Check operational date triggers for daily reset & morning brief
                ref_utc = now_utc()
                ref_jst = now_jst()
                current_b_date = get_business_date(
                    ref_utc, day_boundary_hour=self.bot.config.DAY_BOUNDARY_HOUR
                )

                async with self.bot.get_db() as conn:
                    meta_repo = MetaRepository(conn)
                    last_daily = await meta_repo.get_last_daily_job_date()
                    last_brief = await meta_repo.get_last_morning_brief_date()

                # Trigger 04:00 JST Day Reset when business date flips (Section 7.2 & 7.4)
                if last_daily is not None and last_daily < current_b_date:
                    logger.info(f"Triggering scheduled daily reset for business date {current_b_date}")
                    await execute_daily_reset(self.bot, current_utc=ref_utc)

                # Trigger 08:00 JST Morning Brief when date flips and time is >= 08:00 (Section 7.3 & 7.4)
                if (
                    last_brief is not None
                    and last_brief < current_b_date
                    and ref_jst.hour >= self.bot.config.MORNING_BRIEF_HOUR
                ):
                    logger.info(f"Triggering scheduled morning brief for business date {current_b_date}")
                    await execute_morning_brief(self.bot, current_utc=ref_utc)

            except Exception as e:
                logger.exception(f"Unhandled exception in background scheduler loop: {e}")

        self._loop_task = _periodic_job
        self._loop_task.start()
        logger.info(f"Background scheduler started with {interval_sec}s interval.")

    def stop(self) -> None:
        """Stop the background task loop gracefully."""
        if self._loop_task and self._loop_task.is_running():
            self._loop_task.cancel()
            logger.info("Background scheduler stopped.")
