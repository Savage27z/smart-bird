"""Smart Bird — entry point.

Boots the Telegram bot, opens the shared Birdeye client, initialises the
SQLite database, and runs four concurrent loops plus a supplementary
sentiment analyzer:

    * Layer 1 — graduation predictor
    * Layer 2 — smart money tracker
    * Layer 3 — liquidity stress monitor
    * Layer 4 — social sentiment analyzer (augments entry/graduation alerts)
    * Alert dispatcher — combines the layers into entry alerts

SIGTERM / SIGINT gracefully cancels the loops, closes the HTTP session, stops
the Telegram bot and closes the DB.
"""
from __future__ import annotations

import asyncio
import logging
import signal

from birdeye.client import BirdeyeClient
from birdeye.liquidity import LiquidityMonitor
from birdeye.new_listings import GraduationPredictor
from birdeye.sentiment import SentimentAnalyzer
from birdeye.smart_money import SmartMoneyTracker
from bot.formatter import (
    format_entry_alert,
    format_exit_alert,
    format_graduation_alert,
    format_smart_money_alert,
)
from bot.telegram_bot import SmartBirdBot, _alert_keyboard
from config import (
    ALERT_DEDUP_WINDOW_SECONDS,
    ENABLE_EXIT_ALERTS,
    ENABLE_GRADUATION_ALERTS,
    ENABLE_SMART_MONEY_ALERTS,
    LAYER3_MAX_AGE_SECONDS,
    LIQUIDITY_POLL_SECONDS,
    POLL_INTERVAL_SECONDS,
    SECURITY_SCREEN_REQUIRED,
    SMART_MONEY_POLL_SECONDS,
    SMART_MONEY_WALLETS,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
)
from db.database import Database

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
)
log = logging.getLogger('smart-bird')


# --------------------------------------------------------------------------- #
# Monitoring loops
# --------------------------------------------------------------------------- #

