"""Layer 3 — Liquidity Stress Monitor.

Watches post-Layer-1 tokens for the classic rug pattern: a sharp liquidity
drop and/or a dangerously concentrated LP/holder set. Every poll appends a
snapshot to the DB, and stress detection compares the most recent snapshot
against the oldest still inside the rolling window.

Birdeye endpoints used
----------------------
* ``GET /defi/token_overview``   — current liquidity (USD)
* ``GET /defi/v3/token/holder``  — top-10 holder concentration proxy for LP
"""
from __future__ import annotations

import logging
from typing import Optional

from birdeye.client import BirdeyeClient
from config import (
    LIQUIDITY_DROP_THRESHOLD,
    LIQUIDITY_WINDOW_SECONDS,
    LP_CONCENTRATION_THRESHOLD,
)
from db.database import Database

log = logging.getLogger('smart-bird.layer3')


class LiquidityMonitor:
    """Captures liquidity snapshots and flags stress events."""

    def __init__(self, client: BirdeyeClient, db: Database) -> None:
        self.client = client
        self.db = db

    # ------------------------------------------------------------------ #
    # Snapshot
    # ------------------------------------------------------------------ #
    async def snapshot(self, address: str) -> Optional[dict]:
        """Record a liquidity / LP-concentration snapshot for ``address``.

        Returns a dict with ``liquidity_usd`` and ``lp_concentration`` on
        success, or ``None`` if we couldn't gather enough data.
        """
        # Birdeye endpoint: GET /defi/token_overview
        overview = await self.client.get_token_overview(address)
        if not overview:
            return None

        liquidity_usd = _extract_liquidity(overview)
        if liquidity_usd is None:
            return None

        lp_concentration = await self._lp_concentration(address)

        await self.db.record_liquidity_snapshot(
            address, liquidity_usd, lp_concentration,
        )
        return {
            'liquidity_usd': float(liquidity_usd),
            'lp_concentration': lp_concentration,
        }

    # ------------------------------------------------------------------ #
    # Stress detection
    # ------------------------------------------------------------------ #
    async def detect_stress(self, address: str) -> Optional[dict]:
        """Return a stress descriptor if a drop or concentration breach is detected."""
        snapshots = await self.db.get_liquidity_window(
            address, LIQUIDITY_WINDOW_SECONDS,
        )
        if not snapshots:
            return None

        # Concentration is a single-point measurement; it can fire on the
        # very first snapshot. Liquidity-drop needs at least two snapshots
        # so we can compute a window-relative delta.
        single_snapshot = len(snapshots) < 2

        oldest = snapshots[0]
        newest = snapshots[-1]
        oldest_liq = float(oldest.get('liquidity_usd') or 0.0)
        newest_liq = float(newest.get('liquidity_usd') or 0.0)
        current_lp = newest.get('lp_concentration')
        try:
            current_lp_f = float(current_lp) if current_lp is not None else 0.0
        except (TypeError, ValueError):
            current_lp_f = 0.0

        drop_pct = 0.0
        if not single_snapshot and oldest_liq > 0:
            drop_pct = max(0.0, (oldest_liq - newest_liq) / oldest_liq)

        concentration_breach = current_lp_f > LP_CONCENTRATION_THRESHOLD
        drop_breach = (not single_snapshot) and drop_pct > LIQUIDITY_DROP_THRESHOLD

        if not (concentration_breach or drop_breach):
            return None

        window_minutes = max(
            1,
            int((newest['timestamp'] - oldest['timestamp']) // 60),
        )
        if drop_breach and concentration_breach:
            triggered_by = 'both'
        elif drop_breach:
            triggered_by = 'liquidity_drop'
        else:
            triggered_by = 'lp_concentration'

        log.info(
            'Layer 3 STRESS token=%s trigger=%s drop_pct=%.2f lp_conc=%.2f window_min=%d',
            address, triggered_by, drop_pct, current_lp_f, window_minutes,
        )
        return {
            'triggered_by': triggered_by,
            'drop_pct': drop_pct,
            'window_minutes': window_minutes,
            'lp_concentration': current_lp_f,
            'current_liquidity': newest_liq,
        }

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    async def _lp_concentration(self, address: str) -> Optional[float]:
        """Approximate LP concentration as the top-10 holder share."""
        # Birdeye endpoint: GET /defi/v3/token/holder
        holders = await self.client.get_token_holders(address, limit=10)
        if not holders:
            return None

        percents: list[float] = []
        for h in holders:
            val = (
                h.get('percent')
                or h.get('percentage')
                or h.get('share')
                or h.get('percentOfSupply')
            )
            if val is None:
                continue
            try:
                fv = float(val)
            except (TypeError, ValueError):
                continue
            # Birdeye sometimes returns 0-100, sometimes 0-1 — normalise to 0-1.
            if fv > 1.0:
                fv = fv / 100.0
            percents.append(fv)

        if not percents:
            return None
        return max(0.0, min(1.0, sum(percents)))


def _extract_liquidity(overview: dict) -> Optional[float]:
    """Read the USD liquidity figure from the overview payload.

    Birdeye is inconsistent here: ``token_overview`` may return ``liquidity``
    as a flat USD float, or nest it as ``{'liquidity': {'usd': X}}``.
    """
    # Flat shapes first.
    for key in ('liquidity', 'liquidityUsd', 'liquidityUSD'):
        val = overview.get(key)
        if val is None:
            continue
        if isinstance(val, (int, float)):
            return float(val)
        if isinstance(val, dict):
            nested = None
            for nkey in ('usd', 'USD', 'value'):
                cand = val.get(nkey)
                if cand is not None:
                    nested = cand
                    break
            if nested is not None:
                try:
                    return float(nested)
                except (TypeError, ValueError):
                    continue
            continue
        try:
            return float(val)
        except (TypeError, ValueError):
            continue
    return None
