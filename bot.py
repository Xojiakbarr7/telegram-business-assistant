#!/usr/bin/env python3
"""Telegram Business Assistant Bot - Entry point."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramUnauthorizedError

from assistant.config import (
    ConfigError,
    SETTING_KEYS,
    load_settings,
)
from assistant.handlers import ALLOWED_UPDATES, setup_dispatcher
from assistant.service import Assistant, build_assistant, make_claude_cli


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def setup_data_dirs(settings) -> None:
    """Ensure data directories exist with correct permissions."""
    data_dir = Path(settings.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    try:
        data_dir.chmod(0o700)
    except OSError:
        pass
    claude_run = data_dir / "claude-run"
    claude_run.mkdir(parents=True, exist_ok=True)
    try:
        claude_run.chmod(0o700)
    except OSError:
        pass


def check_env_permissions(env_path: Path) -> None:
    """Check .env file permissions and warn if too open."""
    try:
        stat = env_path.stat()
        if stat.st_mode & 0o077:
            logger.warning(
                "WARNING: %s is readable by group/others. Run: chmod 600 %s",
                env_path, env_path
            )
    except OSError:
        pass


async def run_bot(settings) -> None:
    """Run the bot with polling."""
    setup_data_dirs(settings)

    # Check profile.md
    profile_path = Path(settings.profile_path)
    if not profile_path.exists():
        logger.warning("profile.md not found at %s. Replies will be generic.", profile_path)

    # Build assistant
    assistant = build_assistant(settings)

    # Create bot
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    # Verify token
    try:
        me = await bot.get_me()
        logger.info("Bot @%s is running", me.username)
    except TelegramUnauthorizedError:
        logger.error("Invalid bot token. Check TELEGRAM_BOT_TOKEN.")
        await bot.session.close()
        sys.exit(2)

    # Log startup info
    mode = "PREVIEW (replies are only shown in the logs)" if not settings.auto_send else "AUTO-SEND"
    if not settings.auto_send:
        logger.warning("Running in PREVIEW mode. Set AUTO_SEND=true to send replies.")
    if settings.state.paused if hasattr(settings, 'state') else False:
        logger.warning("Bot is PAUSED. Use /resume to resume.")

    logger.info("Mode: %s", mode)
    logger.info("Claude auth: %s", settings.claude_auth_mode)
    logger.info("Claude binary: %s", settings.claude_bin)

    # Setup dispatcher
    dp = setup_dispatcher(assistant)

    # Start polling
    try:
        await dp.start_polling(bot, allowed_updates=ALLOWED_UPDATES)
    finally:
        await bot.session.close()


async def check_claude(settings) -> None:
    """Check Claude CLI configuration and exit with code."""
    setup_data_dirs(settings)

    try:
        cli = make_claude_cli(settings)
        result = await cli.inspect()
    except Exception as e:
        logger.error("Claude inspect failed: %s", e)
        sys.exit(1)

    # Check init event
    init_data = result.init
    if not init_data:
        logger.error("No init event from Claude CLI")
        sys.exit(1)

    # Check tools, mcp_servers, plugins, skills are empty
    for key in ("tools", "mcp_servers", "plugins", "skills"):
        value = init_data.get(key)
        if value:
            logger.error("%s is not empty: %s", key, value)
            sys.exit(1)

    # Check apiKeySource is not API key
    api_key_source = init_data.get("apiKeySource", "")
    if api_key_source in ("ANTHROPIC_API_KEY", "apiKeyHelper", "/login managed key"):
        logger.error("apiKeySource is %s (should be OAuth/subscription)", api_key_source)
        sys.exit(1)

    # Check apiProvider
    api_provider = init_data.get("apiProvider")
    if api_provider and api_provider != "firstParty":
        logger.error("apiProvider is %s (should be firstParty or None)", api_provider)
        sys.exit(1)

    # Success
    print(json.dumps({
        "auth": result.auth,
        "init": {
            "tools": init_data.get("tools"),
            "mcp_servers": init_data.get("mcp_servers"),
            "plugins": init_data.get("plugins"),
            "skills": init_data.get("skills"),
            "apiKeySource": init_data.get("apiKeySource"),
            "apiProvider": init_data.get("apiProvider"),
            "model": init_data.get("model"),
        },
        "reply": result.reply,
    }, indent=2))


async def try_reply(settings, text: str) -> None:
    """Test a single reply without Telegram."""
    setup_data_dirs(settings)

    assistant = build_assistant(settings)

    # Simulate a message
    from aiogram.types import User, Chat
    from unittest.mock import AsyncMock

    # Create fake message
    message = AsyncMock()
    message.business_connection_id = "test-conn"
    message.chat = Chat(id=12345, type="private")
    message.message_id = 1
    message.from_user = User(id=99999, is_bot=False, first_name="TestUser")
    message.sender_business_bot = None
    message.text = text
    message.caption = None
    message.photo = None
    message.video = None
    message.document = None
    message.voice = None
    message.video_note = None
    message.sticker = None
    message.audio = None
    message.animation = None
    message.location = None
    message.contact = None
    message.poll = None
    message.dice = None

    # Mock bot
    bot = AsyncMock()
    bot.get_business_connection = AsyncMock(return_value=AsyncMock(
        id="test-conn",
        user=User(id=settings.owner_id, is_bot=False, first_name="Owner"),
        is_enabled=True,
        rights=AsyncMock(can_reply=True),
        can_reply=True,
    ))

    # Process
    outcome = await assistant.on_business_message(bot, message)

    # Show result
    lang = assistant.history.last_language("test-conn", 12345)
    history = assistant.history.get("test-conn", 12345)
    reply = ""
    if history and history[-1].role == "owner":
        reply = history[-1].text

    print(f"Language: {lang}")
    print(f"Incoming: {text}")
    print(f"Outcome: {outcome.value}")
    print(f"Reply: {reply}")


def settings_from_process_env() -> tuple:
    """Load settings from environment variables and clear them."""
    source = {}
    for key in SETTING_KEYS:
        value = os.environ.get(key)
        if value is not None:
            source[key] = value

    # Clear them from os.environ
    for key in SETTING_KEYS:
        os.environ.pop(key, None)

    try:
        settings = load_settings(".env", require_telegram=False, source=source)
    except ConfigError as e:
        hint = f" ({e.hint})" if e.hint else ""
        print(f"Config error: {e}{hint}", file=sys.stderr)
        sys.exit(2)

    return settings, source


def main() -> None:
    parser = argparse.ArgumentParser(description="Telegram Business Assistant Bot")
    parser.add_argument(
        "--settings-from-env",
        action="store_true",
        help="Load settings from environment variables (container mode)",
    )
    parser.add_argument(
        "--check-claude",
        action="store_true",
        help="Check Claude CLI configuration and exit",
    )
    parser.add_argument(
        "--try",
        dest="try_text",
        metavar="TEXT",
        help="Test a single reply without Telegram",
    )
    args = parser.parse_args()

    # Load settings
    try:
        if args.settings_from_env:
            settings, _ = settings_from_process_env()
        else:
            env_path = Path(".env")
            if env_path.exists():
                check_env_permissions(env_path)
            settings = load_settings(".env", require_telegram=not (args.check_claude or args.try_text))
    except ConfigError as e:
        hint = f" ({e.hint})" if e.hint else ""
        print(f"Config error: {e}{hint}", file=sys.stderr)
        sys.exit(2)

    # Run requested mode
    if args.check_claude:
        import asyncio
        asyncio.run(check_claude(settings))
    elif args.try_text:
        import asyncio
        asyncio.run(try_reply(settings, args.try_text))
    else:
        import asyncio
        asyncio.run(run_bot(settings))


if __name__ == "__main__":
    main()