async def layer1_loop(
    predictor: GraduationPredictor,
    db: Database,
    bot: SmartBirdBot,
    signal_queue: 'asyncio.Queue[tuple[str, dict]]',
    sentiment: SentimentAnalyzer,
) -> None:
    """Periodically pull new listings, score them, and queue passers.

    Also fires an independent Layer 1 'Graduation Watch' alert for each
    passer when ENABLE_GRADUATION_ALERTS is on. Deduped on (address,
    'graduation') via the existing 1-hour window.
    """
    while True:
        try:
            passed = await predictor.run_once()
            for token in passed:
                await signal_queue.put(('layer1', token))
                if not ENABLE_GRADUATION_ALERTS:
                    continue
                address = token.get('address')
                if not address:
                    continue
                if await db.was_alerted_recently(
                    address, 'graduation', ALERT_DEDUP_WINDOW_SECONDS,
                ):
                    continue
                sentiment_score, sentiment_breakdown = await sentiment.analyze(
                    address,
                    first_seen=token.get('breakdown', {}).get('first_seen'),
                )
                msg = format_graduation_alert(
                    token, token.get('score', 0),
                    token.get('breakdown', {}),
                    sentiment_score=sentiment_score,
                    sentiment_breakdown=sentiment_breakdown,
                )
                await db.record_alert_attempt(address, 'graduation')
                if await bot.send_alert(msg, reply_markup=_alert_keyboard(address)):
                    await db.record_alert_sent(address, 'graduation')
                    await db.record_alert_performance(
                        address,
                        token.get('symbol', ''),
                        'graduation',
                        float(token.get('breakdown', {}).get('price', 0)),
                        float(token.get('breakdown', {}).get('market_cap', 0)),
                    )
                else:
                    log.warning(
                        'Graduation alert send failed for %s; will retry on next pass',
                        address,
                    )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception('layer1_loop error: %s', e)
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def layer2_loop(
    tracker: SmartMoneyTracker,
    db: Database,
    bot: SmartBirdBot,
    signal_queue: 'asyncio.Queue[tuple[str, dict]]',
) -> None:
    """For every Layer-1 token, check if smart money entered recently.

    Also re-enqueues tokens that already passed Layer 2 but have no
    successful entry alert yet — these can get stuck if Telegram delivery
    failed in the dispatcher. We throttle re-enqueues with a short cooldown
    so a persistently-failing send doesn't flood the queue.
    """
    REENQUEUE_COOLDOWN_SECONDS = 120
    while True:
        try:
            # Fresh layer1 candidates — promote them on smart-money hits.
            l1_tokens = await db.get_tracked_tokens(['layer1'])
            for t in l1_tokens:
                address = t.get('address')
                if not address:
                    continue
                hit = await tracker.check_token(address)
                if hit:
                    if await db.mark_layer2_confirmed(address, hit['wallet']):
                        await signal_queue.put(
                            ('layer2', {
                                'token': t,
                                'smart_money': hit,
                                'score': t.get('graduation_score'),
                            })
                        )
                        # Fire independent smart-money alert — separate dedup key.
                        if ENABLE_SMART_MONEY_ALERTS and not await db.was_alerted_recently(
                            address, 'smart_money', ALERT_DEDUP_WINDOW_SECONDS,
                        ):
                            sm_msg = format_smart_money_alert(t, hit)
                            await db.record_alert_attempt(address, 'smart_money')
                            if await bot.send_alert(sm_msg, reply_markup=_alert_keyboard(address)):
                                await db.record_alert_sent(address, 'smart_money')
                            else:
                                log.warning(
                                    'Smart-money alert send failed for %s',
                                    address,
                                )
                    else:
                        log.info(
                            'Layer 2: %s already past layer2 stage, skipping enqueue',
                            address,
                        )

            # Stuck layer2 tokens — re-enqueue if no recent successful alert.
            l2_tokens = await db.get_tracked_tokens(['layer2'])
            for t in l2_tokens:
                address = t.get('address')
                if not address:
                    continue
                if await db.was_attempted_recently(
                    address, 'entry', REENQUEUE_COOLDOWN_SECONDS,
                ):
                    continue
                last_entry = await db.get_latest_smart_money_entry(address)
                if not last_entry:
                    continue
                import time as _time
                minutes_ago = max(
                    0,
                    (int(_time.time()) - int(last_entry.get('entry_time') or 0)) // 60,
                )
                smart_money = {
                    'wallet': last_entry.get('wallet') or '',
                    'entry_time': int(last_entry.get('entry_time') or 0),
                    'amount_usd': last_entry.get('amount_usd'),
                    'minutes_ago': int(minutes_ago),
                }
                log.info(
                    'Layer 2: re-enqueueing stuck token %s for entry-alert retry',
                    address,
                )
                await signal_queue.put(
                    ('layer2', {
                        'token': t,
                        'smart_money': smart_money,
                        'score': t.get('graduation_score'),
                    })
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception('layer2_loop error: %s', e)
        await asyncio.sleep(SMART_MONEY_POLL_SECONDS)


async def layer3_loop(
    monitor: LiquidityMonitor,
    db: Database,
    bot: SmartBirdBot,
) -> None:
    """Snapshot liquidity and fire exit alerts when stress is detected."""
    while True:
        try:
            tokens = await db.get_tracked_tokens(
                ['layer1', 'layer2', 'alerted'],
            )
            for t in tokens:
                address = t.get('address')
                if not address:
                    continue
                first_seen = t.get('first_seen') or 0
                import time as _time
                if _time.time() - first_seen > LAYER3_MAX_AGE_SECONDS:
                    log.info(
                        'Layer 3: expiring stale token %s (age > %ds)',
                        address, LAYER3_MAX_AGE_SECONDS,
                    )
                    await db.mark_exited(address)
                    continue
                await monitor.snapshot(address)
                stress = await monitor.detect_stress(address)
                if not stress:
                    continue
                if await db.was_alerted_recently(
                    address, 'exit', ALERT_DEDUP_WINDOW_SECONDS,
                ):
                    continue
                msg = format_exit_alert(
                    t.get('symbol') or '???',
                    stress['drop_pct'],
                    stress['window_minutes'],
                    stress['lp_concentration'],
                    triggered_by=stress.get('triggered_by', 'both'),
                )
                if not ENABLE_EXIT_ALERTS:
                    # User has silenced exit alerts; still mark exited so we
                    # stop snapshotting, since liquidity has clearly collapsed.
                    await db.mark_exited(address)
                    continue
                await db.record_alert_attempt(address, 'exit')
                if await bot.send_alert(msg, reply_markup=_alert_keyboard(address)):
                    await db.record_alert_sent(address, 'exit')
                    await db.mark_exited(address)
                else:
                    log.warning(
                        'Layer 3 exit alert send failed for %s; will retry next loop',
                        address,
                    )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception('layer3_loop error: %s', e)
        await asyncio.sleep(LIQUIDITY_POLL_SECONDS)


async def alert_dispatcher(
    signal_queue: 'asyncio.Queue[tuple[str, dict]]',
    db: Database,
    monitor: LiquidityMonitor,
    bot: SmartBirdBot,
    predictor: GraduationPredictor,
    sentiment: SentimentAnalyzer,
) -> None:
    """Combine layer outputs into entry alerts, deduped on (address, 'entry')."""
    while True:
        try:
            kind, payload = await signal_queue.get()
            if kind != 'layer2':
                continue
            token = payload.get('token') or {}
            smart_money = payload.get('smart_money') or {}
            address = token.get('address')
            if not address:
                continue

            if await db.was_alerted_recently(
                address, 'entry', ALERT_DEDUP_WINDOW_SECONDS,
            ):
                continue

            # Re-check status — the token may have exited between Layer 2
            # confirmation and our turn at the queue.
            current = await db.get_token(address)
            if not current or current.get('status') in ('exited', 'alerted'):
                continue

            liq = await monitor.snapshot(address)
            if not liq:
                continue

            score, breakdown = await predictor.score_token(address)
            sentiment_score, sentiment_breakdown = await sentiment.analyze(
                address, first_seen=current.get('first_seen'),
            )
            token_for_msg = {
                'address': address,
                'symbol': token.get('symbol') or breakdown.get('symbol') or '???',
                'price': breakdown.get('price', 0.0),
                'market_cap': breakdown.get('market_cap', 0.0),
            }
            msg = format_entry_alert(
                token_for_msg, score, breakdown, smart_money,
                {'current_liquidity': liq['liquidity_usd']},
                sentiment_score=sentiment_score,
                sentiment_breakdown=sentiment_breakdown,
            )
            await db.record_alert_attempt(address, 'entry')
            if await bot.send_alert(msg, reply_markup=_alert_keyboard(address)):
                await db.record_alert_sent(address, 'entry')
                await db.mark_alerted(address)
                await db.record_alert_performance(
                    address,
                    token_for_msg.get('symbol', ''),
                    'entry',
                    float(breakdown.get('price', 0)),
                    float(breakdown.get('market_cap', 0)),
                )
            else:
                log.warning(
                    'Entry alert send failed for %s; leaving status at layer2 for retry',
                    address,
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception('alert_dispatcher error: %s', e)


async def cache_cleanup_loop(client: BirdeyeClient) -> None:
    """Periodically evict stale cache entries so memory doesn't grow unbounded."""
    while True:
        try:
            await client.clear_stale_cache(max_age=300)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception('cache_cleanup_loop error: %s', e)
        await asyncio.sleep(300)


async def performance_loop(
    client: BirdeyeClient,
    db: Database,
) -> None:
    """Periodically check prices for past alerts to track performance."""
    INTERVALS = [
        ('check_1h', 3600),
        ('check_6h', 21600),
        ('check_24h', 86400),
    ]
    while True:
        try:
            for interval_key, min_age in INTERVALS:
                pending = await db.get_pending_performance_checks(interval_key, min_age)
                for row in pending:
                    address = row.get('token_address')
                    if not address:
                        continue
                    overview = await client.get_token_overview(address)
                    if not overview:
                        continue
                    price = overview.get('price')
                    if price is None:
                        continue
                    try:
                        price_f = float(price)
                    except (TypeError, ValueError):
                        continue
                    await db.update_performance_check(row['id'], interval_key, price_f)
                    log.info(
                        'Performance check %s for %s: alert_price=%.8f current=%.8f',
                        interval_key, address, float(row.get('alert_price', 0)), price_f,
                    )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception('performance_loop error: %s', e)
        await asyncio.sleep(300)  # Check every 5 minutes


async def smoke_test(client: BirdeyeClient) -> None:
    """Confirm Birdeye connectivity at startup.

    Also counts toward the BIP Sprint 1 minimum-API-call total.
    """
    log.info('Running Birdeye smoke test...')
    trending = await client.get_trending(limit=5)
    if trending:
        log.info('Birdeye smoke test OK — received %d trending tokens', len(trending))
    else:
        log.warning('Birdeye smoke test failed — check API key and network')


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

async def main() -> None:
    """Wire everything together and run until a termination signal arrives."""
    db = Database()
    client = BirdeyeClient()
    bot: SmartBirdBot | None = None
    sentiment: SentimentAnalyzer | None = None
    tasks: list[asyncio.Task] = []
    try:
        from config import validate as validate_config
        validate_config()
        await db.init()
        predictor = GraduationPredictor(client, db)
        tracker = SmartMoneyTracker(client, db, SMART_MONEY_WALLETS)
        monitor = LiquidityMonitor(client, db)
        sentiment = SentimentAnalyzer(client)
        bot = SmartBirdBot(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, db, client)

        await bot.start()
        await smoke_test(client)
        log.info('Smart Bird bot started — monitoring Solana for graduation signals')

        if not SMART_MONEY_WALLETS:
            log.warning(
                'SMART_MONEY_WALLETS is empty — Layer 2 is a no-op and no entry '
                'alerts will fire. Set SMART_MONEY_WALLETS in your .env to enable.'
            )

        if not SECURITY_SCREEN_REQUIRED:
            log.warning(
                'SECURITY_SCREEN_REQUIRED=false — Layer 1 honeypot/mintable/rug '
                'filtering is DISABLED. Only run in this mode when your Birdeye '
                'tier lacks /defi/token_security access.'
            )

        if not (ENABLE_GRADUATION_ALERTS and ENABLE_SMART_MONEY_ALERTS and ENABLE_EXIT_ALERTS):
            log.info(
                'Alert channels: graduation=%s smart_money=%s exit=%s (combined entry always on)',
                ENABLE_GRADUATION_ALERTS, ENABLE_SMART_MONEY_ALERTS, ENABLE_EXIT_ALERTS,
            )

        signal_queue: asyncio.Queue = asyncio.Queue()
        stop_event = asyncio.Event()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except NotImplementedError:
                # Some platforms (Windows) can't attach signal handlers — fall back
                # to the default KeyboardInterrupt path for SIGINT.
                pass

        tasks = [
            asyncio.create_task(layer1_loop(predictor, db, bot, signal_queue, sentiment)),
            asyncio.create_task(layer2_loop(tracker, db, bot, signal_queue)),
            asyncio.create_task(layer3_loop(monitor, db, bot)),
            asyncio.create_task(
                alert_dispatcher(signal_queue, db, monitor, bot, predictor, sentiment)
            ),
            asyncio.create_task(cache_cleanup_loop(client)),
            asyncio.create_task(performance_loop(client, db)),
        ]
        await stop_event.wait()
        log.info('Shutdown signal received — cleaning up...')
    finally:
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if bot is not None:
            try:
                await bot.stop()
            except Exception:
                log.exception('Error during bot shutdown')
        try:
            await client.aclose()
        except Exception:
            log.exception('Error closing Birdeye client')
        if sentiment is not None:
            try:
                await sentiment.aclose()
            except Exception:
                log.exception('Error closing sentiment analyzer')
        try:
            await db.aclose()
        except Exception:
            log.exception('Error closing database')
        log.info('Smart Bird stopped cleanly')


if __name__ == '__main__':
    asyncio.run(main())
