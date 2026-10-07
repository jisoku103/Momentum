"""Unit tests for configuration loading and validation."""

import pytest
from pydantic import ValidationError

from src.config import Config, load_config


def test_config_defaults(mock_config: Config) -> None:
    """Verify default values match Chapter 11."""
    assert mock_config.DAY_BOUNDARY_HOUR == 4
    assert mock_config.MORNING_BRIEF_HOUR == 8
    assert mock_config.OVERDUE_CHECK_INTERVAL_SEC == 60
    assert mock_config.DELETED_RETENTION_DAYS == 30
    assert mock_config.TZ == "Asia/Tokyo"
    assert mock_config.DB_PATH == "data/test_momentum.db"


def test_config_missing_mandatory_field() -> None:
    """Verify ValidationError when mandatory fields are missing."""
    with pytest.raises(ValidationError):
        Config(
            DISCORD_TOKEN="token",
            # Missing GUILD_ID, OWNER_USER_ID, channel IDs, GEMINI_MODEL
        )


def test_config_type_coercion() -> None:
    """Verify integer string values are coerced to int."""
    cfg = Config(
        DISCORD_TOKEN="token",
        GUILD_ID="12345",  # string coerced to int
        OWNER_USER_ID="67890",
        CH_TODAY_FOCUS="1",
        CH_TASK_INBOX="2",
        CH_BACKLOG="3",
        CH_OVERDUE_TASKS="4",
        CH_DONE_LOG="5",
        GEMINI_MODEL="gemini-2.5-flash",
    )
    assert cfg.GUILD_ID == 12345
    assert cfg.OWNER_USER_ID == 67890
    assert cfg.CH_TODAY_FOCUS == 1
