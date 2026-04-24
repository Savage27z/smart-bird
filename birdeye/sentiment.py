"""Layer 4 — Social Sentiment Analyzer.

Computes a 0-100 buzz score from four signals:

    * trade_engagement (0-25)  — unique wallets in recent trades
    * volume_buzz (0-25)       — volume/mcap ratio (hype indicator)
    * social_presence (0-25)   — social links via DexScreener (free, no auth)
    * community_growth (0-25)  — holder count relative to token age

DexScreener endpoint used
-------------------------
* ``GET https://api.dexscreener.com/tokens/v1/solana/{address}`` — free, no auth

The sentiment score is additive information; it augments entry/graduation/deep-dive
messaging but never gates them. If DexScreener is unreachable we fail open and
return 0 for the social sub-score so the pipeline never stalls on supplementary
data.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import aiohttp

from birdeye.client import BirdeyeClient
from config import DEXSCREENER_BASE_URL

log = logging.getLogger('smart-bird.layer4')


class SentimentAnalyzer:
    """Computes a 0-100 social/engagement buzz score for a token."""

    def __init__(self, client: BirdeyeClient) -> None:
        self.client = client
        # Separate aiohttp session for DexScreener — the Birdeye session has
        # auth headers we must not leak to a third-party endpoint.
        self._dex_session_obj: Optional[aiohttp.ClientSession] = None
        self._dex_lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # Session lifecycle
    # ------------------------------------------------------------------ #
    async def _dex_session(self) -> aiohttp.ClientSession:
        """Return a live DexScreener session, creating it on first use."""
        async with self._dex_lock:
            if self._dex_session_obj is None or self._dex_session_obj.closed:
                self._dex_session_obj = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=10),
                )
            return self._dex_session_obj

    async def aclose(self) -> None:
        """Close the DexScreener session if still open."""
        if self._dex_session_obj and not self._dex_session_obj.closed:
            await self._dex_session_obj.close()

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    async def analyze(
        self, address: str, first_seen: int | None = None,
    ) -> tuple[int, dict]:
        """Return (score, breakdown) for the given token address.

        ``first_seen`` is the unix timestamp when Smart Bird first tracked the
        token; when absent we fall back to the overview's creation timestamp
        or a 30-minute default for the community-growth sub-score.
        """
        # Overview drives volume/mcap ratio and the holder count for growth.
        overview = await self.client.get_token_overview(address) or {}

        te, unique_count = await self._trade_engagement(address)
        vb, ratio = self._volume_buzz(overview)
        sp, links_found = await self._social_presence(address)
        cg, hpm = self._community_growth(overview, first_seen)

        total = min(100, te + vb + sp + cg)
        breakdown = {
            'trade_engagement': te,
            'volume_buzz': vb,
            'social_presence': sp,
            'community_growth': cg,
            'unique_wallets': unique_count,
            'vol_mcap_ratio': ratio,
            'social_links': links_found,
            'holders_per_min': hpm,
        }
        log.info(
            'Layer 4 sentiment token=%s total=%d te=%d vb=%d sp=%d cg=%d '
            'unique=%d vol_mc=%.2f hpm=%.2f links=%s',
            address, total, te, vb, sp, cg,
            unique_count, ratio, hpm, links_found,
        )
        return total, breakdown

    # ------------------------------------------------------------------ #
    # Sub-scores
    # ------------------------------------------------------------------ #
    async def _trade_engagement(self, address: str) -> tuple[int, int]:
        """Count unique wallets in the last 50 trades."""
        # Birdeye endpoint: GET /defi/txs/token (cached, shared with Layer 1/2)
        trades = await self.client.get_token_trades(address, limit=50)
        wallets: set[str] = set()
        for trade in trades or []:
            owner = _extract_owner(trade)
            if owner:
                wallets.add(owner.lower())

        unique = len(wallets)
        if unique > 40:
            score = 25
        elif unique > 25:
            score = 20
        elif unique > 15:
            score = 15
        elif unique > 8:
            score = 10
        else:
            score = 5
        return score, unique

    @staticmethod
    def _volume_buzz(overview: dict) -> tuple[int, float]:
        """Score the 24h volume / market cap ratio."""
        volume = _first_float(
            overview, ('v24hUSD', 'volume24h', 'v24h', 'volume24hUSD'),
        )
        mcap = _first_float(
            overview, ('mc', 'marketCap', 'realMc'),
        )
        if mcap <= 0 or volume <= 0:
            return 5, 0.0
        ratio = volume / mcap
        if ratio > 2.0:
            score = 25
        elif ratio > 1.0:
            score = 20
        elif ratio > 0.5:
            score = 15
        elif ratio > 0.1:
            score = 10
        else:
            score = 5
        return score, ratio

    async def _social_presence(self, address: str) -> tuple[int, list[str]]:
        """Score social links found via DexScreener. Fails open on any error."""
        url = f'{DEXSCREENER_BASE_URL}/tokens/v1/solana/{address}'
        try:
            session = await self._dex_session()
            async with session.get(url) as resp:
                if resp.status != 200:
                    log.debug(
                        'DexScreener returned status %d for %s',
                        resp.status, address,
                    )
                    return 0, []
                try:
                    payload = await resp.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError):
                    return 0, []
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.debug('DexScreener fetch failed for %s: %s', address, e)
            return 0, []
        except Exception as e:  # noqa: BLE001 - fail open on anything
            log.debug('DexScreener unexpected error for %s: %s', address, e)
            return 0, []

        if not isinstance(payload, list) or not payload:
            return 0, []

        first = payload[0] if isinstance(payload[0], dict) else {}
        info = first.get('info') if isinstance(first.get('info'), dict) else {}
        websites = info.get('websites') if isinstance(info.get('websites'), list) else []
        socials = info.get('socials') if isinstance(info.get('socials'), list) else []

        links_found: list[str] = []
        score = 0

        if any(isinstance(w, dict) and w.get('url') for w in websites):
            links_found.append('website')
            score += 8

        has_twitter = False
        has_telegram = False
        for s in socials:
            if not isinstance(s, dict):
                continue
            stype = (s.get('type') or '').lower()
            url_val = s.get('url')
            if not url_val:
                continue
            if not has_twitter and stype in ('twitter', 'x'):
                has_twitter = True
            elif not has_telegram and stype == 'telegram':
                has_telegram = True

        if has_twitter:
            links_found.append('twitter')
            score += 9
        if has_telegram:
            links_found.append('telegram')
            score += 8

        return min(25, score), links_found

    @staticmethod
    def _community_growth(
        overview: dict, first_seen: int | None,
    ) -> tuple[int, float]:
        """Score holders-per-minute relative to token age."""
        holders = 0
        raw = overview.get('holder') or overview.get('holders') or 0
        try:
            holders = int(float(raw))
        except (TypeError, ValueError):
            holders = 0

        now = int(time.time())
        if first_seen and int(first_seen) > 0:
            age_seconds = max(0, now - int(first_seen))
        else:
            # Fall back to an overview-provided creation time if available;
            # otherwise default to a 30-minute age so new tokens don't hit a
            # divide-by-zero and so the growth score stays meaningful.
            created = _first_int(
                overview,
                (
                    'createdTime', 'createdAt', 'createdUnixTime',
                    'createUnixTime', 'listingTime', 'firstTradeUnixTime',
                ),
            )
            if created and created > 0:
                age_seconds = max(0, now - created)
            else:
                age_seconds = 30 * 60

        age_minutes = age_seconds / 60.0
        if age_minutes < 5:
            # Too early to judge — return a neutral 10 and a conservative hpm.
            hpm = holders / max(age_minutes, 1.0)
            return 10, hpm

        hpm = holders / max(age_minutes, 1.0)
        if hpm > 2.0:
            score = 25
        elif hpm > 1.0:
            score = 20
        elif hpm > 0.5:
            score = 15
        elif hpm > 0.2:
            score = 10
        else:
            score = 5
        return score, hpm


# ---------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------- #

def _extract_owner(trade: dict) -> Optional[str]:
    """Find the wallet that initiated the trade across possible payload shapes.

    Mirrors the helper in :mod:`birdeye.smart_money` so the two layers see the
    same set of wallets when they scan the same trade payload.
    """
    owner = trade.get('owner') or trade.get('wallet') or trade.get('walletAddress')
    if isinstance(owner, str) and owner:
        return owner
    src = trade.get('from') if isinstance(trade.get('from'), dict) else None
    if src:
        cand = src.get('owner') or src.get('wallet')
        if isinstance(cand, str) and cand:
            return cand
    return None


def _first_float(data: dict, keys: tuple[str, ...]) -> float:
    """Return the first coercible-to-float value among ``keys`` (0.0 if none)."""
    for key in keys:
        val = data.get(key)
        if val is None:
            continue
        try:
            return float(val)
        except (TypeError, ValueError):
            continue
    return 0.0


def _first_int(data: dict, keys: tuple[str, ...]) -> int:
    """Return the first coercible-to-int value among ``keys`` (0 if none)."""
    for key in keys:
        val = data.get(key)
        if val is None:
            continue
        try:
            return int(float(val))
        except (TypeError, ValueError):
            continue
    return 0
