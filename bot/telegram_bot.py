"""Telegram bot wrapper for Smart Bird.

Uses the ``python-telegram-bot`` v21 async API. The bot runs side-by-side
with the three Birdeye monitoring loops; it handles command traffic and
outbound alert delivery.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

from config import ALERT_DEDUP_WINDOW_SECONDS
from db.database import Database

if TYPE_CHECKING:
    from birdeye.client import BirdeyeClient

log = logging.getLogger('smart-bird.bot')


class SmartBirdBot:
    """Thin async wrapper around :class:`telegram.ext.Application`."""

    def __init__(self, token: str, chat_id: str, db: Database, client: 'BirdeyeClient | None' = None) -> None:
        self._token = token
        self._chat_id = chat_id
        self._db = db
        self._client = client
        self._app: Optional[Application] = None

    # ------------------------------------------------------------------ #
    # Admin check (reserved for future commands; public ones don't use it)
    # ------------------------------------------------------------------ #
    def _is_admin(self, update: Update) -> bool:
        """True if sender matches TELEGRAM_CHAT_ID (when configured).

        Public commands (/start /stop /status /watchlist) do NOT gate on this
        — anyone can subscribe. Reserved for future admin-only endpoints.
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
        self._app.add_handler(CommandHandler('stop', self._cmd_stop))
        self._app.add_handler(CommandHandler('status', self._cmd_status))
        self._app.add_handler(CommandHandler('watchlist', self._cmd_watchlist))
        self._app.add_handler(CommandHandler('token', self._cmd_token))
        self._app.add_handler(CommandHandler('performance', self._cmd_performance))

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
        """Broadcast a Markdown alert to every subscriber.

        Returns True if at least one subscriber received the message. Per-
        recipient failures (blocked bot, deactivated account, etc.) are
        logged but don't fail the broadcast — we only need one successful
        send for the upstream retry logic to consider the alert delivered.

        Falls back to the configured TELEGRAM_CHAT_ID when no subscribers
        are registered yet, so first-boot single-user setups still work.
        """
        if self._app is None:
            log.info('Alert skipped (bot not configured): %s', message.splitlines()[0])
            return False

        recipients = await self._db.get_subscribers()
        if not recipients and self._chat_id:
            recipients = [str(self._chat_id)]
        if not recipients:
            log.info('Alert skipped (no subscribers): %s', message.splitlines()[0])
            return False

        any_success = False
        stale: list[str] = []
        for cid in recipients:
            try:
                await self._app.bot.send_message(
                    chat_id=cid,
                    text=message,
                    parse_mode=ParseMode.MARKDOWN,
                    disable_web_page_preview=False,
                )
                any_success = True
            except Exception as e:
                # Clean up chat IDs Telegram rejects permanently (blocked/deleted).
                msg = str(e).lower()
                if any(k in msg for k in ('forbidden', 'chat not found', 'user is deactivated', 'bot was blocked')):
                    stale.append(cid)
                    log.info('Removing stale subscriber %s (%s)', cid, e)
                else:
                    log.warning('Send failed to %s: %s', cid, e)
        for cid in stale:
            try:
                await self._db.remove_subscriber(cid)
            except Exception:
                pass
        return any_success

    # ------------------------------------------------------------------ #
    # Public command handlers
    # ------------------------------------------------------------------ #
    async def _cmd_start(
        self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """/start — subscribe this chat to Smart Bird alerts."""
        if update.effective_message is None or update.effective_chat is None:
            return
        chat_id = str(update.effective_chat.id)
        fresh = await self._db.add_subscriber(chat_id)
        header = (
            '🐦 *Welcome to Smart Bird!*' if fresh
            else '🐦 Already subscribed.'
        )
        await update.effective_message.reply_text(
            f"{header}\n\n"
            "You'll get:\n"
            "🎯 Graduation Watch — Layer 1 passers (early heads-up)\n"
            "🐋 Smart Money Move — tracked alpha wallet entries\n"
            "🚨 Smart Bird Alert — all three layers aligned (flagship)\n"
            "🔴 Exit Signal — liquidity stress on watched tokens\n\n"
            "Commands: /status /watchlist /token /performance /stop",
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
        )

    async def _cmd_stop(
        self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """/stop — unsubscribe this chat from Smart Bird alerts."""
        if update.effective_message is None or update.effective_chat is None:
            return
        chat_id = str(update.effective_chat.id)
        removed = await self._db.remove_subscriber(chat_id)
        if removed:
            await update.effective_message.reply_text(
                '👋 Unsubscribed. Send /start again to resume alerts.'
            )
        else:
            await update.effective_message.reply_text(
                'You weren\'t subscribed. Send /start to begin.'
            )

    async def _cmd_status(
        self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """/status — live pipeline counters."""
        if update.effective_message is None:
            return
        total = await self._db.count_total_tokens()
        layer1 = await self._db.count_by_status('layer1')
        layer2 = await self._db.count_by_status('layer2')
        alerted = await self._db.count_by_status('alerted')
        alerts_24h = await self._db.count_alerts_since(24 * 60 * 60)
        subs = await self._db.count_subscribers()
        text = (
            '*Smart Bird status*\n'
            f'Subscribers: {subs}\n'
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
        """/watchlist — tokens currently in the pipeline."""
        if update.effective_message is None:
            return
        tokens = await self._db.get_tracked_tokens(
            ['layer1', 'layer2', 'alerted'],
        )
        if not tokens:
            await update.effective_message.reply_text(
                'Watchlist is empty — waiting for the next graduation candidate.'
            )
            return
        from bot.formatter import _md_escape
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

    async def _cmd_performance(
        self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """/performance — show alert accuracy stats."""
        if update.effective_message is None:
            return
        stats = await self._db.get_performance_stats()
        from bot.formatter import format_performance
        text = format_performance(stats)
        await update.effective_message.reply_text(
            text, parse_mode=ParseMode.MARKDOWN,
        )

    async def _cmd_token(
        self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """/token <address> — deep dive into a single token."""
        if update.effective_message is None:
            return
        if self._client is None:
            await update.effective_message.reply_text(
                'Token lookup is not available right now.'
            )
            return

        text = (update.effective_message.text or '').strip()
        parts = text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            await update.effective_message.reply_text(
                'Usage: /token <address>\nExample: /token So11111111111111111111111111111111111111112'
            )
            return

        address = parts[1].strip()
        await update.effective_message.reply_text('🔍 Analyzing token...')

        try:
            overview = await self._client.get_token_overview(address)
            if not overview:
                await update.effective_message.reply_text(
                    f'Could not find token `{address}`. Check the address and try again.',
                    parse_mode=ParseMode.MARKDOWN,
                )
                return

            trades = await self._client.get_token_trades(address, limit=50)
            holders = await self._client.get_token_holders(address, limit=10)

            # Check if we have it in our pipeline
            tracked = await self._db.get_token(address)

            # Score it using the GraduationPredictor scoring logic
            from birdeye.new_listings import GraduationPredictor
            predictor = GraduationPredictor(self._client, self._db)
            score, breakdown = await predictor.score_token(address)

            from birdeye.sentiment import SentimentAnalyzer
            sentiment_analyzer = SentimentAnalyzer(self._client)
            try:
                sentiment_score, sentiment_breakdown = await sentiment_analyzer.analyze(
                    address,
                    first_seen=tracked.get('first_seen') if tracked else None,
                )
            finally:
                await sentiment_analyzer.aclose()

            from bot.formatter import format_token_deep_dive
            msg = format_token_deep_dive(
                address, overview, trades, holders, tracked, score, breakdown,
                sentiment_score=sentiment_score,
                sentiment_breakdown=sentiment_breakdown,
            )
            await update.effective_message.reply_text(
                msg, parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True,
            )
        except Exception as e:
            log.exception('Token lookup failed: %s', e)
            await update.effective_message.reply_text(
                'Something went wrong analyzing that token. Try again later.'
            )
