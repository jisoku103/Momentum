"""Configuration management for Momentum Bot.

Complies with Chapter 11 of the Momentum specification (v17).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError


class Config(BaseModel):
    """Application configuration loaded from environment variables."""

    # Discord credentials & targeting
    DISCORD_TOKEN: str = Field(..., description="Discord Bot token")
    GUILD_ID: int = Field(..., description="Target Discord Guild (Server) ID")
    OWNER_USER_ID: int = Field(..., description="Single authorized owner user ID")

    # Channel IDs
    CH_TODAY_FOCUS: int = Field(..., description="#today-focus channel ID")
    CH_TASK_INBOX: int = Field(..., description="#task-inbox channel ID")
    CH_BACKLOG: int = Field(..., description="#backlog channel ID")
    CH_OVERDUE_TASKS: int = Field(..., description="#overdue-tasks channel ID")
    CH_DONE_LOG: int = Field(..., description="#done-log channel ID")

    # Gemini API settings
    GEMINI_API_KEY: Optional[str] = Field(None, description="Google Gemini API key")
    GEMINI_MODEL: str = Field(..., description="Google Gemini model name (mandatory)")

    # System parameters with defaults (Chapter 11)
    DAY_BOUNDARY_HOUR: int = Field(
        default=4, description="Day boundary hour in JST (default: 4 = 04:00 JST)"
    )
    MORNING_BRIEF_HOUR: int = Field(
        default=8, description="Morning brief post hour in JST (default: 8 = 08:00 JST)"
    )
    OVERDUE_CHECK_INTERVAL_SEC: int = Field(
        default=60, description="Overdue check interval in seconds (default: 60)"
    )
    DELETED_RETENTION_DAYS: int = Field(
        default=30, description="Retention days for soft-deleted tasks (default: 30)"
    )
    TZ: str = Field(
        default="Asia/Tokyo", description="Timezone name for scheduling and display"
    )

    # Database file path
    DB_PATH: str = Field(
        default="data/momentum.db", description="Path to SQLite database file"
    )


def load_config(
    env_file: Optional[str | Path] = None,
    override_values: Optional[dict] = None,
) -> Config:
    """Load configuration from environment variables or .env file.

    Args:
        env_file: Optional path to .env file.
        override_values: Optional dict to override environment variables.

    Returns:
        Config: Validated application configuration instance.

    Raises:
        ValidationError: If mandatory fields are missing or invalid types.
    """
    if env_file:
        load_dotenv(dotenv_path=env_file, override=True)
    else:
        load_dotenv(override=False)

    env_data: dict[str, str | int] = {}

    # Read defined fields from os.environ
    for field_name in Config.model_fields.keys():
        val = os.getenv(field_name)
        if val is not None:
            env_data[field_name] = val

    if override_values:
        env_data.update(override_values)

    return Config(**env_data)
