"""Tests for business message handling."""

from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiogram.types import BusinessConnection, Chat, Message, Sticker, Voice, User

from assistant.claude_cli import ClaudeLimitError, ClaudeAuthError, ClaudeTimeout, ClaudeError
from assistant.service import Outcome


class TestPreviewMode:
    """Tests for preview mode (AUTO_SEND=false)."""

    @pytest.mark.asyncio
    async def test_preview_mode_shows_reply_in_logs(self, make_harness):
        """Preview mode should log reply and not send."""
        h = make_harness(auto_send=False)
        h.connect()

        # Mock generator to return a reply
        h.assistant.generator.responses = ["Preview reply"]

        await h.business_message("Salom!", from_id=99999)

        # Check outcome
        assert h.assistant.outcomes[-1] == Outcome.previewed

        # Check preview was written to previews.jsonl
        previews_file = h.tmp_path / "data" / "previews.jsonl"
        assert previews_file.exists()
        lines = previews_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        preview = json.loads(lines[0])
        assert preview["reply"] == "Preview reply"
        assert preview["incoming"] == "Salom!"

        # Check file permissions (0600)
        mode = previews_file.stat().st_mode
        assert mode & stat.S_IRWXG == 0
        assert mode & stat.S_IRWXO == 0

        # Reply should NOT be in history
        history = h.assistant.history.get("test-conn", 11111)
        assert len(history) == 1  # Only incoming message
        assert history[0].role == "other"

    @pytest.mark.asyncio
    async def test_preview_mode_no_telegram_call_for_known_connection(self, make_harness):
        """Preview mode should not call Telegram for known connection."""
        h = make_harness(auto_send=False)
        h.connect()

        h.assistant.generator.responses = ["Reply"]
        await h.business_message("Hello", from_id=99999)

        # Only get_business_connection called once (for connection)
        calls = [c[0] for c in h.bot.calls]
        assert calls.count("get_business_connection") == 1
        assert "send_message" not in calls

    @pytest.mark.asyncio
    async def test_unknown_connection_lookup_in_preview(self, make_harness):
        """Preview mode should lookup unknown connection once."""
        h = make_harness(auto_send=False)
        # Don't pre-connect

        h.assistant.generator.responses = ["Reply"]
        await h.business_message("Hello", from_id=99999, connection_id="unknown-conn")

        # Should have looked up the connection
        calls = [c[0] for c in h.bot.calls]
        assert calls.count("get_business_connection") == 1
        assert "send_message" not in calls


class TestForeignAndDisabledConnections:
    """Tests for foreign accounts and disabled connections."""

    @pytest.mark.asyncio
    async def test_foreign_account_no_claude_call(self, make_harness):
        """Foreign account should not call Claude."""
        h = make_harness()
        # Connect as foreign user
        foreign_conn = BusinessConnection(
            id="foreign-conn",
            user=User(id=99999, is_bot=False, first_name="Foreign"),
            is_enabled=True,
            rights=MagicMock(can_reply=True),
            can_reply=True,
        )
        h.connect(foreign_conn)

        h.assistant.generator.responses = ["Should not be called"]
        await h.business_message("Hello", from_id=99999, connection_id="foreign-conn")

        assert h.assistant.outcomes[-1] == Outcome.foreign_connection
        assert len(h.assistant.generator.calls) == 0

    @pytest.mark.asyncio
    async def test_disabled_connection_no_claude_call(self, make_harness):
        """Disabled connection should not call Claude."""
        h = make_harness()
        h.connect()
        h.set_connection(is_enabled=False)

        h.assistant.generator.responses = ["Should not be called"]
        await h.business_message("Hello", from_id=12345)

        assert h.assistant.outcomes[-1] == Outcome.connection_disabled
        assert len(h.assistant.generator.calls) == 0

    @pytest.mark.asyncio
    async def test_any_chat_gets_reply_no_allowlist(self, make_harness):
        """Any chat should get reply - no allowlist in code."""
        h = make_harness()
        h.connect()

        h.assistant.generator.responses = ["Reply 1", "Reply 2"]
        await h.business_message("Chat 1", from_id=111, chat_id=111)
        await h.business_message("Chat 2", from_id=222, chat_id=222)

        assert h.assistant.outcomes[-2] == Outcome.previewed
        assert h.assistant.outcomes[-1] == Outcome.previewed


