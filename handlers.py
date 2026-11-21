"""Message handlers for the Telegram Business Assistant Bot."""

from __future__ import annotations

from aiogram import Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.types import BusinessConnection, Message

from .service import Assistant


ALLOWED_UPDATES = ["message", "business_connection", "business_message"]


def build_router(assistant: Assistant) -> Router:
    """Build the router with all handlers."""
    router = Router()

    @router.business_connection()
    async def on_business_connection(connection: BusinessConnection) -> None:
        """Handle business connection updates."""
        await assistant.on_business_connection(connection)

    @router.business_message()
    async def on_business_message(message: Message) -> None:
        """Handle incoming business messages."""
        await assistant.on_business_message(message.bot, message)

    # Owner-only commands (direct messages to bot)
    @router.message(Command("start"))
    async def cmd_start(message: Message) -> None:
        """Handle /start command."""
        if message.from_user.id != assistant.settings.owner_id:
            # Stranger's /start - brief explanation
            await message.reply(
                "Bu shaxsiy yordamchi bot, faqat egasi uchun ishlaydi. "
                "Bu yerda suhbatlashib bo'lmaydi. Iltimos, egasiga to'g'ridan-to'g'ri yozing."
            )
            return

        # Owner's /start - full instructions
        await message.reply(assistant.start_text())

    @router.message(Command("pause"))
    async def cmd_pause(message: Message) -> None:
        """Handle /pause command."""
        if message.from_user.id != assistant.settings.owner_id:
            return
        assistant.pause()
        await message.reply("⏸ Bot pauzada. Javoblar yuborilmaydi.")

    @router.message(Command("resume"))
    async def cmd_resume(message: Message) -> None:
        """Handle /resume command."""
        if message.from_user.id != assistant.settings.owner_id:
            return
        assistant.resume()
        await message.reply("▶️ Bot qayta ishga tushdi. Javoblar yuboriladi.")

    @router.message(Command("status"))
    async def cmd_status(message: Message) -> None:
        """Handle /status command."""
        if message.from_user.id != assistant.settings.owner_id:
            return
        await message.reply(assistant.status_text())

    # Ignore other direct messages from strangers
    @router.message(F.chat.type == "private")
    async def ignore_stranger_dm(message: Message) -> None:
        """Ignore direct messages from non-owner."""
        if message.from_user.id != assistant.settings.owner_id:
            return
        # Owner's other messages - could add more commands here
        pass

    return router


def setup_dispatcher(assistant: Assistant) -> Dispatcher:
    """Create and configure dispatcher."""
    dp = Dispatcher()
    dp.include_router(build_router(assistant))
    return dp