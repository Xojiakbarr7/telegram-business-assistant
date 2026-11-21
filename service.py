"""Core business logic for the Telegram Business Assistant Bot."""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

from aiogram import Bot
from aiogram.types import BusinessConnection, Message

from .claude_cli import (
    ClaudeCLI,
    ClaudeError,
    ClaudeAuthError,
    ClaudeLimitError,
    ClaudeTimeout,
)
from .config import Settings
from .language import detect_language
from .prompts import SKIP_TOKEN, build_system_prompt, build_user_prompt
from .store import (
    HistoryItem,
    HistoryStore,
    PreviewStore,
    RuntimeState,
    SeenMessages,
    ensure_private_dir,
)


logger = logging.getLogger(__name__)


class Outcome(str, Enum):
    """Possible outcomes of message processing."""
    previewed = "previewed"
    sent = "sent"
    duplicate = "duplicate"
    ignored_bot = "ignored_bot"
    no_connection = "no_connection"
    foreign_connection = "foreign_connection"
    connection_disabled = "connection_disabled"
    unsupported = "unsupported"
    owner_message = "owner_message"
    owner_active = "owner_active"
    paused = "paused"
    limited = "limited"
    skipped = "skipped"
    permission_denied = "permission_denied"
    error = "error"


@dataclass
class Preview:
    """Preview data for logging."""
    connection_id: str
    chat_id: int
    chat_name: str
    language: str | None
    incoming: str
    reply: str
    note: str = ""

    def __str__(self) -> str:
        parts = [
            f"PREVIEW, not sent",
            f"connection={self.connection_id}",
            f"chat={self.chat_id} ({self.chat_name})",
            f"lang={self.language or 'unknown'}",
            f"incoming: {self.incoming[:100]}",
            f"reply: {self.reply[:100]}",
        ]
        if self.note:
            parts.append(f"note: {self.note}")
        return " | ".join(parts)


def print_preview(preview: Preview) -> None:
    """Default preview sink - logs preview."""
    logger.info("%s", preview)


def describe(message: Message) -> str | None:
    """Extract text description from message."""
    if message.text:
        return message.text
    if message.caption:
        if message.photo:
            return f"[photo] {message.caption}"
        if message.video:
            return f"[video] {message.caption}"
        if message.document:
            return f"[document] {message.caption}"
        return f"[media] {message.caption}"
    if message.voice:
        return "[voice message]"
    if message.video_note:
        return "[video note]"
    if message.sticker:
        emoji = message.sticker.emoji or "😀"
        return f"[sticker] {emoji}"
    if message.audio:
        return "[audio]"
    if message.animation:
        return f"[animation] {message.caption or ''}"
    if message.location:
        return "[location]"
    if message.contact:
        return "[contact]"
    if message.poll:
        return f"[poll] {message.poll.question}"
    if message.dice:
        return f"[dice] {message.dice.emoji}"
    # Service messages
    return None


def can_reply(connection: BusinessConnection) -> bool:
    """Check if bot can reply in this business connection."""
    # Check new rights field first
    if connection.rights and hasattr(connection.rights, "can_reply"):
        return bool(connection.rights.can_reply)
    # Fallback to old field
    return bool(getattr(connection, "can_reply", False))


def read_profile(path: Path) -> str:
    """Read profile file, stripping HTML comments."""
    import re
    try:
        content = path.read_text(encoding="utf-8")
        # Strip HTML comments
        content = re.sub(r"<!--.*?-->", "", content, flags=re.DOTALL)
        return content.strip()
    except OSError:
        return ""


