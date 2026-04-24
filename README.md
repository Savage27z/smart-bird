# Smart Bird 🐦

> Four-layer Solana token intelligence bot — graduation predictor + smart money tracker + liquidity stress monitor + social sentiment analyzer, powered by Birdeye Data API & DexScreener

---

## 🏆 Built for Birdeye Data BIP Competition — Sprint 1 (April 2026)

A unified signal pipeline that stacks four distinct on-chain intelligence layers into a single, high-conviction Telegram alert. Every alert comes with interactive inline buttons, and the bot tracks its own accuracy over time. Every Birdeye call is logged to `api_calls.log`, and the stack is packaged as a reproducible `docker compose` deployment.

---

## 🧠 What it does

Smart Bird runs four asynchronous analysis layers in parallel and stitches them into actionable Telegram alerts with inline buttons:

1. **Layer 1 — Graduation Predictor.** Scans newly-listed Solana tokens and scores them 0–100 on volume velocity, holder base, buy pressure and short-window price trajectory. Honeypot / mintable / top-holder-concentrated rugs are filtered out before scoring. Results are displayed as visual bar charts.
2. **Layer 2 — Smart Money Tracker.** Watches the recent swap history of every Layer-1 passer for entries by a curated alpha-wallet set (configurable via env). Validates the wallet still holds the token via portfolio lookup before confirming.
3. **Layer 3 — Liquidity Stress Monitor.** Snapshots liquidity and LP concentration every minute for every active token. Powers **independent exit alerts** whenever liquidity drops >20% in a 5-minute window or the top-10 holder share exceeds 80%. Automatically expires stale tokens after a configurable age (default 2h).
4. **Layer 4 — Social Sentiment Analyzer.** Computes a 0–100 buzz score from four signals: trade engagement (unique wallets), volume buzz (volume/mcap ratio), social presence (via free DexScreener API — no key needed), and community growth (holders per minute). Augments all alerts with a sentiment panel.

### Alert channels

Smart Bird emits four alert channels, each independently deduped on a 1-hour window:

- 🎯 **Graduation Watch** — Layer 1 alone. Fires the moment a token clears the graduation threshold, before Layer 2 confirmation.
- 🐋 **Smart Money Move** — Layer 2 alone. Fires when a tracked alpha wallet buys a Layer-1 passer.
- 🚨 **Smart Bird Alert** (flagship) — Layers 1 + 2 aligned, Layer 3 liquidity attached, Layer 4 sentiment included.
- 🔴 **Exit Signal** — Layer 3 alone. Fires when a watched token's liquidity collapses or top-holder concentration spikes.

Each channel can be silenced independently via env (`ENABLE_GRADUATION_ALERTS`, `ENABLE_SMART_MONEY_ALERTS`, `ENABLE_EXIT_ALERTS`). The flagship Smart Bird Alert is always on.

### Interactive alerts

Every alert includes **inline keyboard buttons**:

- 🔍 **Deep Dive** — runs a full token analysis inline (score breakdown, sentiment, liquidity, holders)
- ⭐ **Watch** — adds the token to your personal watchlist
- 📊 **Chart** — opens the Birdeye chart directly in your browser

### Performance tracking

The bot records the price of every alerted token and automatically checks it at **1h, 6h, and 24h** intervals. Use `/performance` to see win rate and average return — built-in self-accountability.

---

## 🏗️ Architecture

