"""Test infrastructure for Telegram Business Assistant Bot."""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
from collections import OrderedDict
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Callable, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import BusinessConnection, Chat, Message, Update, User

from assistant.config import Settings, load_settings
from assistant.service import Assistant, Outcome, build_assistant, make_claude_cli
from assistant.store import HistoryItem, ensure_private_dir


# ===== Test Clock =====

class Clock:
    """Controllable clock for testing."""

    def __init__(self, initial: float = 1000000.0) -> None:
        self._time = initial

    def __call__(self) -> float:
        return self._time

    def advance(self, seconds: float) -> None:
        self._time += seconds

    def set(self, value: float) -> None:
        self._time = value


# ===== Fake Generator =====

class FakeGenerator:
    """Fake Claude generator for testing."""

    def __init__(
        self,
        responses: list[str | Exception],
        *,
        on_generate: Callable[[], None] | None = None,
    ) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []
        self.on_generate = on_generate

    async def generate(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        if self.on_generate:
            self.on_generate()
        if not self.responses:
            raise RuntimeError("No more fake responses")
        resp = self.responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp

    async def inspect(self):
        from assistant.claude_cli import InspectResult
        return InspectResult(
            auth="subscription token from .env",
            init={
                "tools": [],
                "mcp_servers": [],
                "plugins": [],
                "skills": [],
                "apiKeySource": "oauth",
                "apiProvider": "firstParty",
                "model": "claude-3-5-sonnet-20241022",
            },
            reply="OK",
        )

    def __repr__(self) -> str:
        return f"FakeGenerator(remaining={len(self.responses)})"


# ===== Recording Session =====

class RecordingBot:
    """Bot that records API calls instead of making network requests."""

    def __init__(self, connection: BusinessConnection | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._connection = connection or BusinessConnection(
            id="test-conn",
            user=User(id=12345, is_bot=False, first_name="Owner"),
            is_enabled=True,
            rights=MagicMock(can_reply=True),
            can_reply=True,
        )
        self._sent_messages: list[dict] = []

    async def get_me(self) -> User:
        return User(id=999999, is_bot=True, first_name="TestBot", username="testbot")

    async def get_business_connection(self, connection_id: str) -> BusinessConnection:
        self.calls.append(("get_business_connection", {"connection_id": connection_id}))
        if connection_id != self._connection.id:
            from aiogram.exceptions import TelegramBadRequest
            raise TelegramBadRequest("Connection not found", method="getBusinessConnection")
        return self._connection

    async def send_message(
        self,
        chat_id: int,
        text: str,
        business_connection_id: str | None = None,
        **kwargs: Any,
    ) -> Message:
        self.calls.append(("send_message", {
            "chat_id": chat_id,
            "text": text,
            "business_connection_id": business_connection_id,
        }))
        msg = Message(
            message_id=len(self._sent_messages) + 1,
            date=int(time.time()),
            chat=Chat(id=chat_id, type="private"),
            from_user=User(id=999999, is_bot=True, first_name="TestBot"),
            text=text,
        )
        self._sent_messages.append({"chat_id": chat_id, "text": text, "conn": business_connection_id})
        return msg

    async def session_close(self) -> None:
        pass


# ===== Harness =====

@dataclass
class Harness:
    """Test harness with Assistant and fake dependencies."""

    assistant: Assistant
    bot: RecordingBot
    dp: Dispatcher
    clock: Clock
    tmp_path: Path
    settings: Settings

    @classmethod
    def create(
        cls,
        tmp_path: Path,
        *,
        auto_send: bool = False,
        owner_id: int = 12345,
        generator: FakeGenerator | None = None,
        clock: Clock | None = None,
        profile_content: str = "",
    ) -> "Harness":
        # Write profile
        profile_path = tmp_path / "profile.md"
        profile_path.write_text(profile_content, encoding="utf-8")

        # Settings
        overrides = {
            "TELEGRAM_BOT_TOKEN": "123456:test-token",
            "OWNER_ID": str(owner_id),
            "AUTO_SEND": str(auto_send).lower(),
            "DATA_DIR": str(tmp_path / "data"),
            "PROFILE_PATH": str(profile_path),
            "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat-test-token",
            "OWNER_TAKEOVER_MINUTES": "15",
            "LIMIT_COOLDOWN_MINUTES": "30",
        }
        settings = load_settings(".env", require_telegram=False, overrides=overrides)

        # Clock
        if clock is None:
            clock = Clock()

        # Generator
        if generator is None:
            generator = FakeGenerator(["Test reply"])

        # Build assistant
        assistant = build_assistant(settings, clock=clock, preview_sink=lambda p: None)
        # Replace generator with fake
        assistant.generator = generator

        # Bot
        bot = RecordingBot()

        # Dispatcher
        from assistant.handlers import setup_dispatcher
        dp = setup_dispatcher(assistant)

        return cls(
            assistant=assistant,
            bot=bot,
            dp=dp,
            clock=clock,
            tmp_path=tmp_path,
            settings=settings,
        )

    def restart(self) -> "Harness":
        """Create new Assistant with same data directory."""
        return Harness.create(
            self.tmp_path,
            auto_send=self.settings.auto_send,
            owner_id=self.settings.owner_id,
            generator=FakeGenerator(self.assistant.generator.responses.copy())
            if isinstance(self.assistant.generator, FakeGenerator)
            else None,
            clock=self.clock,
            profile_content=Path(self.settings.profile_path).read_text(encoding="utf-8"),
        )

    async def feed_update(self, update: Update) -> None:
        """Feed update to dispatcher."""
        await self.dp.feed_update(self.bot, update)

    # ===== Helpers =====

    def connect(self, connection: BusinessConnection | None = None) -> None:
        """Simulate business connection event."""
        if connection is None:
            connection = BusinessConnection(
                id="test-conn",
                user=User(id=self.settings.owner_id, is_bot=False, first_name="Owner"),
                is_enabled=True,
                rights=MagicMock(can_reply=True),
                can_reply=True,
            )
        self.bot._connection = connection
        # Feed connection update
        update = Update(
            update_id=1,
            business_connection=connection,
        )
        asyncio.run(self.feed_update(update))

    def set_connection(self, **kwargs: Any) -> None:
        """Modify the current connection (for testing disabled/foreign connections)."""
        conn = self.bot._connection
        for key, value in kwargs.items():
            setattr(conn, key, value)

    def business_message(
        self,
        text: str,
        *,
        from_id: int = 67890,
        chat_id: int = 11111,
        message_id: int | None = None,
        connection_id: str = "test-conn",
        is_bot: bool = False,
        via_business_bot: bool = False,
    ) -> Message:
        """Create a business message update and feed it."""
        if message_id is None:
            message_id = len(self.bot.calls) + 100

        message = Message(
            message_id=message_id,
            date=int(time.time()),
            chat=Chat(id=chat_id, type="private"),
            from_user=User(id=from_id, is_bot=is_bot, first_name="TestUser"),
            text=text,
            business_connection_id=connection_id,
        )
        if via_business_bot:
            message.sender_business_bot = User(id=99999, is_bot=True, first_name="Bot")

        update = Update(
            update_id=message_id,
            business_message=message,
        )
        asyncio.run(self.feed_update(update))
        return message

    def direct_message(self, text: str, *, from_id: int = 12345) -> Message:
        """Create a direct message (command) to the bot."""
        message = Message(
            message_id=len(self.bot.calls) + 200,
            date=int(time.time()),
            chat=Chat(id=from_id, type="private"),
            from_user=User(id=from_id, is_bot=False, first_name="Owner"),
            text=text,
            entities=[{"type": "bot_command", "offset": 0, "length": len(text)}],
        )
        update = Update(
            update_id=message.message_id,
            message=message,
        )
        asyncio.run(self.feed_update(update))
        return message


@pytest.fixture
def make_harness(tmp_path: Path) -> Callable[..., Harness]:
    """Fixture to create a test harness."""
    def _make(**kwargs: Any) -> Harness:
        return Harness.create(tmp_path, **kwargs)
    return _make