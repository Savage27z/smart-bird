"""Telegram bot wrapper for Smart Bird.

Uses the ``python-telegram-bot`` v21 async API. The bot runs side-by-side
with the three Birdeye monitoring loops; it handles command traffic and
outbound alert delivery.
"""
from __future__ import annotations

import logging
from typing import Optional

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

from config import ALERT_DEDUP_WINDOW_SECONDS
from bot.formatter import _md_escape
from db.database import Database

log = logging.getLogger('smart-bird.bot')


class SmartBirdBot:
    """Thin async wrapper around :class:`telegram.ext.Application`."""

    def __init__(self, token: str, chat_id: str, db: Database) -> None:
        self._token = token
        self._chat_id = chat_id
        self._db = db
        self._app: Optional[Application] = None

    def _is_authorised(self, update: Update) -> bool:
        """Return True if the update originated from the configured chat_id.

        Fails closed: if no chat_id is configured, all command handlers
        refuse input. Configure TELEGRAM_CHAT_ID in .env to enable commands.
        """
        if not self._chat_id:
            return False
        chat = update.effective_chat
        if chat is None:
            return False
        try:
            return str(chat.id) == str(self._chat_id)
        except Exception:
            return False

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        """Build the Application, register handlers, and start polling."""
        if not self._token:
            log.warning(
                'Telegram bot token is empty — bot will not deliver alerts'
            )
            return

        self._app = ApplicationBuilder().token(self._token).build()
        self._app.add_handler(CommandHandler('start', self._cmd_start))
        self._app.add_handler(CommandHandler('status', self._cmd_status))
        self._app.add_handler(CommandHandler('watchlist', self._cmd_watchlist))

        await self._app.initialize()
        await self._app.start()
        if self._app.updater is not None:
            await self._app.updater.start_polling(drop_pending_updates=True)
        log.info('Telegram bot polling started')

    async def stop(self) -> None:
        """Stop polling and shut the Application down cleanly."""
        if self._app is None:
            return
        try:
            if self._app.updater is not None and self._app.updater.running:
                await self._app.updater.stop()
            if self._app.running:
                await self._app.stop()
            await self._app.shutdown()
        finally:
            self._app = None

    # ------------------------------------------------------------------ #
    # Outbound
    # ------------------------------------------------------------------ #
    async def send_alert(self, message: str) -> bool:
        """Deliver a Markdown-formatted alert to the configured chat.

        Returns True if Telegram accepted the message, False on any failure
        (including 'bot not configured'). Callers should only mark the alert
        as recorded when this returns True so failed sends are retried.
        """
        if self._app is None or not self._chat_id:
            log.info('Alert skipped (bot not configured): %s', message.splitlines()[0])
            return False
        try:
            await self._app.bot.send_message(
                chat_id=self._chat_id,
                text=message,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=False,
            )
            return True
        except Exception:
            log.exception('Failed to send Telegram alert')
            return False

    # ------------------------------------------------------------------ #
    # Command handlers
    # ------------------------------------------------------------------ #
    async def _cmd_start(
        self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Respond to /start."""
        if update.effective_message is None:
            return
        if not self._is_authorised(update):
            log.info(
                '/start from unauthorized chat %s ignored',
                update.effective_chat.id if update.effective_chat else '?',
            )
            return
        await update.effective_message.reply_text(
            "Smart Bird monitoring active. You'll receive alerts when "
            "the three-layer signal aligns."
        )

    async def _cmd_status(
        self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Respond to /status with live pipeline counters."""
        if update.effective_message is None:
            return
        if not self._is_authorised(update):
            log.info(
                '/status from unauthorized chat %s ignored',
                update.effective_chat.id if update.effective_chat else '?',
            )
            return
        total = await self._db.count_total_tokens()
        layer1 = await self._db.count_by_status('layer1')
        layer2 = await self._db.count_by_status('layer2')
        alerted = await self._db.count_by_status('alerted')
        alerts_24h = await self._db.count_alerts_since(24 * 60 * 60)
        text = (
            '*Smart Bird status*\n'
            f'Tracked tokens: {total}\n'
            f'Layer 1 passed: {layer1}\n'
            f'Layer 2 confirmed: {layer2}\n'
            f'Alerted: {alerted}\n'
            f'Alerts (24h): {alerts_24h}\n'
            f'Dedup window: {ALERT_DEDUP_WINDOW_SECONDS // 60} min'
        )
        await update.effective_message.reply_text(
            text, parse_mode=ParseMode.MARKDOWN,
        )

    async def _cmd_watchlist(
        self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Respond to /watchlist with the current pipeline state."""
        if update.effective_message is None:
            return
        if not self._is_authorised(update):
            log.info(
                '/watchlist from unauthorized chat %s ignored',
                update.effective_chat.id if update.effective_chat else '?',
            )
            return
        tokens = await self._db.get_tracked_tokens(
            ['layer1', 'layer2', 'alerted'],
        )
        if not tokens:
            await update.effective_message.reply_text(
                'Watchlist is empty — waiting for the next graduation candidate.'
            )
            return
        lines = ['*Smart Bird watchlist*']
        for t in tokens[:25]:
            symbol = _md_escape(t.get('symbol') or '???')
            status = t.get('status') or '?'
            score = t.get('graduation_score')
            score_s = f'{score}/100' if score is not None else 'n/a'
            addr = t.get('address') or ''
            lines.append(f'• `{addr[:6]}…{addr[-4:]}` ${symbol} — {status} ({score_s})')
        if len(tokens) > 25:
            lines.append(f'…and {len(tokens) - 25} more')
        await update.effective_message.reply_text(
            '\n'.join(lines), parse_mode=ParseMode.MARKDOWN,
        )