```
        ┌──────────────────────────────────────────────┐
        │              Birdeye Data API                │
        │   /defi/v2/tokens/new_listing                │
        │   /defi/token_security                       │
        │   /defi/token_overview    (TTL-cached)       │
        │   /defi/token_trending                       │
        │   /defi/ohlcv             (TTL-cached)       │
        │   /defi/txs/token         (TTL-cached)       │
        │   /v1/wallet/token_list   (TTL-cached)       │
        │   /defi/v3/token/holder   (TTL-cached)       │
        └────────────────────┬─────────────────────────┘
                             │ async aiohttp + response cache
                             ▼
        ┌──────────────────────────────────────────────┐
        │            BirdeyeClient (client.py)         │
        │   • shared session  • exp backoff            │
        │   • TTL response cache (configurable)        │
        │   • api_calls.log writer                     │
        └──┬──────────┬──────────┬──────────┬──────────┘
           │          │          │          │
   ┌───────▼───┐ ┌────▼────┐ ┌──▼───────┐ ┌▼──────────┐
   │  Layer 1  │ │ Layer 2 │ │ Layer 3  │ │  Layer 4  │
   │ Gradua-   │ │ Smart $ │ │ Liquid-  │ │ Sentiment │
   │ tion      │ │ Tracker │ │ ity      │ │ Analyzer  │
   └─────┬─────┘ └───┬─────┘ └──┬───────┘ └┬──────────┘
         │           │          │           │ DexScreener
         └─────┬─────┴─────┬────┘           │  (free API)
               ▼           ▼                │
        ┌────────────┐ ┌──────────┐         │
        │Signal Queue│ │ SQLite DB│◄────────┘
        └──────┬─────┘ └──────────┘
               ▼
        ┌──────────────┐    ┌───────────────┐
        │ Alert        │───►│ Telegram Bot   │
        │ Dispatcher   │    │ • inline keys  │
        └──────────────┘    │ • /token cmd   │
        ┌──────────────┐    │ • /performance │
        │ Performance  │───►│ • /mywatchlist │
        │ Tracker      │    └───────────────┘
        └──────────────┘
```

---

## 🔌 APIs used

### Birdeye Data API

| Endpoint | Purpose |
|---|---|
| `GET /defi/v2/tokens/new_listing` | Layer 1 candidate pool — freshly listed Solana tokens. |
| `GET /defi/token_security` | Layer 1 filter — drops honeypots, mintable tokens, and top-10-concentrated rugs. |
| `GET /defi/token_overview` | Price, market cap, liquidity, holder count, and short-window price deltas. |
| `GET /defi/token_trending` | Sanity smoke test at startup — also counts toward total API usage. |
| `GET /defi/ohlcv` | 1-minute OHLCV candles for Layer 1's volume-velocity score. |
| `GET /defi/txs/token` | Recent trades — drives buy/sell ratio, smart-money detection, and trade engagement. |
| `GET /v1/wallet/token_list` | Layer 2 confirmation — verify the smart wallet still holds the token. |
| `GET /defi/v3/token/holder` | Layer 3 — top-10 holder share as an LP-concentration proxy. |

### DexScreener (free, no auth)

| Endpoint | Purpose |
|---|---|
| `GET /tokens/v1/solana/{address}` | Layer 4 — social links (website, Twitter, Telegram) for sentiment scoring. |

### Response cache

All frequently-called Birdeye endpoints are served through a configurable TTL cache. Cross-layer duplicate calls for the same token within the TTL window are served from memory. Configurable via env:

| Variable | Default | Description |
|---|---|---|
| `CACHE_TTL_OVERVIEW` | 30s | Token overview |
| `CACHE_TTL_SECURITY` | 120s | Security data |
| `CACHE_TTL_OHLCV` | 30s | OHLCV candles |
| `CACHE_TTL_TRADES` | 20s | Recent trades |
| `CACHE_TTL_HOLDERS` | 60s | Holder data |
| `CACHE_TTL_PORTFOLIO` | 30s | Wallet portfolio |

---

## ⚙️ Setup (Docker — recommended)

```bash
git clone https://github.com/Pyrex-13/smart-bird.git
cd smart-bird
cp .env.example .env
# edit .env with your BIRDEYE_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
docker compose up --build -d
docker compose logs -f smart-bird
```

> **Public mode by default.** Any Telegram user who sends `/start` to your bot subscribes and will receive every alert. Use `/stop` to unsubscribe. If you want a **private** single-recipient bot instead, set `TELEGRAM_CHAT_ID` and don't share the bot username publicly.

> **Layer 2 requires `SMART_MONEY_WALLETS`** — a comma-separated list of Solana wallet addresses you want to track. Without it, Layer 2 is a no-op and no entry alerts will fire (exit alerts still work).

The SQLite database and the `api_calls.log` file are persisted in the named volume `smart-bird-data` mounted at `/data`.

### Local (non-Docker) run

```bash
pip install -r requirements.txt
python main.py
```

Override the data paths for local runs:

```
DB_PATH=./smart-bird.db
API_CALLS_LOG=./api_calls.log
```

---

## 🤖 Telegram commands

