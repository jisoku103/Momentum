"""Main entry point for Momentum Bot application."""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from aiohttp import web
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


async def handle_ping(request: web.Request) -> web.Response:
    """Health check endpoint for Render."""
    return web.Response(text="Bot is running!")


async def start_web_server(logger: logging.Logger) -> web.AppRunner:
    """Start minimal HTTP server to satisfy Render port detection."""
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/healthz", handle_ping)

    runner = web.AppRunner(app)
    await runner.setup()

    # Render sets the PORT environment variable automatically (e.g., 10000)
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Health-check web server started on port {port}.")
    return runner


async def async_main() -> None:
    """Run Momentum Bot and the health-check web server."""
    logger = logging.getLogger("momentum.main")

    try:
        config = load_config()
    except Exception as e:
        logger.critical(f"Failed to load configuration: {e}")
        sys.exit(1)

    logger.info("Configuration loaded successfully. Starting Momentum Bot...")
    bot = MomentumBot(config)

    # 1. Start the dummy web server
    runner = await start_web_server(logger)

    # 2. Start the Discord bot
    try:
        async with bot:
            await bot.start(config.DISCORD_TOKEN)
    finally:
        await runner.cleanup()


def main() -> None:
    """Load configuration and run Momentum Bot."""
    setup_logging()
    asyncio.run(async_main())


if __name__ == "__main__":
    main()