"""Layer 2 — Smart Money Tracker.

Given a list of known-alpha wallets (provided via env), this layer scans the
recent swap history of a specific token and fires a confirmation whenever one
of those wallets was on the buy side within the configured lookback window.

We then cross-check the wallet's current portfolio via
``/v1/wallet/token_list`` — if the wallet has already fully exited, we skip
the confirmation, because a buy-then-dump is anti-signal.

Birdeye endpoints used
----------------------
* ``GET /defi/txs/token``        — recent trades to scan for wallet matches
* ``GET /v1/wallet/token_list``  — confirm the wallet still holds the token
"""
from __future__ import annotations

import logging
import time
from typing import Iterable, Optional

from birdeye.client import BirdeyeClient
from config import SMART_MONEY_LOOKBACK_MINUTES
from db.database import Database

log = logging.getLogger('smart-bird.layer2')


class SmartMoneyTracker:
    """Detects recent smart-money entries on a given token."""

    def __init__(
        self,
        client: BirdeyeClient,
        db: Database,
        smart_money_wallets: Iterable[str],
    ) -> None:
        self.client = client
        self.db = db
        # Normalise to a lowercase-lookup set so we don't miss casing quirks.
        self._wallets_ci = {w.lower() for w in smart_money_wallets if w}
        # Keep the original casing too, for nicer display.
        self._wallets_original = {w.lower(): w for w in smart_money_wallets if w}

    async def check_token(self, address: str) -> Optional[dict]:
        """Return details of the first smart-money buy in the lookback window.

        The returned dict has keys ``wallet``, ``entry_time`` (unix seconds),
        ``amount_usd`` (best-effort), and ``minutes_ago``. Returns ``None`` if
        no smart-money buy is found.
        """
        if not self._wallets_ci:
            return None

        # Birdeye endpoint: GET /defi/txs/token
        trades = await self.client.get_token_trades(address, limit=50)
        if not trades:
            return None

        now = int(time.time())
        cutoff = now - SMART_MONEY_LOOKBACK_MINUTES * 60

        for trade in trades:
            ts = _extract_timestamp(trade)
            if ts is None or ts < cutoff:
                continue
            side = _extract_side(trade)
            if side != 'buy':
                continue
            wallet = _extract_owner(trade)
            if not wallet:
                continue
            if wallet.lower() not in self._wallets_ci:
                continue

            # Confirm the wallet still holds the token — skip if they already dumped.
            if not await self._wallet_still_holds(wallet, address):
                log.info(
                    'Layer 2: %s bought %s but portfolio shows zero balance — skipping',
                    wallet, address,
                )
                continue

            amount_usd = _extract_amount_usd(trade)
            await self.db.record_smart_money_entry(
                address, wallet, amount_usd, entry_time=ts,
            )

            minutes_ago = max(0, (now - ts) // 60)
            display_wallet = self._wallets_original.get(wallet.lower(), wallet)
            log.info(
                'Layer 2 HIT token=%s wallet=%s minutes_ago=%d amount_usd=%s',
                address, display_wallet, minutes_ago, amount_usd,
            )
            return {
                'wallet': display_wallet,
                'entry_time': ts,
                'amount_usd': amount_usd,
                'minutes_ago': int(minutes_ago),
            }

        return None

    # ------------------------------------------------------------------ #
    # Portfolio validation
    # ------------------------------------------------------------------ #
    async def _wallet_still_holds(self, wallet: str, token_address: str) -> bool:
        """Return True if the wallet's portfolio still shows a positive balance."""
        # Birdeye endpoint: GET /v1/wallet/token_list
        portfolio = await self.client.get_wallet_portfolio(wallet)
        if not portfolio:
            # Portfolio lookup failed — the on-chain trade is itself fresh evidence,
            # so we proceed with confirmation but flag the bypass for observability.
            log.warning(
                'Layer 2: portfolio lookup failed for %s; proceeding on trade evidence',
                wallet,
            )
            return True
        items = portfolio.get('items') if isinstance(portfolio, dict) else None
        if not isinstance(items, list):
            log.warning(
                'Layer 2: portfolio for %s lacks a list "items" field; '
                'proceeding on trade evidence',
                wallet,
            )
            return True
        for holding in items:
            if not isinstance(holding, dict):
                continue
            addr = holding.get('address') or holding.get('tokenAddress')
            if not addr or addr.lower() != token_address.lower():
                continue
            try:
                ui_amount = float(
                    holding.get('uiAmount')
                    or holding.get('balance')
                    or holding.get('amount')
                    or 0.0,
                )
            except (TypeError, ValueError):
                ui_amount = 0.0
            return ui_amount > 0
        # Token not found in portfolio — wallet no longer holds.
        return False


# ---------------------------------------------------------------------- #
# Helpers for parsing varying Birdeye trade payload shapes
# ---------------------------------------------------------------------- #

def _extract_timestamp(trade: dict) -> Optional[int]:
    for key in ('blockUnixTime', 'unixTime', 'time', 'timestamp'):
        val = trade.get(key)
        if val is None:
            continue
        try:
            return int(val)
        except (TypeError, ValueError):
            continue
    return None


def _extract_side(trade: dict) -> Optional[str]:
    for key in ('side', 'txType', 'type'):
        val = trade.get(key)
        if isinstance(val, str):
            low = val.lower()
            if low in ('buy', 'sell'):
                return low
            if low in ('swap_in', 'in'):
                return 'buy'
            if low in ('swap_out', 'out'):
                return 'sell'
    if isinstance(trade.get('isBuy'), bool):
        return 'buy' if trade['isBuy'] else 'sell'
    return None


def _extract_owner(trade: dict) -> Optional[str]:
    """Find the wallet that initiated the trade across possible payload shapes."""
    owner = trade.get('owner') or trade.get('wallet') or trade.get('walletAddress')
    if isinstance(owner, str) and owner:
        return owner
    src = trade.get('from') if isinstance(trade.get('from'), dict) else None
    if src:
        cand = src.get('owner') or src.get('wallet')
        if isinstance(cand, str) and cand:
            return cand
    return None


def _extract_amount_usd(trade: dict) -> Optional[float]:
    for key in ('volumeUsd', 'amountUsd', 'valueUsd', 'usdAmount', 'quoteAmountUsd'):
        val = trade.get(key)
        if val is None:
            continue
        try:
            return float(val)
        except (TypeError, ValueError):
            continue
    return None