class TestOwnerMessages:
    """Tests for owner's own messages."""

    @pytest.mark.asyncio
    async def test_owner_message_recorded_no_reply(self, make_harness):
        """Owner's message should be recorded in history, no reply generated."""
        h = make_harness()
        h.connect()

        h.assistant.generator.responses = ["Should not be called"]
        await h.business_message("My message", from_id=12345)  # Owner ID

        assert h.assistant.outcomes[-1] == Outcome.owner_message
        assert len(h.assistant.generator.calls) == 0

        # Check history
        history = h.assistant.history.get("test-conn", 11111)
        assert len(history) == 1
        assert history[0].role == "owner"
        assert history[0].text == "My message"

    @pytest.mark.asyncio
    async def test_takeover_timer_silences_bot(self, make_harness):
        """Bot should stay silent during takeover period after owner message."""
        h = make_harness()
        h.connect()

        # Owner sends message
        await h.business_message("Owner here", from_id=12345)

        # Stranger replies during takeover
        h.assistant.generator.responses = ["Should not be called"]
        await h.business_message("Stranger reply", from_id=99999)

        assert h.assistant.outcomes[-1] == Outcome.owner_active

        # Advance clock past takeover
        h.clock.advance(16 * 60)  # 16 minutes

        h.assistant.generator.responses = ["Now I reply"]
        await h.business_message("Stranger again", from_id=99999)

        assert h.assistant.outcomes[-1] == Outcome.previewed


class TestBotMessagesIgnored:
    """Tests for bot messages being ignored."""

    @pytest.mark.asyncio
    async def test_sender_business_bot_ignored(self, make_harness):
        """Messages from sender_business_bot should be ignored."""
        h = make_harness()
        h.connect()

        h.assistant.generator.responses = ["Should not be called"]
        await h.business_message("From bot", from_id=99999, via_business_bot=True)

        assert h.assistant.outcomes[-1] == Outcome.ignored_bot

    @pytest.mark.asyncio
    async def test_is_bot_ignored(self, make_harness):
        """Messages from bot users should be ignored."""
        h = make_harness()
        h.connect()

        h.assistant.generator.responses = ["Should not be called"]
        await h.business_message("From bot user", from_id=99999, is_bot=True)

        assert h.assistant.outcomes[-1] == Outcome.ignored_bot


class TestDeduplication:
    """Tests for message deduplication."""

    @pytest.mark.asyncio
    async def test_duplicate_message_ignored(self, make_harness):
        """Same message ID should be processed only once."""
        h = make_harness()
        h.connect()

        h.assistant.generator.responses = ["Reply 1"]
        await h.business_message("Hello", from_id=99999, message_id=100)
        await h.business_message("Hello", from_id=99999, message_id=100)  # Same ID

        assert h.assistant.outcomes[-1] == Outcome.duplicate

    @pytest.mark.asyncio
    async def test_deduplication_persists_after_restart(self, make_harness):
        """Deduplication should persist after restart."""
        h = make_harness()
        h.connect()

        await h.business_message("Hello", from_id=99999, message_id=200)

        # Restart
        h2 = h.restart()
        h2.connect()

        await h2.business_message("Hello", from_id=99999, message_id=200)

        assert h2.assistant.outcomes[-1] == Outcome.duplicate


