"""Unit tests for time and business date utilities.

Complies with Section 1.1 and TC-029 of the specification.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import pytest

from src.core.time_utils import (
    TIMEZONE_JST,
    TIMEZONE_UTC,
    format_jst_display,
    format_utc_iso,
    get_business_date,
    is_valid_utc_iso,
    now_jst,
    now_utc,
    now_utc_iso,
    parse_utc_iso,
    to_jst,
    to_utc,
)


def test_utc_iso_format_precision() -> None:
    """Verify UTC ISO 8601 formatting has strict 3-digit millisecond precision and 'Z' suffix."""
    dt = datetime(2026, 10, 7, 12, 34, 56, 789123, tzinfo=timezone.utc)
    iso_str = format_utc_iso(dt)
    assert iso_str == "2026-10-07T12:34:56.789Z"
    assert is_valid_utc_iso(iso_str)

    # Test round trip parsing
    parsed = parse_utc_iso(iso_str)
    assert parsed.year == 2026
    assert parsed.month == 10
    assert parsed.day == 7
    assert parsed.hour == 12
    assert parsed.minute == 34
    assert parsed.second == 56
    assert parsed.microsecond == 789000
    assert parsed.tzinfo == timezone.utc


def test_now_utc_iso() -> None:
    """Verify now_utc_iso() produces a strictly valid UTC ISO string."""
    now_str = now_utc_iso()
    assert is_valid_utc_iso(now_str)
    assert now_str.endswith("Z")


def test_is_valid_utc_iso() -> None:
    """Verify regex validation on valid and invalid strings."""
    assert is_valid_utc_iso("2026-10-07T00:00:00.000Z")
    assert is_valid_utc_iso("2026-01-01T23:59:59.999Z")

    # Invalid cases
    assert not is_valid_utc_iso("2026-10-07T00:00:00Z")         # Missing millis
    assert not is_valid_utc_iso("2026-10-07T00:00:00.00Z")      # 2-digit millis
    assert not is_valid_utc_iso("2026-10-07T00:00:00.0000Z")    # 4-digit millis
    assert not is_valid_utc_iso("2026-10-07T00:00:00.000+09:00")# Not UTC Z
    assert not is_valid_utc_iso("invalid-string")
    assert not is_valid_utc_iso(None)


def test_jst_utc_conversions() -> None:
    """Verify bidirectional conversions between UTC and JST."""
    # 12:00 UTC is 21:00 JST (+9 hours)
    utc_dt = datetime(2026, 10, 7, 12, 0, 0, 0, tzinfo=timezone.utc)
    jst_dt = to_jst(utc_dt)
    assert jst_dt.tzinfo == TIMEZONE_JST
    assert jst_dt.hour == 21
    assert jst_dt.day == 7

    # Converting back to UTC
    back_to_utc = to_utc(jst_dt)
    assert back_to_utc == utc_dt


def test_tc029_business_date_boundary() -> None:
    """Verify Business Date boundary transition at 03:59:59 vs 04:00:00 JST (TC-029).

    Specification:
      business_date = (now_jst - 4 hours).date()
      03:59:59 JST -> Previous day (e.g., 2026-10-07)
      04:00:00 JST -> Current day (e.g., 2026-10-08)
    """
    # 1 second before boundary: 2026-10-08 03:59:59 JST
    dt_before = datetime(2026, 10, 8, 3, 59, 59, 0, tzinfo=TIMEZONE_JST)
    assert get_business_date(dt_before, day_boundary_hour=4) == "2026-10-07"

    # Microsecond before boundary: 2026-10-08 03:59:59.999999 JST
    dt_before_ms = datetime(2026, 10, 8, 3, 59, 59, 999999, tzinfo=TIMEZONE_JST)
    assert get_business_date(dt_before_ms, day_boundary_hour=4) == "2026-10-07"

    # Exactly at boundary: 2026-10-08 04:00:00 JST
    dt_at = datetime(2026, 10, 8, 4, 0, 0, 0, tzinfo=TIMEZONE_JST)
    assert get_business_date(dt_at, day_boundary_hour=4) == "2026-10-08"

    # 1 second after boundary: 2026-10-08 04:00:01 JST
    dt_after = datetime(2026, 10, 8, 4, 0, 1, 0, tzinfo=TIMEZONE_JST)
    assert get_business_date(dt_after, day_boundary_hour=4) == "2026-10-08"


def test_business_date_from_utc_input() -> None:
    """Verify get_business_date behaves identically when passed UTC datetime."""
    # 2026-10-07 18:59:59 UTC = 2026-10-08 03:59:59 JST -> Business Date: 2026-10-07
    utc_before = datetime(2026, 10, 7, 18, 59, 59, tzinfo=timezone.utc)
    assert get_business_date(utc_before, day_boundary_hour=4) == "2026-10-07"

    # 2026-10-07 19:00:00 UTC = 2026-10-08 04:00:00 JST -> Business Date: 2026-10-08
    utc_at = datetime(2026, 10, 7, 19, 0, 0, tzinfo=timezone.utc)
    assert get_business_date(utc_at, day_boundary_hour=4) == "2026-10-08"


def test_business_date_custom_boundary() -> None:
    """Verify boundary hour can be customized (e.g. 5 AM)."""
    # Boundary at 05:00
    dt_at_430 = datetime(2026, 10, 8, 4, 30, 0, tzinfo=TIMEZONE_JST)
    assert get_business_date(dt_at_430, day_boundary_hour=5) == "2026-10-07"

    dt_at_500 = datetime(2026, 10, 8, 5, 0, 0, tzinfo=TIMEZONE_JST)
    assert get_business_date(dt_at_500, day_boundary_hour=5) == "2026-10-08"


def test_format_jst_display() -> None:
    """Verify display formatting in JST with weekday options."""
    # 2026-10-09 09:00 UTC = 2026-10-09 18:00 JST (Friday)
    iso_str = "2026-10-09T09:00:00.000Z"
    assert format_jst_display(iso_str, include_weekday=False) == "10/09 18:00"
    assert format_jst_display(iso_str, include_weekday=True) == "10/09(金) 18:00"
    assert format_jst_display(iso_str, include_weekday=True, include_year=True) == "2026/10/09(金) 18:00"
