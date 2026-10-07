"""Startup catchup and duplicate prevention logic.

Complies with Section 7.4, TC-019, TC-021, and TC-025 of the Momentum specification (v17).
Executes on bot startup (on_ready) before starting background loops.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from src.core.lock import db_lock_context
from src.core.time_utils import get_business_date, now_jst, now_utc, to_jst
from src.db.repository import MetaRepository
from src.jobs.daily_reset import execute_daily_reset
from src.jobs.morning_brief import execute_morning_brief
from src.jobs.overdue_checker import check_overdue_and_recover

if TYPE_CHECKING:
    from src.bot.client import MomentumBot

logger = logging.getLogger(__name__)


@dataclass
class CatchupResult:
    """Outcome of startup catchup sequence."""
    daily_reset_executed: bool = False
    morning_brief_executed: bool = False
    done_logs_recovered_count: int = 0
    initial_keys_seeded: bool = False


async def execute_catchup(
    bot: "MomentumBot",
    current_utc: Optional[datetime] = None,
) -> CatchupResult:
    """Execute Section 7.4 catchup logic upon bot startup.

    Protocol:
      1. If meta keys are missing (first boot), seed with current business date and skip jobs.
      2. If last_daily_job_date < today_business_date: execute daily reset once (TC-019).
      3. If last_morning_brief_date < today_business_date AND current JST hour >= 8: execute morning brief.
      4. Always run done-log recovery for unsent messages (TC-021, TC-025).
    """
    ref_utc = current_utc or now_utc()
    ref_jst = to_jst(ref_utc)
    current_b_date = get_business_date(ref_utc, day_boundary_hour=bot.config.DAY_BOUNDARY_HOUR)

    result = CatchupResult()

    async with db_lock_context():
        async with bot.get_db() as conn:
            meta_repo = MetaRepository(conn)

            last_daily = await meta_repo.get_last_daily_job_date()
            last_brief = await meta_repo.get_last_morning_brief_date()

            # 1. First startup initialization: seed missing keys and skip (Section 7.4)
            needs_daily_seed = last_daily is None
            needs_brief_seed = last_brief is None

            if needs_daily_seed:
                await meta_repo.set_last_daily_job_date(current_b_date)
                last_daily = current_b_date
                result.initial_keys_seeded = True

            if needs_brief_seed:
                await meta_repo.set_last_morning_brief_date(current_b_date)
                last_brief = current_b_date
                result.initial_keys_seeded = True

            # If both were seeded fresh, no missed jobs occurred
            if needs_daily_seed and needs_brief_seed:
                logger.info("Initialized meta date keys on first run. Skipping catchup jobs.")
                # Still run done-log recovery
                overdue_res = await check_overdue_and_recover(bot, current_utc=ref_utc)
                result.done_logs_recovered_count = len(overdue_res.recovered_done_logs)
                return result

    # 2. Daily reset catchup (last_daily_job_date < current_b_date)
    if last_daily is not None and last_daily < current_b_date:
        logger.info(f"Catchup: last daily job was {last_daily}, catching up to {current_b_date}")
        await execute_daily_reset(bot, current_utc=ref_utc)
        result.daily_reset_executed = True

    # 3. Morning brief catchup (last_morning_brief_date < current_b_date and JST hour >= MORNING_BRIEF_HOUR)
    if (
        last_brief is not None
        and last_brief < current_b_date
        and ref_jst.hour >= bot.config.MORNING_BRIEF_HOUR
    ):
        logger.info(f"Catchup: last morning brief was {last_brief}, catching up to {current_b_date}")
        await execute_morning_brief(bot, current_utc=ref_utc)
        result.morning_brief_executed = True

    # 4. Done-log unsent recovery on startup (Section 7.4)
    overdue_res = await check_overdue_and_recover(bot, current_utc=ref_utc)
    result.done_logs_recovered_count = len(overdue_res.recovered_done_logs)

    return result