class TestUnsupportedMessages:
    """Tests for unsupported message types."""

    @pytest.mark.asyncio
    async def test_service_message_unsupported(self, make_harness):
        """Service messages (no text/caption) should be unsupported."""
        h = make_harness()
        h.connect()

        # Create a message with no describable content
        message = Message(
            message_id=300,
            date=int(h.clock()),
            chat=Chat(id=11111, type="private"),
            from_user=User(id=99999, is_bot=False, first_name="Test"),
            text=None,
            caption=None,
            photo=None,
            video=None,
            document=None,
            voice=None,
            video_note=None,
            sticker=None,
            audio=None,
            animation=None,
            location=None,
            contact=None,
            poll=None,
            dice=None,
            business_connection_id="test-conn",
        )

        bot = MagicMock()
        bot.get_business_connection = MagicMock(return_value=h.bot._connection)

        outcome = await h.assistant.on_business_message(bot, message)
        assert outcome == Outcome.unsupported

    @pytest.mark.asyncio
    async def test_sticker_described_correctly(self, make_harness):
        """Sticker should be described with emoji."""
        h = make_harness()
        h.connect()

        message = Message(
            message_id=400,
            date=int(h.clock()),
            chat=Chat(id=11111, type="private"),
            from_user=User(id=99999, is_bot=False, first_name="Test"),
            sticker=Sticker(
                file_id="sticker123",
                file_unique_id="unique123",
                type="regular",
                width=512,
                height=512,
                is_animated=False,
                is_video=False,
                emoji="😀",
            ),
            business_connection_id="test-conn",
        )

        bot = MagicMock()
        bot.get_business_connection = MagicMock(return_value=h.bot._connection)

        h.assistant.generator.responses = ["Reply to sticker"]
        outcome = await h.assistant.on_business_message(bot, message)

        assert outcome == Outcome.previewed
        # Check the prompt included sticker description
        assert len(h.assistant.generator.calls) == 1
        user_prompt = h.assistant.generator.calls[0][1]
        assert "[sticker] 😀" in user_prompt

    @pytest.mark.asyncio
    async def test_voice_message_described(self, make_harness):
        """Voice message should be described as [voice message]."""
        h = make_harness()
        h.connect()

        message = Message(
            message_id=500,
            date=int(h.clock()),
            chat=Chat(id=11111, type="private"),
            from_user=User(id=99999, is_bot=False, first_name="Test"),
            voice=Voice(
                file_id="voice123",
                file_unique_id="unique123",
                duration=10,
            ),
            business_connection_id="test-conn",
        )

        bot = MagicMock()
        bot.get_business_connection = MagicMock(return_value=h.bot._connection)

        h.assistant.generator.responses = ["Reply to voice"]
        outcome = await h.assistant.on_business_message(bot, message)

        assert outcome == Outcome.previewed
        user_prompt = h.assistant.generator.calls[0][1]
        assert "[voice message]" in user_prompt


class TestHistoryIsolation:
    """Tests for history isolation per connection and chat."""

    @pytest.mark.asyncio
    async def test_history_per_connection_and_chat(self, make_harness):
        """History should be separate per connection and chat."""
        h = make_harness()
        h.connect()

        h.assistant.generator.responses = ["Reply 1", "Reply 2", "Reply 3"]
        await h.business_message("Chat A msg 1", from_id=111, chat_id=100)
        await h.business_message("Chat B msg 1", from_id=222, chat_id=200)
        await h.business_message("Chat A msg 2", from_id=111, chat_id=100)

        # Check history for each chat
        hist_a = h.assistant.history.get("test-conn", 100)
        hist_b = h.assistant.history.get("test-conn", 200)

        assert len(hist_a) == 2  # 2 incoming
        assert len(hist_b) == 1

    @pytest.mark.asyncio
    async def test_prompt_no_cross_chat_contamination(self, make_harness):
        """Prompts should not contain messages from other chats."""
        h = make_harness()
        h.connect()

        h.assistant.generator.responses = ["Reply A", "Reply B"]
        await h.business_message("Secret in chat A", from_id=111, chat_id=100)
        await h.business_message("Chat B message", from_id=222, chat_id=200)

        # Check second prompt doesn't contain first chat's message
        prompt_b = h.assistant.generator.calls[1][1]
        assert "Secret in chat A" not in prompt_b
        assert "Chat B message" in prompt_b


