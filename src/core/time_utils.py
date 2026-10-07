"""Time and date utility functions for Momentum Bot.

Complies with Section 1.1 and TC-029 of the Momentum specification (v17).
All internal timestamps use UTC ISO 8601 with 3-digit milliseconds (YYYY-MM-DDTHH:MM:SS.sssZ).
Business day boundary calculation defaults to 04:00 JST.
"""

from __future__ import annotations

import re
from datetime import datetime, date, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

# Timezones
TIMEZONE_JST = ZoneInfo("Asia/Tokyo")
TIMEZONE_UTC = timezone.utc

# Regex for strict UTC ISO 8601 validation: YYYY-MM-DDTHH:MM:SS.sssZ
_UTC_ISO_REGEX = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def now_utc() -> datetime:
    """Return current UTC datetime (timezone-aware)."""
    return datetime.now(timezone.utc)


def now_jst() -> datetime:
    """Return current JST datetime (timezone-aware)."""
    return datetime.now(TIMEZONE_JST)


def to_utc(dt: datetime) -> datetime:
    """Convert any datetime to UTC timezone-aware datetime.

    If dt is naive, it is assumed to be in UTC.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_jst(dt: datetime) -> datetime:
    """Convert any datetime to JST timezone-aware datetime.

    If dt is naive, it is assumed to be in UTC.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TIMEZONE_JST)


def format_utc_iso(dt: datetime) -> str:
    """Format datetime into UTC ISO 8601 with exact 3-digit milliseconds (YYYY-MM-DDTHH:MM:SS.sssZ).

    If dt is naive, it is assumed to be in UTC.
    """
    dt_utc = to_utc(dt)
    millis = dt_utc.microsecond // 1000
    return dt_utc.strftime("%Y-%m-%dT%H:%M:%S") + f".{millis:03d}Z"


def now_utc_iso() -> str:
    """Return current UTC time formatted as UTC ISO 8601 with 3-digit milliseconds."""
    return format_utc_iso(now_utc())


def parse_utc_iso(iso_str: str) -> datetime:
    """Parse UTC ISO 8601 string into a timezone-aware UTC datetime.

    Supports:
      - Strict YYYY-MM-DDTHH:MM:SS.sssZ
      - Standard ISO variants with 'Z' or '+00:00'
    """
    cleaned = iso_str.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    dt = datetime.fromisoformat(cleaned)
    return to_utc(dt)


def is_valid_utc_iso(iso_str: str) -> bool:
    """Check if the string strictly matches YYYY-MM-DDTHH:MM:SS.sssZ format."""
    if not isinstance(iso_str, str):
        return False
    return bool(_UTC_ISO_REGEX.match(iso_str))


def get_business_date(
    dt: Optional[datetime] = None,
    day_boundary_hour: int = 4,
) -> str:
    """Calculate the operational business date (YYYY-MM-DD) for a given datetime.

    Specification (Section 1.1 & TC-029):
      business_date = (now_jst - day_boundary_hour hours).date()

    Boundary behavior (TC-029):
      03:59:59.999 JST -> Previous day
      04:00:00.000 JST -> Current day

    Args:
        dt: The datetime to calculate for. If None, current JST time is used.
        day_boundary_hour: The boundary hour in JST (default: 4).

    Returns:
        String formatted as 'YYYY-MM-DD'.
    """
    if dt is None:
        dt_jst = now_jst()
    else:
        dt_jst = to_jst(dt)

    business_dt = dt_jst - timedelta(hours=day_boundary_hour)
    return business_dt.strftime("%Y-%m-%d")


def format_jst_display(
    dt_or_iso: datetime | str,
    include_weekday: bool = False,
    include_year: bool = False,
) -> str:
    """Format a datetime or UTC ISO string for display in JST.

    Example:
      '10/09 18:00'
      '10/09(金) 18:00'
      '2026/10/09(金) 18:00'
    """
    if isinstance(dt_or_iso, str):
        dt = parse_utc_iso(dt_or_iso)
    else:
        dt = dt_or_iso

    dt_jst = to_jst(dt)
    weekdays_ja = ["月", "火", "水", "木", "金", "土", "日"]
    wd_str = f"({weekdays_ja[dt_jst.weekday()]})" if include_weekday else ""

    if include_year:
        return dt_jst.strftime(f"%Y/%m/%d{wd_str} %H:%M")
    return dt_jst.strftime(f"%m/%d{wd_str} %H:%M")
