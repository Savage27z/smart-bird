"""SQLite persistence for Smart Bird.

The module exposes a single :class:`Database` class whose methods are all
``async``. Under the hood we hold one :class:`sqlite3.Connection` guarded by a
:class:`threading.Lock` and push every real DB call through
:func:`asyncio.to_thread` so the event loop never blocks on disk I/O.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
import time
from typing import Any, Iterable, Optional

from config import DB_PATH


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tracked_tokens (
    address TEXT PRIMARY KEY,
    symbol TEXT,
    name TEXT,
    first_seen INTEGER NOT NULL,
    graduation_score INTEGER,
    layer1_passed_at INTEGER,
    layer2_confirmed_at INTEGER,
    smart_money_wallet TEXT,
    status TEXT NOT NULL DEFAULT 'new'
);

CREATE TABLE IF NOT EXISTS liquidity_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_address TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    liquidity_usd REAL NOT NULL,
    lp_concentration REAL
);
CREATE INDEX IF NOT EXISTS idx_liq_token_time
    ON liquidity_snapshots(token_address, timestamp);

CREATE TABLE IF NOT EXISTS smart_money_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_address TEXT NOT NULL,
    wallet TEXT NOT NULL,
    entry_time INTEGER NOT NULL,
    amount_usd REAL
);

CREATE TABLE IF NOT EXISTS alerts_sent (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_address TEXT NOT NULL,
    alert_type TEXT NOT NULL,
    sent_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_dedup
    ON alerts_sent(token_address, alert_type, sent_at);

CREATE TABLE IF NOT EXISTS alert_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_address TEXT NOT NULL,
    alert_type TEXT NOT NULL,
    attempted_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attempts_dedup
    ON alert_attempts(token_address, alert_type, attempted_at);
"""