class TestLanguageDetection:
    """Tests for language detection in context."""

    @pytest.mark.asyncio
    async def test_language_russian(self, make_harness):
        """Russian message should be detected as ru."""
        h = make_harness()
        h.connect()

        h.assistant.generator.responses = ["Reply"]
        await h.business_message("Привет, как дела?", from_id=99999)

        prompt = h.assistant.generator.calls[0][1]
        assert "Their latest message is in Russian." in prompt

    @pytest.mark.asyncio
    async def test_language_english(self, make_harness):
        """English message should be detected as en."""
        h = make_harness()
        h.connect()

        h.assistant.generator.responses = ["Reply"]
        await h.business_message("Hi, are you free tonight?", from_id=99999)

        prompt = h.assistant.generator.calls[0][1]
        assert "Their latest message is in English." in prompt

    @pytest.mark.asyncio
    async def test_language_uzbek(self, make_harness):
        """Uzbek message should be detected as uz."""
        h = make_harness()
        h.connect()

        h.assistant.generator.responses = ["Reply"]
        await h.business_message("Salom, qalesan?", from_id=99999)

        prompt = h.assistant.generator.calls[0][1]
        assert "Their latest message is in Uzbek." in prompt

    @pytest.mark.asyncio
    async def test_emoji_fallback_to_chat_language(self, make_harness):
        """Emoji-only message should use chat's last language."""
        h = make_harness()
        h.connect()

        # First message in Russian
        h.assistant.generator.responses = ["Reply 1"]
        await h.business_message("Привет", from_id=99999)

        # Second message is emoji
        h.assistant.generator.responses = ["Reply 2"]
        await h.business_message("👍", from_id=99999)

        prompt = h.assistant.generator.calls[1][1]
        assert "Their latest message is in Russian." in prompt


class TestSkipToken:
    """Tests for [SKIP] token handling."""

    @pytest.mark.asyncio
    async def test_skip_token_not_sent(self, make_harness):
        """[SKIP] reply should not be sent or logged as preview."""
        h = make_harness(auto_send=False)
        h.connect()

        h.assistant.generator.responses = ["[SKIP]"]
        await h.business_message("Just a sticker", from_id=99999)

        assert h.assistant.outcomes[-1] == Outcome.skipped

        # No preview written
        previews_file = h.tmp_path / "data" / "previews.jsonl"
        if previews_file.exists():
            lines = previews_file.read_text(encoding="utf-8").strip().splitlines()
            assert len(lines) == 0


