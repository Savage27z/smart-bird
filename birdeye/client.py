"""Async Birdeye API client with rate limiting, retries, and per-call logging.

Every Birdeye Data API call from Smart Bird flows through this module. We log
each call to ``api_calls.log`` to satisfy the BIP competition's API-usage proof.

Design notes
------------
* One shared :class:`aiohttp.ClientSession` per client instance; created lazily
  on first use, torn down explicitly via :meth:`aclose`.
* Exponential backoff with jitter on HTTP 429 and 5xx responses. Max 5 retries,
  base 1.5 seconds — see :data:`config.MAX_RETRIES` / :data:`config.BASE_BACKOFF_SECONDS`.
* Every call — successful or not — appends one line to
  ``api_calls.log`` for auditability::

      [2026-04-18T21:10:05.123456+00:00] [GET /defi/token_overview] [200] [So11...1112]

* All public methods return the unwrapped ``data`` object on success or
  ``None`` on failure. They never raise to the caller.
"""
from __future__ import annotations

import asyncio
import os
import random
import time
from datetime import datetime, timezone
from typing import Any, Optional

import aiohttp

from config import (
    API_CALLS_LOG,
    BASE_BACKOFF_SECONDS,
    BIRDEYE_API_KEY,
    BIRDEYE_BASE_URL,
    MAX_RETRIES,
)


