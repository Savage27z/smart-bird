"""Layer 1 — Graduation Predictor.

Consumes newly-listed tokens from Birdeye and scores them 0-100 based on four
signals, each worth 25 points:

    * volume velocity   — recent 5m volume vs trailing 25m average (OHLCV 1m)
    * holder base       — absolute holder count (overview.holder)
    * buy pressure      — buy / (buy + sell) share of last 50 trades
    * price trajectory  — sign of priceChange30m / priceChange1h

Before scoring, each candidate is screened through ``/defi/token_security`` to
drop honeypots, mintable tokens, and top-10-holder-concentrated rugs.

Birdeye endpoints used
----------------------
* ``GET /defi/v2/tokens/new_listing``  — candidate pool
* ``GET /defi/token_security``         — honeypot / mint / concentration filter
* ``GET /defi/token_overview``         — price, market cap, holders, deltas
* ``GET /defi/ohlcv``                  — 1m candles for volume velocity
* ``GET /defi/txs/token``              — recent trades for buy pressure
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from birdeye.client import BirdeyeClient
from config import (
    GRADUATION_SCORE_THRESHOLD,
    MIN_BUY_PRESSURE,
    MIN_HOLDER_COUNT,
)
from db.database import Database

log = logging.getLogger('smart-bird.layer1')


def _safe_float(val, default: float = 0.0) -> float:
    """Coerce ``val`` to float, returning ``default`` on any failure."""
    if val is None:
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _safe_int(val, default: int = 0) -> int:
    """Coerce ``val`` to int, returning ``default`` on any failure."""
    if val is None:
        return default
    try:
        return int(float(val))  # accept '123.0' style strings
    except (TypeError, ValueError):
        return default


class GraduationPredictor:
    """Scores freshly-listed tokens for Pump.fun graduation likelihood."""

    def __init__(self, client: BirdeyeClient, db: Database) -> None:
        self.client = client
        self.db = db

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    async def run_once(self) -> list[dict]:
        """Fetch new listings, score them, and return those that passed.

        Returned token dicts carry ``address``, ``symbol``, ``name``, ``price``,
        ``market_cap``, ``score`` and ``breakdown`` for downstream consumers.
        """
        passed: list[dict] = []
        # Birdeye endpoint: GET /defi/v2/tokens/new_listing
        listings = await self.client.get_new_listings(limit=20)
        log.info('Layer 1: fetched %d new listings', len(listings))

        for item in listings:
            address = item.get('address') or item.get('tokenAddress')
            if not address:
                continue
            symbol = item.get('symbol') or ''
            name = item.get('name') or ''

            await self.db.upsert_token(address, symbol, name, int(time.time()))

            # Security screen — rug / honeypot filter.
            if not await self._security_ok(address):
                log.info('Layer 1: %s failed security screen, skipping', address)
                continue

            score, breakdown = await self.score_token(address)
            await self.db.update_token_score(address, score)

            holders = breakdown.get('holders', 0)
            buy_pressure = breakdown.get('buy_pressure_ratio', 0.0)

            if (
                score >= GRADUATION_SCORE_THRESHOLD
                and holders >= MIN_HOLDER_COUNT
                and buy_pressure >= MIN_BUY_PRESSURE
            ):
                if not await self.db.mark_layer1_passed(address):
                    # Already past Layer 1 — don't double-promote.
                    continue
                passed.append({
                    'address': address,
                    'symbol': symbol or breakdown.get('symbol', ''),
                    'name': name or breakdown.get('name', ''),
                    'price': breakdown.get('price', 0.0),
                    'market_cap': breakdown.get('market_cap', 0.0),
                    'score': score,
                    'breakdown': breakdown,
                })
                log.info(
                    'Layer 1 PASS %s score=%d holders=%d buy_pressure=%.2f',
                    address, score, holders, buy_pressure,
                )

        return passed

    async def score_token(self, address: str) -> tuple[int, dict]:
        """Compute the 0-100 graduation score and return (score, breakdown)."""
        breakdown: dict = {
            'address': address,
            'volume_velocity_score': 0,
            'volume_ratio': 0.0,
            'holder_score': 0,
            'holders': 0,
            'buy_pressure_score': 0,
            'buy_pressure_ratio': 0.0,
            'trajectory_score': 0,
            'price': 0.0,
            'market_cap': 0.0,
        }

        # Token overview drives holders, price, market cap and trajectory.
        # Birdeye endpoint: GET /defi/token_overview
        overview = await self.client.get_token_overview(address) or {}
        holders = _safe_int(overview.get('holder'))
        price = _safe_float(overview.get('price'))
        market_cap = _safe_float(
            overview.get('mc')
            or overview.get('marketCap')
            or overview.get('realMc')
        )
        change_1h = _safe_float(
            overview.get('priceChange1h')
            or overview.get('priceChange1hPercent')
        )
        change_30m = _safe_float(
            overview.get('priceChange30m')
            or overview.get('priceChange30mPercent')
        )
        breakdown['holders'] = holders
        breakdown['price'] = price
        breakdown['market_cap'] = market_cap
        breakdown['symbol'] = overview.get('symbol', '')
        breakdown['name'] = overview.get('name', '')

        # 1) Volume velocity
        vv_score, vol_ratio = await self._score_volume_velocity(address)
        breakdown['volume_velocity_score'] = vv_score
        breakdown['volume_ratio'] = vol_ratio

        # 2) Holder base
        breakdown['holder_score'] = self._score_holders(holders)

        # 3) Buy pressure
        bp_score, bp_ratio = await self._score_buy_pressure(address)
        breakdown['buy_pressure_score'] = bp_score
        breakdown['buy_pressure_ratio'] = bp_ratio

        # 4) Market cap / price trajectory
        breakdown['trajectory_score'] = self._score_trajectory(change_30m, change_1h)

        total = (
            breakdown['volume_velocity_score']
            + breakdown['holder_score']
            + breakdown['buy_pressure_score']
            + breakdown['trajectory_score']
        )
        total = max(0, min(100, total))
        breakdown['total'] = total
        return total, breakdown

    # ------------------------------------------------------------------ #
    # Internal scoring helpers
    # ------------------------------------------------------------------ #
    async def _security_ok(self, address: str) -> bool:
        """Return True if the token passes the security screen."""
        # Birdeye endpoint: GET /defi/token_security
        sec = await self.client.get_token_security(address)
        if not sec:
            # Fail closed: a security check we can't run is no security at all.
            # Log so flaky Birdeye responses don't silently kill the funnel.
            log.warning(
                'Layer 1: token_security lookup failed for %s; dropping candidate',
                address,
            )
            return False
        if bool(sec.get('isHoneypot')):
            return False
        if bool(sec.get('isMintable')):
            return False
        top10 = sec.get('top10HolderPercent')
        if top10 is None:
            log.warning(
                'Layer 1: %s missing top10HolderPercent in security payload; dropping candidate',
                address,
            )
            return False
        try:
            top10_f = float(top10)
        except (TypeError, ValueError):
            log.warning(
                'Layer 1: %s has unparseable top10HolderPercent=%r; dropping candidate',
                address, top10,
            )
            return False
        # Birdeye sometimes returns 0–1, sometimes 0–100; normalise to 0–1.
        if top10_f > 1.0:
            top10_f = top10_f / 100.0
        if top10_f > 0.80:
            return False
        return True

    async def _score_volume_velocity(self, address: str) -> tuple[int, float]:
        """Compare last-5m volume vs the trailing 25m average."""
        # Birdeye endpoint: GET /defi/ohlcv
        candles = await self.client.get_ohlcv(address, type_='1m', minutes_back=30)
        # Need a meaningful trailing window before the ratio means anything.
        # 25 candles = the prior-25m baseline + the recent 5 will overlap once
        # we have 30, but we score conservatively below that.
        if not candles or len(candles) < 25:
            return 0, 0.0

        # Candles are typically returned oldest-first; normalise just in case.
        candles = sorted(candles, key=lambda c: c.get('unixTime') or c.get('time') or 0)

        def _vol(c: dict) -> float:
            for key in ('v', 'volume', 'volumeUsd', 'vUsd'):
                val = c.get(key)
                if val is not None:
                    try:
                        return float(val)
                    except (TypeError, ValueError):
                        continue
            return 0.0

        recent = candles[-5:]
        prior = candles[:-5]
        recent_5m_vol = sum(_vol(c) for c in recent)
        prior_vols = [_vol(c) for c in prior] or [0.0]
        prior_avg = sum(prior_vols) / len(prior_vols)

        denom = max(prior_avg * 5, 1.0)
        ratio = recent_5m_vol / denom

        # Anti-game cap: when the trailing baseline is essentially zero, the
        # ratio is meaningless — a $5 trade can produce ratio>>3. Require
        # absolute recent volume to clear a floor before the top tiers unlock.
        BASELINE_MIN_USD = 100.0
        weak_baseline = prior_avg * 5 < BASELINE_MIN_USD
        if weak_baseline and recent_5m_vol < BASELINE_MIN_USD:
            return 5, ratio

        if ratio >= 3:
            score = 25
        elif ratio >= 2:
            score = 20
        elif ratio >= 1.5:
            score = 15
        elif ratio >= 1:
            score = 10
        else:
            score = 5
        return score, ratio

    @staticmethod
    def _score_holders(holders: int) -> int:
        """Map absolute holder count to a 0-25 sub-score."""
        if holders > 500:
            return 25
        if holders > 250:
            return 20
        if holders > 100:
            return 15
        if holders > 50:
            return 10
        return 0

    async def _score_buy_pressure(self, address: str) -> tuple[int, float]:
        """Compute buy / (buy + sell) over the last 50 swap trades."""
        # Birdeye endpoint: GET /defi/txs/token
        trades = await self.client.get_token_trades(address, limit=50)
        if not trades:
            return 0, 0.0

        buys = 0
        sells = 0
        for t in trades:
            side = _extract_side(t)
            if side == 'buy':
                buys += 1
            elif side == 'sell':
                sells += 1

        total = buys + sells
        if total == 0:
            return 0, 0.0
        ratio = buys / total
        if ratio < 0.5:
            return 0, ratio
        return round(ratio * 25), ratio

    @staticmethod
    def _score_trajectory(change_30m: float, change_1h: float) -> int:
        """Reward tokens whose short-window price action is pointing up."""
        if change_30m > 0 and change_1h > 0:
            return 25
        if change_1h > 0:
            return 18
        if change_30m > 0:
            return 12
        # Tokens with zero price movement still get a small floor — absence of data
        # is not the same as bearish action.
        return 5


def _extract_side(trade: dict) -> Optional[str]:
    """Best-effort extraction of a trade's side across Birdeye shape variants."""
    # Common shapes: {'side': 'buy'}, {'txType': 'buy'}, or nested inside from/to.
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
    # Some payloads mark direction with a boolean.
    if isinstance(trade.get('isBuy'), bool):
        return 'buy' if trade['isBuy'] else 'sell'
    return None