class TestAutoSendMode:
    """Tests for auto-send mode (AUTO_SEND=true)."""

    @pytest.mark.asyncio
    async def test_auto_send_calls_get_connection_then_send(self, make_harness):
        """Auto-send should re-verify connection then send."""
        h = make_harness(auto_send=True)
        h.connect()

        h.assistant.generator.responses = ["Auto reply"]
        await h.business_message("Hello", from_id=99999)

        assert h.assistant.outcomes[-1] == Outcome.sent

        calls = [c[0] for c in h.bot.calls]
        # get_business_connection called twice (once initial, once re-verify)
        assert calls.count("get_business_connection") == 2
        assert calls.count("send_message") == 1

    @pytest.mark.asyncio
    async def test_auto_send_long_message_split(self, make_harness):
        """Long messages should be split into 4000-char chunks."""
        h = make_harness(auto_send=True)
        h.connect()

        long_reply = "x" * 5000
        h.assistant.generator.responses = [long_reply]
        await h.business_message("Hello", from_id=99999)

        send_calls = [c for c in h.bot.calls if c[0] == "send_message"]
        assert len(send_calls) == 2  # 4000 + 1000
        assert len(send_calls[0][1]["text"]) == 4000
        assert len(send_calls[1][1]["text"]) == 1000

    @pytest.mark.asyncio
    async def test_auto_send_can_reply_false_blocks(self, make_harness):
        """Auto-send should not send if can_reply becomes false."""
        h = make_harness(auto_send=True)
        h.connect()

        h.assistant.generator.responses = ["Reply"]
        await h.business_message("Hello", from_id=99999)

        # Now disable can_reply
        h.set_connection(rights=MagicMock(can_reply=False), can_reply=False)

        h.assistant.generator.responses = ["Reply 2"]
        await h.business_message("Hello again", from_id=99999)

        # Should be permission_denied
        assert h.assistant.outcomes[-1] == Outcome.permission_denied

    @pytest.mark.asyncio
    async def test_auto_send_connection_disabled_blocks(self, make_harness):
        """Auto-send should not send if connection disabled."""
        h = make_harness(auto_send=True)
        h.connect()

        h.assistant.generator.responses = ["Reply"]
        await h.business_message("Hello", from_id=99999)

        h.set_connection(is_enabled=False)

        h.assistant.generator.responses = ["Reply 2"]
        await h.business_message("Hello again", from_id=99999)

        assert h.assistant.outcomes[-1] == Outcome.permission_denied

    @pytest.mark.asyncio
    async def test_auto_send_owner_active_during_generation(self, make_harness):
        """Auto-send should abort if owner sends during generation."""
        h = make_harness(auto_send=True)
        h.connect()

        # Generator will be called, during which owner sends
        def on_generate():
            # Simulate owner sending during generation
            message = Message(
                message_id=999,
                date=int(h.clock()),
                chat=Chat(id=11111, type="private"),
                from_user=User(id=12345, is_bot=False, first_name='Owner'),
                text='Owner message',
                business_connection_id='test-conn',
            )
            asyncio.run(h.assistant.on_business_message(h.bot, message))

        h.assistant.generator = type('Gen', (), {
            'responses': ["Reply"],
            'calls': [],
            'on_generate': on_generate,
            'generate': lambda self, s, u: (self.calls.append((s, u)), self.responses.pop(0))[1],
        })()

        await h.business_message("Hello", from_id=99999)

        # Should be owner_active
        assert h.assistant.outcomes[-1] == Outcome.owner_active


class TestOwnerCommands:
    """Tests for owner-only commands."""

    @pytest.mark.asyncio
    async def test_pause_resume_only_owner(self, make_harness):
        """Pause/resume should only work for owner."""
        h = make_harness()
        h.connect()

        # Stranger tries /pause
        await h.direct_message("/pause", from_id=99999)
        assert not h.assistant.state.paused

        # Owner /pause
        await h.direct_message("/pause", from_id=12345)
        assert h.assistant.state.paused

        # Stranger message during pause
        h.assistant.generator.responses = ["Should not reply"]
        await h.business_message("Hello", from_id=99999)
        assert h.assistant.outcomes[-1] == Outcome.paused

        # Owner /resume
        await h.direct_message("/resume", from_id=12345)
        assert not h.assistant.state.paused

    @pytest.mark.asyncio
    async def test_pause_persists_after_restart(self, make_harness):
        """Pause state should persist after restart."""
        h = make_harness()
        h.connect()
        await h.direct_message("/pause", from_id=12345)
        assert h.assistant.state.paused

        h2 = h.restart()
        assert h2.assistant.state.paused

    @pytest.mark.asyncio
    async def test_stranger_commands_ignored(self, make_harness):
        """Stranger's commands and messages to bot should be ignored."""
        h = make_harness()
        # No connection needed for direct messages

        # Stranger sends /pause
        await h.direct_message("/pause", from_id=99999)
        # No reply sent

        # Stranger sends random message
        await h.direct_message("Hello bot", from_id=99999)
        # No reply sent

        # Should not crash and not call Claude
        assert len(h.assistant.generator.calls) == 0


