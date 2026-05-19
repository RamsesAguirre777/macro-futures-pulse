from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


async def _send_async(token: str, chat_id: str, text: str) -> None:
    from telegram import Bot

    async with Bot(token=token) as bot:
        await bot.send_message(chat_id=chat_id, text=text)


def send_message(text: str) -> None:
    from alerts.config import TELEGRAM_CHAT_ID, TELEGRAM_TOKEN

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.debug("Telegram not configured — message skipped: %s", text[:60])
        return
    try:
        asyncio.run(_send_async(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, text))
    except Exception as exc:
        logger.error("Telegram send_message failed: %s", exc)