class Database:
    """Async-friendly wrapper around a single SQLite connection.

    Because every call funnels through :func:`asyncio.to_thread` and a single
    :class:`threading.Lock`, concurrent callers are safely serialised at the
    DB boundary without blocking the event loop.
    """

    def __init__(self, path: str = DB_PATH) -> None:
        self._path = path
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def init(self) -> None:
        """Create the DB file and schema if they don't already exist."""
        await asyncio.to_thread(self._init_sync)

    def _init_sync(self) -> None:
        db_dir = os.path.dirname(self._path) or '.'
        os.makedirs(db_dir, exist_ok=True)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA_SQL)
            self._conn.commit()

    async def aclose(self) -> None:
        """Close the underlying connection."""
        await asyncio.to_thread(self._close_sync)

    def _close_sync(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #
    def _execute(self, sql: str, params: Iterable[Any] = ()) -> None:
        with self._lock:
            assert self._conn is not None, 'Database.init() was not called'
            self._conn.execute(sql, tuple(params))
            self._conn.commit()

    def _execute_with_rowcount(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self._lock:
            assert self._conn is not None, 'Database.init() was not called'
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur.rowcount

    def _query_all(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        with self._lock:
            assert self._conn is not None, 'Database.init() was not called'
            cur = self._conn.execute(sql, tuple(params))
            rows = cur.fetchall()
            return [dict(row) for row in rows]

    def _query_one(self, sql: str, params: Iterable[Any] = ()) -> Optional[dict]:
        with self._lock:
            assert self._conn is not None, 'Database.init() was not called'
            cur = self._conn.execute(sql, tuple(params))
            row = cur.fetchone()
            return dict(row) if row else None

    # ------------------------------------------------------------------ #
    # Tracked tokens
    # ------------------------------------------------------------------ #
    async def upsert_token(
        self,
        address: str,
        symbol: str,
        name: str,
        first_seen: int,
    ) -> None:
        """Insert a freshly-seen token or no-op if it already exists."""
        def _run() -> None:
            self._execute(
                """
                INSERT INTO tracked_tokens (address, symbol, name, first_seen, status)
                VALUES (?, ?, ?, ?, 'new')
                ON CONFLICT(address) DO UPDATE SET
                    symbol = COALESCE(NULLIF(excluded.symbol, ''), tracked_tokens.symbol),
                    name = COALESCE(NULLIF(excluded.name, ''), tracked_tokens.name)
                """,
                (address, symbol, name, int(first_seen)),
            )
        await asyncio.to_thread(_run)

    async def update_token_score(self, address: str, score: int) -> None:
        """Store the latest graduation score for a token."""
        def _run() -> None:
            self._execute(
                """
                UPDATE tracked_tokens
                   SET graduation_score = ?, status = CASE
                       WHEN status = 'new' THEN 'scoring' ELSE status END
                 WHERE address = ?
                """,
                (int(score), address),
            )
        await asyncio.to_thread(_run)

    async def mark_layer1_passed(self, address: str) -> bool:
        """Promote a token to the 'layer1' pipeline stage.

        Returns True if the row was actually updated. Returns False if the
        token has already moved past Layer 1 (layer2 / alerted / exited),
        in which case the caller should NOT enqueue a duplicate downstream event.
        """
        def _run() -> int:
            return self._execute_with_rowcount(
                """
                UPDATE tracked_tokens
                   SET status = 'layer1', layer1_passed_at = ?
                 WHERE address = ?
                   AND status NOT IN ('layer2', 'alerted', 'exited')
                """,
                (int(time.time()), address),
            )
        rowcount = await asyncio.to_thread(_run)
        return rowcount > 0

    async def mark_layer2_confirmed(self, address: str, wallet: str) -> bool:
        """Promote a token to 'layer2' and record which smart wallet entered.

        Returns True if the transition succeeded; False if the token has
        already been alerted or exited.
        """
        def _run() -> int:
            return self._execute_with_rowcount(
                """
                UPDATE tracked_tokens
                   SET status = 'layer2',
                       layer2_confirmed_at = ?,
                       smart_money_wallet = ?
                 WHERE address = ?
                   AND status NOT IN ('alerted', 'exited')
                """,
                (int(time.time()), wallet, address),
            )
        rowcount = await asyncio.to_thread(_run)
        return rowcount > 0

    async def mark_alerted(self, address: str) -> None:
        """Final pipeline stage — an entry alert has been dispatched."""
        def _run() -> None:
            self._execute(
                """
                UPDATE tracked_tokens
                   SET status = 'alerted'
                 WHERE address = ?
                   AND status NOT IN ('exited')
                """,
                (address,),
            )
        await asyncio.to_thread(_run)

    async def mark_exited(self, address: str) -> None:
        """Final terminal stage — an exit alert has been dispatched.

        Stops Layer 3 from continuing to snapshot a token whose pool has
        already collapsed. Use after :meth:`record_alert_sent` for an exit alert.
        """
        def _run() -> None:
            self._execute(
                "UPDATE tracked_tokens SET status = 'exited' WHERE address = ?",
                (address,),
            )
        await asyncio.to_thread(_run)

    async def get_tracked_tokens(self, status_filter: list[str]) -> list[dict]:
        """Return all tokens whose status matches one of the given values."""
        def _run() -> list[dict]:
            if not status_filter:
                return self._query_all('SELECT * FROM tracked_tokens')
            placeholders = ','.join('?' for _ in status_filter)
            sql = (
                f'SELECT * FROM tracked_tokens '
                f'WHERE status IN ({placeholders}) '
                f'ORDER BY first_seen DESC'
            )
            return self._query_all(sql, status_filter)
        return await asyncio.to_thread(_run)

    async def get_token(self, address: str) -> Optional[dict]:
        """Fetch a single tracked-token row by address."""
        def _run() -> Optional[dict]:
            return self._query_one(
                'SELECT * FROM tracked_tokens WHERE address = ?',
                (address,),
            )
        return await asyncio.to_thread(_run)

    # ------------------------------------------------------------------ #
    # Liquidity snapshots
    # ------------------------------------------------------------------ #
    async def record_liquidity_snapshot(
        self,
        address: str,
        liquidity_usd: float,
        lp_concentration: float | None,
    ) -> None:
        """Append a liquidity/LP-concentration data point for rolling analysis."""
        def _run() -> None:
            self._execute(
                """
                INSERT INTO liquidity_snapshots
                    (token_address, timestamp, liquidity_usd, lp_concentration)
                VALUES (?, ?, ?, ?)
                """,
                (address, int(time.time()), float(liquidity_usd), lp_concentration),
            )
        await asyncio.to_thread(_run)

    async def get_liquidity_window(
        self,
        address: str,
        seconds_back: int,
    ) -> list[dict]:
        """Return snapshots for ``address`` within the last ``seconds_back`` seconds."""
        def _run() -> list[dict]:
            cutoff = int(time.time()) - int(seconds_back)
            return self._query_all(
                """
                SELECT timestamp, liquidity_usd, lp_concentration
                  FROM liquidity_snapshots
                 WHERE token_address = ? AND timestamp >= ?
                 ORDER BY timestamp ASC
                """,
                (address, cutoff),
            )
        return await asyncio.to_thread(_run)

    # ------------------------------------------------------------------ #
    # Smart money entries
    # ------------------------------------------------------------------ #
    async def record_smart_money_entry(
        self,
        address: str,
        wallet: str,
        amount_usd: float | None,
        entry_time: int | None = None,
    ) -> None:
        """Log that ``wallet`` bought ``address``.

        ``entry_time`` should be the on-chain block unix timestamp when known
        (preferred) so the layer2 retry path can compute an accurate
        minutes-ago for the alert body. Falls back to the current time if the
        caller couldn't extract the trade timestamp.
        """
        def _run() -> None:
            ts = int(entry_time) if entry_time else int(time.time())
            self._execute(
                """
                INSERT INTO smart_money_entries
                    (token_address, wallet, entry_time, amount_usd)
                VALUES (?, ?, ?, ?)
                """,
                (address, wallet, ts, amount_usd),
            )
        await asyncio.to_thread(_run)

    async def get_latest_smart_money_entry(self, address: str) -> Optional[dict]:
        """Return the most recent smart_money_entries row for the given token.

        Used by the entry-alert retry path to recreate the dispatcher payload
        when a token is stuck in 'layer2' status because Telegram delivery
        failed earlier.
        """
        def _run() -> Optional[dict]:
            return self._query_one(
                """
                SELECT wallet, entry_time, amount_usd
                  FROM smart_money_entries
                 WHERE token_address = ?
                 ORDER BY entry_time DESC
                 LIMIT 1
                """,
                (address,),
            )
        return await asyncio.to_thread(_run)

    # ------------------------------------------------------------------ #
    # Alerts
    # ------------------------------------------------------------------ #
    async def was_alerted_recently(
        self,
        address: str,
        alert_type: str,
        window_seconds: int,
    ) -> bool:
        """Return True if the same alert for this token fired within the window."""
        def _run() -> bool:
            cutoff = int(time.time()) - int(window_seconds)
            row = self._query_one(
                """
                SELECT id FROM alerts_sent
                 WHERE token_address = ? AND alert_type = ? AND sent_at >= ?
                 LIMIT 1
                """,
                (address, alert_type, cutoff),
            )
            return row is not None
        return await asyncio.to_thread(_run)

    async def record_alert_sent(self, address: str, alert_type: str) -> None:
        """Persist that an alert of ``alert_type`` just fired for ``address``."""
        def _run() -> None:
            self._execute(
                """
                INSERT INTO alerts_sent (token_address, alert_type, sent_at)
                VALUES (?, ?, ?)
                """,
                (address, alert_type, int(time.time())),
            )
        await asyncio.to_thread(_run)

    async def record_alert_attempt(self, address: str, alert_type: str) -> None:
        """Record that we tried to dispatch an alert (success or failure).

        Used by layer2_loop's re-enqueue throttle so a persistently-failing
        Telegram send doesn't spam the signal queue every poll.
        """
        def _run() -> None:
            self._execute(
                """
                INSERT INTO alert_attempts (token_address, alert_type, attempted_at)
                VALUES (?, ?, ?)
                """,
                (address, alert_type, int(time.time())),
            )
        await asyncio.to_thread(_run)

    async def was_attempted_recently(self, address: str, alert_type: str,
                                      window_seconds: int) -> bool:
        """True if any attempt (success or failure) was recorded inside the window."""
        def _run() -> bool:
            cutoff = int(time.time()) - int(window_seconds)
            row = self._query_one(
                """
                SELECT id FROM alert_attempts
                 WHERE token_address = ? AND alert_type = ? AND attempted_at >= ?
                 LIMIT 1
                """,
                (address, alert_type, cutoff),
            )
            return row is not None
        return await asyncio.to_thread(_run)

    async def count_alerts_since(self, seconds_back: int) -> int:
        """Return how many alerts have been recorded in the trailing window."""
        def _run() -> int:
            cutoff = int(time.time()) - int(seconds_back)
            row = self._query_one(
                'SELECT COUNT(*) AS c FROM alerts_sent WHERE sent_at >= ?',
                (cutoff,),
            )
            return int(row['c']) if row else 0
        return await asyncio.to_thread(_run)

    async def count_by_status(self, status: str) -> int:
        """Count tracked tokens currently in a given pipeline stage."""
        def _run() -> int:
            row = self._query_one(
                'SELECT COUNT(*) AS c FROM tracked_tokens WHERE status = ?',
                (status,),
            )
            return int(row['c']) if row else 0
        return await asyncio.to_thread(_run)

    async def count_total_tokens(self) -> int:
        """Total number of tokens ever tracked."""
        def _run() -> int:
            row = self._query_one('SELECT COUNT(*) AS c FROM tracked_tokens')
            return int(row['c']) if row else 0
        return await asyncio.to_thread(_run)