class TestStartCommand:
    """Tests for /start command."""

    @pytest.mark.asyncio
    async def test_start_owner_shows_instructions(self, make_harness):
        """Owner's /start should show full instructions."""
        h = make_harness(auto_send=False)
        h.connect()

        await h.direct_message("/start", from_id=12345)

        send_calls = [c for c in h.bot.calls if c[0] == "send_message"]
        assert len(send_calls) == 1
        text = send_calls[0][1]["text"]
        assert "Salom!" in text
        assert "PREVIEW" in text
        assert "Chatbots" in text
        assert "/pause" in text
        assert "profile.md" in text

    @pytest.mark.asyncio
    async def test_start_stranger_brief_explanation(self, make_harness):
        """Stranger's /start should show brief explanation."""
        h = make_harness()
        # No connection needed

        await h.direct_message("/start", from_id=99999)

        send_calls = [c for c in h.bot.calls if c[0] == "send_message"]
        assert len(send_calls) == 1
        text = send_calls[0][1]["text"]
        assert "shaxsiy yordamchi bot" in text
        assert "faqat egasi" in text
        # Should not contain profile info or commands
        assert "PREVIEW" not in text
        assert "/pause" not in text

    @pytest.mark.asyncio
    async def test_start_owner_shows_correct_mode(self, make_harness):
        """Owner's /start should show current mode correctly."""
        h = make_harness(auto_send=True)
        h.connect()
        await h.direct_message("/start", from_id=12345)

        text = [c for c in h.bot.calls if c[0] == "send_message"][0][1]["text"]
        assert "AUTO-SEND" in text

        h2 = make_harness(auto_send=False)
        h2.connect()
        await h2.direct_message("/start", from_id=12345)

        text = [c for c in h2.bot.calls if c[0] == "send_message"][0][1]["text"]
        assert "PREVIEW" in text

    @pytest.mark.asyncio
    async def test_start_shows_paused_state(self, make_harness):
        """Owner's /start should show paused state."""
        h = make_harness()
        h.connect()
        h.assistant.pause()
        await h.direct_message("/start", from_id=12345)

        text = [c for c in h.bot.calls if c[0] == "send_message"][0][1]["text"]
        assert "pauzada" in text


class TestRateLimiting:
    """Tests for rate limiting and limit handling."""

    @pytest.mark.asyncio
    async def test_limit_resets_at_resets_at(self, make_harness):
        """Limit should reset at resets_at time."""
        h = make_harness()
        h.connect()

        # First message hits limit
        h.assistant.generator.responses = [
            ClaudeLimitError("Limit reached", resets_at=1000000 + 3600, reset_text="in 1 hour")
        ]
        await h.business_message("Hello", from_id=99999)
        assert h.assistant.outcomes[-1] == Outcome.limited
        assert h.assistant.state.limit_until == 1000000 + 3600

        # Advance time but not past reset
        h.clock.advance(1800)  # 30 min
        h.assistant.generator.responses = ["Should not reply"]
        await h.business_message("Hello", from_id=99999)
        assert h.assistant.outcomes[-1] == Outcome.limited

        # Advance past reset
        h.clock.advance(2000)  # Total 50 min > 1 hour
        h.assistant.generator.responses = ["Now works"]
        await h.business_message("Hello", from_id=99999)
        assert h.assistant.outcomes[-1] == Outcome.previewed

    @pytest.mark.asyncio
    async def test_limit_cooldown_when_no_resets_at(self, make_harness):
        """Limit should use cooldown when no resets_at."""
        h = make_harness()
        h.connect()

        h.assistant.generator.responses = [
            ClaudeLimitError("Limit reached", resets_at=None, reset_text="")
        ]
        await h.business_message("Hello", from_id=99999)
        assert h.assistant.outcomes[-1] == Outcome.limited

        # Should be limited for cooldown period
        expected_until = 1000000 + 30 * 60  # 30 min cooldown
        assert h.assistant.state.limit_until == expected_until

    @pytest.mark.asyncio
    async def test_limit_persists_after_restart(self, make_harness):
        """Limit state should persist after restart."""
        h = make_harness()
        h.connect()

        h.assistant.generator.responses = [
            ClaudeLimitError("Limit reached", resets_at=1000000 + 3600, reset_text="1 hour")
        ]
        await h.business_message("Hello", from_id=99999)

        h2 = h.restart()
        h2.connect()

        # Should still be limited
        h2.assistant.generator.responses = ["Should not reply"]
        await h2.business_message("Hello", from_id=99999)
        assert h2.assistant.outcomes[-1] == Outcome.limited

    @pytest.mark.asyncio
    async def test_auto_send_notifies_owner_on_limit(self, make_harness):
        """Auto-send should notify owner when limit reached."""
        h = make_harness(auto_send=True)
        h.connect()

        h.assistant.generator.responses = [
            ClaudeLimitError("Limit reached", resets_at=1000000 + 3600, reset_text="1 hour")
        ]
        await h.business_message("Hello", from_id=99999)

        # Owner should be notified
        send_calls = [c for c in h.bot.calls if c[0] == "send_message"]
        owner_notify = [c for c in send_calls if c[1]["chat_id"] == 12345]
        assert len(owner_notify) == 1
        assert "limit reached" in owner_notify[0][1]["text"].lower()

    @pytest.mark.asyncio
    async def test_resume_clears_limit(self, make_harness):
        """Resume should clear limit."""
        h = make_harness()
        h.connect()

        h.assistant.generator.responses = [
            ClaudeLimitError("Limit reached", resets_at=1000000 + 3600, reset_text="1 hour")
        ]
        await h.business_message("Hello", from_id=99999)
        assert h.assistant.state.limit_until > h.clock()

        await h.direct_message("/resume", from_id=12345)
        assert h.assistant.state.limit_until == 0


