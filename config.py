"""Smart Bird configuration — env-driven constants and the smart money wallet list.

Every knob that controls the three-layer pipeline lives here: API credentials,
scoring thresholds, polling cadence, dedup windows, and on-disk paths. Keeping
these in one place makes tuning from ``.env`` trivial and keeps the rest of the
codebase declarative.
"""
import os
from typing import Optional

from dotenv import load_dotenv

load_dotenv()


def _env_int(name: str, default: int, min_value: Optional[int] = None,
             max_value: Optional[int] = None) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        import logging
        logging.getLogger('smart-bird.config').warning(
            'Invalid int for %s=%r, falling back to %d', name, raw, default,
        )
        return default
    if min_value is not None and value < min_value:
        import logging
        logging.getLogger('smart-bird.config').warning(
            '%s=%d below min %d, clamping', name, value, min_value,
        )
        return min_value
    if max_value is not None and value > max_value:
        import logging
        logging.getLogger('smart-bird.config').warning(
            '%s=%d above max %d, clamping', name, value, max_value,
        )
        return max_value
    return value


def _env_float(name: str, default: float, min_value: Optional[float] = None,
               max_value: Optional[float] = None) -> float:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        import logging
        logging.getLogger('smart-bird.config').warning(
            'Invalid float for %s=%r, falling back to %f', name, raw, default,
        )
        return default
    if min_value is not None and value < min_value:
        import logging
        logging.getLogger('smart-bird.config').warning(
            '%s=%f below min %f, clamping', name, value, min_value,
        )
        return min_value
    if max_value is not None and value > max_value:
        import logging
        logging.getLogger('smart-bird.config').warning(
            '%s=%f above max %f, clamping', name, value, max_value,
        )
        return max_value
    return value


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------
BIRDEYE_API_KEY = os.getenv('BIRDEYE_API_KEY', '')
BIRDEYE_BASE_URL = 'https://public-api.birdeye.so'

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')

# ---------------------------------------------------------------------------
# Feature flags
# ---------------------------------------------------------------------------
_BOOL_TRUE = ('1', 'true', 'yes', 'on')

# Birdeye's /defi/token_security endpoint requires Lite tier or higher.
# Users on Standard (free) tier should set SECURITY_SCREEN_REQUIRED=false so
# the Layer 1 funnel doesn't drop every candidate on a 401. When disabled, a
# prominent warning fires at startup because honeypot/mintable/rug filtering
# is skipped entirely.
SECURITY_SCREEN_REQUIRED = os.getenv(
    'SECURITY_SCREEN_REQUIRED', 'true',
).strip().lower() in _BOOL_TRUE

# Individual per-layer alert channels. All default ON. Set to false to silence
# a specific channel without disabling the whole bot.
ENABLE_GRADUATION_ALERTS = os.getenv(
    'ENABLE_GRADUATION_ALERTS', 'true',
).strip().lower() in _BOOL_TRUE

ENABLE_SMART_MONEY_ALERTS = os.getenv(
    'ENABLE_SMART_MONEY_ALERTS', 'true',
).strip().lower() in _BOOL_TRUE

ENABLE_EXIT_ALERTS = os.getenv(
    'ENABLE_EXIT_ALERTS', 'true',
).strip().lower() in _BOOL_TRUE

# ---------------------------------------------------------------------------
# Layer 1 — Graduation predictor
# ---------------------------------------------------------------------------
GRADUATION_SCORE_THRESHOLD = _env_int('GRADUATION_SCORE_THRESHOLD', 65, min_value=0, max_value=100)
MIN_HOLDER_COUNT = _env_int("MIN_HOLDER_COUNT", 20, min_value=1, max_value=100000)
MIN_BUY_PRESSURE = _env_float('MIN_BUY_PRESSURE', 0.55, min_value=0.0, max_value=1.0)

# ---------------------------------------------------------------------------
# Layer 2 — Smart money tracker
# ---------------------------------------------------------------------------
SMART_MONEY_LOOKBACK_MINUTES = 15
_raw_wallets = os.getenv('SMART_MONEY_WALLETS', '')
SMART_MONEY_WALLETS = [w.strip() for w in _raw_wallets.split(',') if w.strip()]

# ---------------------------------------------------------------------------
# Layer 3 — Liquidity stress monitor
# ---------------------------------------------------------------------------
LIQUIDITY_DROP_THRESHOLD = _env_float('LIQUIDITY_DROP_THRESHOLD', 0.20, min_value=0.0, max_value=1.0)  # 20%
LIQUIDITY_WINDOW_SECONDS = 5 * 60  # 5 min
LP_CONCENTRATION_THRESHOLD = 0.80  # 80%

# ---------------------------------------------------------------------------
# Polling cadence
# ---------------------------------------------------------------------------
POLL_INTERVAL_SECONDS = _env_int('POLL_INTERVAL_SECONDS', 60, min_value=10, max_value=3600)
LIQUIDITY_POLL_SECONDS = 60
SMART_MONEY_POLL_SECONDS = 45

# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------
ALERT_DEDUP_WINDOW_SECONDS = 60 * 60  # 1 hour

# ---------------------------------------------------------------------------
# Database & logging
# ---------------------------------------------------------------------------
DB_PATH = os.getenv('DB_PATH', '/data/smart-bird.db')
API_CALLS_LOG = os.getenv('API_CALLS_LOG', '/data/api_calls.log')

# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------
MAX_RETRIES = 5
BASE_BACKOFF_SECONDS = 1.5

import logging

log_cfg = logging.getLogger('smart-bird.config')


def validate() -> None:
    """Verify required credentials are present; abort startup otherwise.

    Telegram delivery and the smart-money tracker both fail closed when
    misconfigured (with warnings), but a missing Birdeye API key produces
    nothing but 401s forever and is never useful in production.
    """
    if not BIRDEYE_API_KEY:
        raise SystemExit(
            'BIRDEYE_API_KEY is missing. Set it in .env (see .env.example) '
            'before starting Smart Bird.'
        )
    if not TELEGRAM_BOT_TOKEN:
        log_cfg.warning(
            'TELEGRAM_BOT_TOKEN is empty — the bot will run but cannot deliver alerts.'
        )
    if not TELEGRAM_CHAT_ID:
        log_cfg.warning(
            'TELEGRAM_CHAT_ID is empty — alerts will be skipped at send time.'
        )