class BirdeyeClient:
    """Thin async wrapper around the Birdeye Data API.

    All methods that hit the network are coroutines; each one documents the
    concrete Birdeye endpoint it calls with a ``# Birdeye endpoint:`` comment
    so reviewers can map code to docs at a glance.
    """

    def __init__(self) -> None:
        self._session: Optional[aiohttp.ClientSession] = None
        self._lock = asyncio.Lock()
        # Make sure the log directory exists so the very first append succeeds.
        log_dir = os.path.dirname(API_CALLS_LOG) or '.'
        try:
            os.makedirs(log_dir, exist_ok=True)
        except (PermissionError, OSError) as e:
            # Fall back to a process-local path so the bot doesn't die at import.
            import logging
            logging.getLogger('smart-bird.client').warning(
                'Cannot create audit log dir %r (%s); falling back to ./api_calls.log',
                log_dir, e,
            )
            # Re-bind the module-level constant so subsequent _log() calls work.
            import config
            config.API_CALLS_LOG = './api_calls.log'

    # ------------------------------------------------------------------ #
    # Session lifecycle
    # ------------------------------------------------------------------ #
    async def _session_get(self) -> aiohttp.ClientSession:
        """Return the live session, creating it on first use."""
        async with self._lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(
                    headers={
                        'X-API-KEY': BIRDEYE_API_KEY,
                        'x-chain': 'solana',
                        'accept': 'application/json',
                    },
                    timeout=aiohttp.ClientTimeout(total=30),
                )
            return self._session

    async def aclose(self) -> None:
        """Close the underlying aiohttp session if still open."""
        if self._session and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------ #
    # Logging
    # ------------------------------------------------------------------ #
    def _log(self, endpoint: str, status: int, token: str | None) -> None:
        """Append a single audit line to the api_calls.log file."""
        ts = datetime.now(timezone.utc).isoformat()
        line = f"[{ts}] [GET {endpoint}] [{status}] [{token or '-'}]\n"
        try:
            with open(API_CALLS_LOG, 'a', encoding='utf-8') as f:
                f.write(line)
        except Exception:
            # Logging failures must never take down the trading loop.
            pass

    # ------------------------------------------------------------------ #
    # Core request with retries
    # ------------------------------------------------------------------ #
    async def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        token_for_log: str | None = None,
    ) -> Optional[dict]:
        """Perform a GET with retry/backoff; return unwrapped ``data`` or None."""
        url = f"{BIRDEYE_BASE_URL}{path}"
        session = await self._session_get()
        last_status = 0
        for attempt in range(MAX_RETRIES):
            try:
                async with session.get(url, params=params) as resp:
                    last_status = resp.status
                    if resp.status == 200:
                        self._log(path, 200, token_for_log)
                        try:
                            payload = await resp.json()
                        except (aiohttp.ContentTypeError, ValueError):
                            return None
                        if not isinstance(payload, dict) or not payload.get('success', False):
                            return None
                        return payload.get('data')
                    if resp.status == 429 or 500 <= resp.status < 600:
                        if attempt < MAX_RETRIES - 1:
                            backoff = BASE_BACKOFF_SECONDS * (2 ** attempt) + random.random()
                            await asyncio.sleep(backoff)
                        continue
                    # Non-retryable non-200 — log and bail.
                    self._log(path, resp.status, token_for_log)
                    return None
            except (aiohttp.ClientError, asyncio.TimeoutError):
                last_status = 0
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(BASE_BACKOFF_SECONDS * (2 ** attempt) + random.random())
        # Exhausted retries.
        self._log(path, last_status, token_for_log)
        return None

    # ================================================================== #
    # Public endpoint methods
    # ================================================================== #

    # Birdeye endpoint: GET /defi/v2/tokens/new_listing
    async def get_new_listings(self, limit: int = 20) -> list[dict]:
        """Fetch the most recent token listings (Layer 1 candidate pool)."""
        params = {
            'time_to': int(time.time()),
            'limit': limit,
            'meme_platform_enabled': 'true',
        }
        data = await self._get('/defi/v2/tokens/new_listing', params=params)
        if not data:
            return []
        if isinstance(data, dict):
            return data.get('items', []) or []
        if isinstance(data, list):
            return data
        return []

    # Birdeye endpoint: GET /defi/token_security
    async def get_token_security(self, address: str) -> Optional[dict]:
        """Honeypot / mint authority / top-holder concentration snapshot."""
        return await self._get(
            '/defi/token_security',
            params={'address': address},
            token_for_log=address,
        )

    # Birdeye endpoint: GET /defi/token_overview
    async def get_token_overview(self, address: str) -> Optional[dict]:
        """Price, market cap, liquidity, holders and short-window price deltas."""
        return await self._get(
            '/defi/token_overview',
            params={'address': address},
            token_for_log=address,
        )

    # Birdeye endpoint: GET /defi/token_trending
    async def get_trending(self, limit: int = 20) -> list[dict]:
        """Top-of-funnel trending tokens — also used as a startup smoke test."""
        params = {
            'sort_by': 'rank',
            'sort_type': 'asc',
            'offset': 0,
            'limit': limit,
        }
        data = await self._get('/defi/token_trending', params=params)
        if not data:
            return []
        if isinstance(data, dict):
            return data.get('tokens', []) or data.get('items', []) or []
        if isinstance(data, list):
            return data
        return []

    # Birdeye endpoint: GET /defi/ohlcv
    async def get_ohlcv(
        self,
        address: str,
        type_: str = '1m',
        minutes_back: int = 30,
    ) -> list[dict]:
        """OHLCV candles used by the volume-velocity component of Layer 1."""
        now = int(time.time())
        params = {
            'address': address,
            'type': type_,
            'time_from': now - minutes_back * 60,
            'time_to': now,
        }
        data = await self._get(
            '/defi/ohlcv',
            params=params,
            token_for_log=address,
        )
        if not data:
            return []
        if isinstance(data, dict):
            return data.get('items', []) or []
        if isinstance(data, list):
            return data
        return []

    # Birdeye endpoint: GET /defi/txs/token
    async def get_token_trades(self, address: str, limit: int = 50) -> list[dict]:
        """Recent swap trades — drives buy/sell ratio and smart-money detection."""
        params = {
            'address': address,
            'offset': 0,
            'limit': limit,
            'tx_type': 'swap',
            'sort_type': 'desc',
        }
        data = await self._get(
            '/defi/txs/token',
            params=params,
            token_for_log=address,
        )
        if not data:
            return []
        if isinstance(data, dict):
            return data.get('items', []) or []
        if isinstance(data, list):
            return data
        return []

    # Birdeye endpoint: GET /v1/wallet/token_list
    async def get_wallet_portfolio(self, wallet: str) -> Optional[dict]:
        """Current token holdings for a wallet — used to confirm smart-money still holds."""
        return await self._get(
            '/v1/wallet/token_list',
            params={'wallet': wallet},
            token_for_log=wallet,
        )

    # Birdeye endpoint: GET /defi/v3/token/holder
    async def get_token_holders(self, address: str, limit: int = 10) -> list[dict]:
        """Top holders — used to compute LP/holder concentration for Layer 3."""
        params = {
            'address': address,
            'offset': 0,
            'limit': limit,
        }
        data = await self._get(
            '/defi/v3/token/holder',
            params=params,
            token_for_log=address,
        )
        if not data:
            return []
        if isinstance(data, dict):
            return data.get('items', []) or []
        if isinstance(data, list):
            return data
        return []