class TestErrorHandling:
    """Tests for error handling."""

    @pytest.mark.asyncio
    async def test_timeout_no_reply_to_chat(self, make_harness):
        """Timeout should not send reply to chat."""
        h = make_harness(auto_send=False)
        h.connect()

        h.assistant.generator.responses = [ClaudeTimeout("Timed out")]
        await h.business_message("Hello", from_id=99999)

        assert h.assistant.outcomes[-1] == Outcome.error

    @pytest.mark.asyncio
    async def test_auth_error_no_reply_to_chat(self, make_harness):
        """Auth error should not send reply to chat."""
        h = make_harness(auto_send=False)
        h.connect()

        h.assistant.generator.responses = [ClaudeAuthError("Auth failed", detail="invalid token")]
        await h.business_message("Hello", from_id=99999)

        assert h.assistant.outcomes[-1] == Outcome.error

    @pytest.mark.asyncio
    async def test_auto_send_notifies_owner_on_error(self, make_harness):
        """Auto-send should notify owner on error (rate limited)."""
        h = make_harness(auto_send=True)
        h.connect()

        h.assistant.generator.responses = [ClaudeError("Some error")]
        await h.business_message("Hello", from_id=99999)

        # Owner notified
        send_calls = [c for c in h.bot.calls if c[0] == "send_message"]
        owner_notify = [c for c in send_calls if c[1]["chat_id"] == 12345]
        assert len(owner_notify) == 1

        # Second error within 10 min should not notify
        h.assistant.generator.responses = [ClaudeError("Another error")]
        await h.business_message("Hello", from_id=99999)

        send_calls = [c for c in h.bot.calls if c[0] == "send_message"]
        owner_notify = [c for c in send_calls if c[1]["chat_id"] == 12345]
        assert len(owner_notify) == 1  # Still only 1

    @pytest.mark.asyncio
    async def test_connection_lookup_failure(self, make_harness):
        """Failed connection lookup should return no_connection."""
        h = make_harness()
        # Don't connect, and make lookup fail
        h.bot._connection = None

        from aiogram.exceptions import TelegramBadRequest
        from unittest.mock import AsyncMock
        h.bot.get_business_connection = AsyncMock(side_effect=TelegramBadRequest("Not found"))

        await h.business_message("Hello", from_id=99999, connection_id="bad-conn")

        assert h.assistant.outcomes[-1] == Outcome.no_connection