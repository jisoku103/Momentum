"""Main entry point for Momentum Bot application."""

from __future__ import annotations

import logging
import sys

from src.bot.client import MomentumBot
from src.config import load_config


def setup_logging() -> None:
    """Configure stdout logging."""
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def main() -> None:
    """Load configuration and run Momentum Bot."""
    setup_logging()
    logger = logging.getLogger("momentum.main")

    try:
        config = load_config()
    except Exception as e:
        logger.critical(f"Failed to load configuration: {e}")
        sys.exit(1)

    logger.info("Configuration loaded successfully. Starting Momentum Bot...")
    bot = MomentumBot(config)
    bot.run(config.DISCORD_TOKEN)


if __name__ == "__main__":
    main()