| Command | Description |
|---|---|
| `/start` | Subscribe this chat to Smart Bird alerts. |
| `/stop` | Unsubscribe. |
| `/status` | Live counters: subscribers, tracked tokens, layer stats, alerts in last 24h. |
| `/watchlist` | Tokens currently in the pipeline (system-wide). |
| `/mywatchlist` | Your personal watched tokens (added via ⭐ button). |
| `/token <address>` | Deep dive into any token — full score breakdown, sentiment, liquidity, holders. |
| `/performance` | Alert accuracy: win rate and avg return at 1h/6h/24h intervals. |

---

## 🪜 How signals stack

- **Entry alert** (🚨) fires only when **Layer 1 AND Layer 2** both pass for the same token, and a fresh Layer 3 snapshot succeeds. Layer 4 sentiment is attached as supplementary data.
  - Layer 1 requires `score ≥ GRADUATION_SCORE_THRESHOLD` (default 65) AND `holders ≥ MIN_HOLDER_COUNT` AND `buy_pressure ≥ MIN_BUY_PRESSURE`.
  - Layer 2 requires a known alpha wallet to have bought within the last 15 minutes **and** still hold the token.
- **Exit alert** (🔴) fires independently from Layer 3 whenever a token already on the watchlist shows a >20% liquidity drop over a 5-minute window OR an LP concentration above 80%.
- Layer 4 sentiment is **additive** — it augments alerts but never gates them. If DexScreener is unreachable, the pipeline continues with sentiment score 0.
- The `(address, alert_type)` pair is deduped across a rolling 1-hour window so a flapping token can't spam the channel.

---

## 📟 Sample alerts

### 🎯 Graduation Watch (Layer 1)
```
🎯 *GRADUATION WATCH*
Token: $PEPE2 (`So11...1112`)
Price: $0.000123 | MCap: $842,000
✅ Holders: 312 | Buy Pressure: 72%

📊 *Score Breakdown* (78/100)
`Vol  ` ████████░░ 20/25
`Hold ` ██████████ 25/25
`Buy  ` ██████░░░░ 15/25
`Traj ` ███████░░░ 18/25
⚡ Signal: STRONG

📡 *Sentiment* (62/100 — BUZZING)
`Trad ` ████████░░ 20/25
`Buzz ` ██████░░░░ 15/25
`Socl ` ███████░░░ 17/25
`Grow ` ████░░░░░░ 10/25
🔗 Socials: website, twitter

⏳ Awaiting smart-money confirmation for full alert
🔗 Birdeye: https://birdeye.so/token/So11...1112

[🔍 Deep Dive] [⭐ Watch]
[        📊 Chart        ]
```

### 🔴 Exit Signal (Layer 3)
```
🔴 *EXIT SIGNAL* — $PEPE2
Liquidity dropped 34% in 4min
LP concentration: 87%

[🔍 Deep Dive] [⭐ Watch]
[        📊 Chart        ]
```

---

## 📑 API usage proof

Every logical Birdeye call writes exactly one audit line to `api_calls.log` once it resolves — success, non-retryable failure, or final retry exhaustion. Cached responses do not produce audit lines:

```
[2026-04-18T21:10:05.123456+00:00] [GET /defi/token_overview] [200] [So11...1112]
```

The log defaults to `/data/api_calls.log` so the Docker volume captures it without configuration. Tail it live with:

```bash
docker compose exec smart-bird tail -f /data/api_calls.log
```

---

## 🧾 Project layout

```
/
├── main.py                  # orchestrator + loops + graceful shutdown
├── config.py                # env-driven constants
├── birdeye/
│   ├── client.py            # async aiohttp Birdeye wrapper + TTL cache
│   ├── new_listings.py      # Layer 1 — graduation predictor
│   ├── smart_money.py       # Layer 2 — alpha-wallet tracker
│   ├── liquidity.py         # Layer 3 — liquidity stress monitor
│   └── sentiment.py         # Layer 4 — social sentiment analyzer
├── bot/
│   ├── telegram_bot.py      # python-telegram-bot v21 + inline keyboards
│   └── formatter.py         # Markdown formatters + score visualizations
├── db/
│   └── database.py          # SQLite, async via asyncio.to_thread
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
└── .gitignore
```

---

## 📝 License

MIT