class Assistant:
    """Main assistant logic."""

    def __init__(
        self,
        settings: Settings,
        generator: ClaudeCLI,
        *,
        clock: Callable[[], float] = time.time,
        preview_sink: Callable[[Preview], None] = print_preview,
    ) -> None:
        self.settings = settings
        self.generator = generator
        self.clock = clock
        self.preview_sink = preview_sink

        # Data directory
        data_dir = Path(settings.data_dir)
        ensure_private_dir(data_dir)
        ensure_private_dir(data_dir / "claude-run")

        # Stores
        self.history = HistoryStore(data_dir / "history.json", settings.history_limit)
        self.seen = SeenMessages(data_dir / "seen.json")
        self.state = RuntimeState.load(data_dir / "state.json")
        self.previews = PreviewStore(data_dir / "previews.jsonl")

        # Connection cache
        self._connection_cache: dict[str, BusinessConnection] = {}

        # Outcomes for testing
        self.outcomes: deque[Outcome] = deque(maxlen=100)

        # Takeover timers: chat_key -> expiry timestamp
        self._takeover_timers: dict[str, float] = {}

        # Per-chat locks
        self._chat_locks: dict[str, asyncio.Lock] = {}

    def _get_lock(self, connection_id: str, chat_id: int) -> asyncio.Lock:
        key = f"{connection_id}:{chat_id}"
        if key not in self._chat_locks:
            self._chat_locks[key] = asyncio.Lock()
        return self._chat_locks[key]

    def _chat_key(self, connection_id: str, chat_id: int) -> str:
        return f"{connection_id}:{chat_id}"

    def _record_outcome(self, outcome: Outcome) -> None:
        self.outcomes.append(outcome)

    async def on_business_message(self, bot: Bot, message: Message) -> Outcome:
        """Process incoming business message."""
        # 1. Check business_connection_id
        conn_id = getattr(message, "business_connection_id", None)
        if not conn_id:
            self._record_outcome(Outcome.no_connection)
            return Outcome.no_connection

        # 2. Deduplication key (before any await)
        msg_id = getattr(message, "message_id", 0)
        dedup_key = f"{conn_id}:{message.chat.id}:{msg_id}"
        if not self.seen.first_time(dedup_key):
            self._record_outcome(Outcome.duplicate)
            return Outcome.duplicate

        # 3. Ignore bot messages
        if getattr(message, "sender_business_bot", None) or getattr(message.from_user, "is_bot", False):
            self._record_outcome(Outcome.ignored_bot)
            return Outcome.ignored_bot

        # 4. Get connection (cached or fetch)
        connection = self._connection_cache.get(conn_id)
        if connection is None:
            try:
                connection = await bot.get_business_connection(conn_id)
                self._connection_cache[conn_id] = connection
            except Exception as e:
                logger.warning("Failed to get business connection %s: %s", conn_id, e)
                self._record_outcome(Outcome.no_connection)
                return Outcome.no_connection

        # 5. Check owner
        if message.from_user.id != self.settings.owner_id:
            self._record_outcome(Outcome.foreign_connection)
            return Outcome.foreign_connection

        # 6. Check connection enabled
        if not connection.is_enabled:
            self._record_outcome(Outcome.connection_disabled)
            return Outcome.connection_disabled

        # 7. Describe message
        incoming_text = describe(message)
        if incoming_text is None:
            self._record_outcome(Outcome.unsupported)
            return Outcome.unsupported

        # 8. Owner's own message - record to history, set takeover timer
        if message.from_user.id == self.settings.owner_id:
            self.history.add(
                conn_id,
                message.chat.id,
                HistoryItem(
                    role="owner",
                    name="You",
                    text=incoming_text,
                    lang=detect_language(incoming_text),
                    ts=self.clock(),
                ),
            )
            if self.settings.owner_takeover_minutes > 0:
                key = self._chat_key(conn_id, message.chat.id)
                self._takeover_timers[key] = self.clock() + self.settings.owner_takeover_minutes * 60
            self._record_outcome(Outcome.owner_message)
            return Outcome.owner_message

        # 9. Detect language
        language = detect_language(incoming_text)
        if language is None:
            language = self.history.last_language(conn_id, message.chat.id)

        # 10. Add to history as other
        self.history.add(
            conn_id,
            message.chat.id,
            HistoryItem(
                role="other",
                name=message.from_user.full_name or str(message.from_user.id),
                text=incoming_text,
                lang=language,
                ts=self.clock(),
            ),
        )

        # 11. Process with chat lock
        lock = self._get_lock(conn_id, message.chat.id)
        async with lock:
            # Check pause
            if self.state.paused:
                self._record_outcome(Outcome.paused)
                return Outcome.paused

            # Check limit
            if self.state.limit_until > self.clock():
                self._record_outcome(Outcome.limited)
                return Outcome.limited

            # Check takeover
            key = self._chat_key(conn_id, message.chat.id)
            takeover_until = self._takeover_timers.get(key, 0)
            if takeover_until > self.clock():
                self._record_outcome(Outcome.owner_active)
                return Outcome.owner_active

            # Generate reply
            try:
                reply = await self._generate_reply(conn_id, message.chat.id, language)
            except ClaudeLimitError as e:
                # Set limit_until
                if e.resets_at and e.resets_at > self.clock():
                    self.state.limit_until = e.resets_at
                else:
                    self.state.limit_until = self.clock() + self.settings.limit_cooldown_minutes * 60
                self.state.save(Path(self.settings.data_dir) / "state.json")
                logger.warning("Claude limit reached: %s", e.detail)
                self._record_outcome(Outcome.limited)
                # Notify owner only in auto-send mode
                if self.settings.auto_send:
                    try:
                        await bot.send_message(
                            self.settings.owner_id,
                            f"⚠️ Claude limit reached. Resets {e.reset_text or 'soon'}. "
                            f"Auto-replies paused until then.",
                        )
                    except Exception:
                        pass
                return Outcome.limited
            except (ClaudeAuthError, ClaudeTimeout, ClaudeError) as e:
                logger.error("Claude error: %s", e)
                self._record_outcome(Outcome.error)
                if self.settings.auto_send:
                    # Notify owner (rate limited - max once per 10 min)
                    await self._notify_owner_error(bot, str(e))
                return Outcome.error

        # Check for skip
        if not reply or reply == SKIP_TOKEN:
            self._record_outcome(Outcome.skipped)
            return Outcome.skipped

        # Preview mode
        if not self.settings.auto_send:
            preview = Preview(
                connection_id=conn_id,
                chat_id=message.chat.id,
                chat_name=message.chat.title or message.chat.full_name or str(message.chat.id),
                language=language,
                incoming=incoming_text,
                reply=reply,
            )
            self.preview_sink(preview)
            self.previews.append({
                "connection_id": conn_id,
                "chat_id": message.chat.id,
                "chat_name": preview.chat_name,
                "language": language,
                "incoming": incoming_text,
                "reply": reply,
                "ts": self.clock(),
            })
            self._record_outcome(Outcome.previewed)
            return Outcome.previewed

        # Auto-send mode: re-check blocks
        if self.state.paused:
            preview = Preview(
                connection_id=conn_id,
                chat_id=message.chat.id,
                chat_name=message.chat.title or message.chat.full_name or str(message.chat.id),
                language=language,
                incoming=incoming_text,
                reply=reply,
                note="paused during generation",
            )
            self.preview_sink(preview)
            self._record_outcome(Outcome.paused)
            return Outcome.paused

        if self.state.limit_until > self.clock():
            preview = Preview(
                connection_id=conn_id,
                chat_id=message.chat.id,
                chat_name=message.chat.title or message.chat.full_name or str(message.chat.id),
                language=language,
                incoming=incoming_text,
                reply=reply,
                note="limited during generation",
            )
            self.preview_sink(preview)
            self._record_outcome(Outcome.limited)
            return Outcome.limited

        takeover_until = self._takeover_timers.get(key, 0)
        if takeover_until > self.clock():
            preview = Preview(
                connection_id=conn_id,
                chat_id=message.chat.id,
                chat_name=message.chat.title or message.chat.full_name or str(message.chat.id),
                language=language,
                incoming=incoming_text,
                reply=reply,
                note="owner_active during generation",
            )
            self.preview_sink(preview)
            self._record_outcome(Outcome.owner_active)
            return Outcome.owner_active

        # Re-verify connection from Telegram (not cache)
        try:
            fresh_connection = await bot.get_business_connection(conn_id)
        except Exception as e:
            logger.warning("Failed to re-verify connection %s: %s", conn_id, e)
            preview = Preview(
                connection_id=conn_id,
                chat_id=message.chat.id,
                chat_name=message.chat.title or message.chat.full_name or str(message.chat.id),
                language=language,
                incoming=incoming_text,
                reply=reply,
                note="connection lookup failed",
            )
            self.preview_sink(preview)
            self._record_outcome(Outcome.permission_denied)
            return Outcome.permission_denied

        if (fresh_connection.user.id != self.settings.owner_id
                or not fresh_connection.is_enabled
                or not can_reply(fresh_connection)):
            preview = Preview(
                connection_id=conn_id,
                chat_id=message.chat.id,
                chat_name=message.chat.title or message.chat.full_name or str(message.chat.id),
                language=language,
                incoming=incoming_text,
                reply=reply,
                note="permission denied on re-verify",
            )
            self.preview_sink(preview)
            self._record_outcome(Outcome.permission_denied)
            return Outcome.permission_denied

        # Send reply in chunks of 4000 chars
        try:
            for i in range(0, len(reply), 4000):
                chunk = reply[i:i + 4000]
                await bot.send_message(
                    message.chat.id,
                    chunk,
                    business_connection_id=conn_id,
                )
        except Exception as e:
            logger.error("Failed to send message: %s", e)
            self._record_outcome(Outcome.error)
            return Outcome.error

        # Record sent reply to history
        self.history.add(
            conn_id,
            message.chat.id,
            HistoryItem(
                role="owner",
                name="You",
                text=reply,
                lang=language,
                ts=self.clock(),
            ),
        )

        self._record_outcome(Outcome.sent)
        return Outcome.sent

    async def _generate_reply(
        self,
        connection_id: str,
        chat_id: int,
        language: str | None,
    ) -> str:
        """Generate reply using Claude CLI."""
        # Build conversation history
        history_items = self.history.get(connection_id, chat_id)
        items = [
            {
                "role": item.role,
                "name": item.name,
                "text": item.text,
                "lang": item.lang,
            }
            for item in history_items
        ]

        # Read profile
        profile_path = Path(self.settings.profile_path)
        profile = read_profile(profile_path)

        # Build prompts
        system_prompt = build_system_prompt(profile, self._now_datetime())
        user_prompt = build_user_prompt(items, language)

        # Generate
        return await self.generator.generate(system_prompt, user_prompt)

    def _now_datetime(self):
        from datetime import datetime
        return datetime.fromtimestamp(self.clock())

    async def _notify_owner_error(self, bot: Bot, error: str) -> None:
        """Notify owner of error (rate limited)."""
        # Simple rate limiting: track last notification time
        if not hasattr(self, "_last_error_notify"):
            self._last_error_notify = 0
        now = self.clock()
        if now - self._last_error_notify < 600:  # 10 minutes
            return
        self._last_error_notify = now
        try:
            await bot.send_message(self.settings.owner_id, f"⚠️ Bot error: {error[:400]}")
        except Exception:
            pass

    async def on_business_connection(self, connection: BusinessConnection) -> None:
        """Handle business connection update."""
        self._connection_cache[connection.id] = connection
        if connection.user.id != self.settings.owner_id:
            logger.warning("Foreign business connection: user=%s", connection.user.id)
        else:
            logger.info(
                "Business connection: user=%s enabled=%s can_reply=%s",
                connection.user.id,
                connection.is_enabled,
                can_reply(connection),
            )

    # Owner control methods
    def pause(self) -> None:
        """Pause auto-replies."""
        self.state.paused = True
        self.state.save(Path(self.settings.data_dir) / "state.json")

    def resume(self) -> None:
        """Resume auto-replies."""
        self.state.paused = False
        self.state.limit_until = 0
        # Clear takeover timers
        self._takeover_timers.clear()
        self.state.save(Path(self.settings.data_dir) / "state.json")

    def status_text(self) -> str:
        """Get status text in English."""
        mode = "PREVIEW (replies are only shown in the logs)" if not self.settings.auto_send else "AUTO-SEND"
        state_parts = []
        if self.state.paused:
            state_parts.append("PAUSED")
        if self.state.limit_until > self.clock():
            remaining = int(self.state.limit_until - self.clock())
            state_parts.append(f"LIMITED ({remaining}s remaining)")
        if not state_parts:
            state_parts.append("running")

        return (
            f"Mode: {mode}\n"
            f"Status: {', '.join(state_parts)}\n"
            f"Chats in history: {self.history.chat_count()}\n"
            f"Claude auth: {self.settings.claude_auth_mode}"
        )

    def start_text(self) -> str:
        """Get start text in Uzbek."""
        mode = "PREVIEW (javoblar faqat logda ko'rinadi)" if not self.settings.auto_send else "AUTO-SEND"
        state = "pauzada" if self.state.paused else "ishlayapti"

        return (
            f"Salom! Men sizning shaxsiy Telegram Business yordamchingizman. 🤖\n\n"
            f"Nima qilaman:\n"
            f"• Kimdir sizga Telegram Business chatlarida yozsa, men sizning profilingiz va uslubingiz asosida javob tayyorlayman.\n"
            f"• Javoblar `claude -p` (sizning obuna tokeningiz) orqali yaratiladi.\n\n"
            f"Hozirgi holat:\n"
            f"• Rejim: {mode}\n"
            f"• Holat: {state}\n\n"
            f"Qanday ulanadi:\n"
            f"1. Telegram → Settings → Telegram Business → Chatbots\n"
            f"2. Bot username'ni tanlang\n"
            f"3. Qaysi chatlarni ko'rishini belgilang\n"
            f"4. \"Reply on my behalf\" (javob berish) ruxsatini bering\n"
            f"⚠️ Telegram Premium talab qilinadi\n\n"
            f"Buyruqlar (faqat siz uchun):\n"
            f"/pause — javob berishni to'xtatish\n"
            f"/resume — qayta yoqish\n"
            f"/status — hozirgi holat\n\n"
            f"Bilib qo'ying:\n"
            f"• Siz o'zingiz yozganingizda {self.settings.owner_takeover_minutes} daqiqa jim turaman\n"
            f"• Pul, qarz, uchrashuv, rejalar haqida hech narsaga rozilik bermayman\n"
            f"• `profile.md` ni to'ldiring — shunda javoblar aniqroq bo'ladi\n"
            f"• Preview rejimida bo'lsangiz, `AUTO_SEND=true` qiling\n"
        )


def make_claude_cli(settings: Settings) -> ClaudeCLI:
    """Create ClaudeCLI instance from settings."""
    # Resolve claude binary
    claude_bin = settings.claude_bin
    if not Path(claude_bin).is_absolute():
        found = shutil.which(claude_bin)
        if found:
            claude_bin = found

    # Create work directory
    workdir = Path(settings.data_dir) / "claude-run"
    ensure_private_dir(workdir)

    return ClaudeCLI(
        claude_bin=claude_bin,
        workdir=workdir,
        model=settings.claude_model,
        effort=settings.claude_effort,
        timeout=settings.claude_timeout_seconds,
        oauth_token=settings.claude_oauth_token,
        auth_label=settings.claude_auth_mode,
    )


def build_assistant(settings: Settings, **kwargs: Any) -> Assistant:
    """Build Assistant with all dependencies."""
    cli = make_claude_cli(settings)
    return Assistant(settings, cli, **kwargs)