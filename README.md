# StockArb — Statistical Arbitrage (Pairs Trading) Framework

A local, containerized statistical-arbitrage pipeline: it pulls daily OHLCV
data for a configurable stock universe, screens for cointegrated pairs
(Engle-Granger + ADF), computes rolling spread z-scores, and trades signals
automatically against the **Alpaca paper-trading (sandbox) API**. Designed
to run unattended on an Oracle Cloud Ampere (ARM64) instance under plain
Docker/Docker Compose.

This system submits real (paper) orders automatically. Read the whole
README — especially [Risk controls](#risk-controls--safety-notes) — before
running it against your Alpaca account.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│ Docker container (single image, single service)                     │
│                                                                       │
│  entrypoint.sh  →  validates env, initializes SQLite schema, execs   │
│                     supercronic (PID 1) as the in-container scheduler│
│                                                                       │
│  supercronic runs three Python jobs on schedule, output streamed     │
│  straight to stdout (`docker logs`):                                 │
│                                                                       │
│   18:00 ET Mon-Fri  →  run_data_pipeline.py   (yfinance → SQLite)    │
│   18:30 ET Mon-Fri  →  run_analytics.py       (cointegration+ADF,    │
│                                                 rolling z-scores)     │
│   every 15 min,                                                      │
│   09:00-16:00 ET Mon-Fri → run_execution.py   (signal → Alpaca order)│
└─────────────────────────────────────────────────────────────────────┘
              │
              ▼
      ./data/stat_arb.db   (SQLite, bind-mounted, survives restarts)
```

Why this shape, briefly:

- **supercronic instead of a custom Python scheduler loop or host crontab** —
  it's a static Go binary built for containers: no syslog, jobs' stdout/stderr
  go straight to the container's stdout, and it respects `CRON_TZ` per line so
  DST is handled correctly without any code.
- **SQLite over Postgres/etc.** — this is a single-writer-at-a-time batch
  system (jobs never overlap in the schedule above); SQLite in WAL mode
  is more than sufficient and needs zero extra services or ARM64 image
  concerns.
- **`alpaca-py` and `yfinance`** — both pure Python + `requests`, so they
  install from prebuilt wheels on `linux/aarch64` with no compilation.
  `numpy`/`pandas`/`scipy`/`statsmodels` also ship `manylinux_aarch64`
  wheels for Python 3.11, so the Ampere build doesn't compile anything
  either — `build-essential` in the Dockerfile is only a safety net.

## Project layout

```
StockArb/
├── Dockerfile                  # multi-stage, multi-arch (amd64/arm64) build
├── docker-compose.yml
├── crontab                     # supercronic schedule
├── entrypoint.sh                # startup validation + schema init + exec supercronic
├── requirements.txt
├── .env.example                 # copy to .env and fill in
└── app/
    ├── config.py                # env-var driven config, fails fast if invalid
    ├── logging_config.py        # stdout-only structured logging
    ├── db.py                    # SQLite schema + connection helpers (WAL mode)
    ├── utils/retry.py           # shared tenacity backoff policy for network I/O
    ├── data_pipeline/
    │   └── fetch_data.py        # yfinance → ohlcv table, per-ticker fault isolation
    ├── analytics/
    │   ├── cointegration.py     # Engle-Granger + ADF + half-life screen → pairs table
    │   └── zscore.py            # rolling spread z-score → signals table
    ├── execution/
    │   ├── alpaca_client.py     # retrying TradingClient wrapper, paper-only guard
    │   └── trade_pairs.py       # signal → open/close dual-leg orders
    ├── run_data_pipeline.py     # entrypoint for the 18:00 ET job
    ├── run_analytics.py         # entrypoint for the 18:30 ET job
    └── run_execution.py         # entrypoint for the every-15-min job
```

## Setup

### 1. Get Alpaca paper-trading keys

Sign up at [alpaca.markets](https://alpaca.markets), open the **Paper
Trading** dashboard, and generate an API key/secret. Never use live-trading
keys with this project.

### 2. Configure

```bash
cp .env.example .env
$EDITOR .env   # fill in ALPACA_API_KEY / ALPACA_SECRET_KEY at minimum
```

Key settings to review before your first run — see `.env.example` for the
full list with defaults:

| Variable | Purpose |
|---|---|
| `UNIVERSE_TICKERS` | Stock universe to scan for pairs (default: liquid US Utilities names) |
| `LOOKBACK_DAYS` | History window for cointegration testing (default 756 ≈ 3y) |
| `COINTEGRATION_PVALUE_THRESHOLD` / `ADF_PVALUE_THRESHOLD` | Statistical strictness for accepting a pair |
| `ZSCORE_ENTRY_THRESHOLD` / `ZSCORE_EXIT_THRESHOLD` / `ZSCORE_STOPLOSS_THRESHOLD` | Trading rule thresholds |
| `POSITION_SIZE_USD` | Dollar size per leg, per pair |
| `MAX_CONCURRENT_PAIRS` | Hard cap on simultaneously open pair positions |

### 3. Build & run — directly on the Oracle Ampere instance (recommended)

Building natively on the ARM64 host is the simplest, most reliable path —
every dependency installs from a native wheel, no cross-compilation involved:

```bash
git clone <this-repo-url> stockarb && cd stockarb
cp .env.example .env && $EDITOR .env
docker compose build
docker compose up -d
docker compose logs -f
```

### 3b. Alternative — cross-build from an x86 dev machine with buildx

If you'd rather build on your laptop and push to a registry the Ampere
instance pulls from:

```bash
docker buildx create --use --name stockarb-builder   # one-time
docker buildx build --platform linux/arm64 \
  -t <your-registry>/stockarb:latest --push .

# on the Oracle instance:
docker pull <your-registry>/stockarb:latest
docker compose up -d
```

### 4. Verify it's alive

```bash
docker compose logs -f stockarb
```

You should see the entrypoint's startup banner immediately, then nothing
further until the next scheduled job fires (data pipeline at 18:00 ET,
analytics at 18:30 ET, execution every 15 min during market hours). To
sanity-check the pipeline without waiting for the schedule:

```bash
docker compose exec stockarb python -m app.run_data_pipeline
docker compose exec stockarb python -m app.run_analytics
docker compose exec stockarb python -m app.run_execution
```

### 5. Inspect the database

```bash
docker compose exec stockarb python -c "
import sqlite3
conn = sqlite3.connect('/app/data/stat_arb.db')
for row in conn.execute('SELECT pair_id, coint_pvalue, adf_pvalue, half_life_days, is_tradable FROM pairs ORDER BY coint_pvalue LIMIT 10'):
    print(row)
"
```

Or copy `./data/stat_arb.db` to your workstation and open it with any
SQLite browser (DB Browser for SQLite, etc.) — it's a single portable file.

## Trading logic

**Pair discovery** (`analytics/cointegration.py`, daily): for every ticker
pair in the universe, run the Engle-Granger cointegration test, fit the OLS
hedge ratio `A = α + β·B`, run an ADF test on the resulting spread `A − β·B`,
and estimate the spread's mean-reversion half-life via an AR(1) fit. A pair
is marked `is_tradable` only if it passes **all** of: cointegration p-value,
spread ADF p-value, and half-life within `[MIN_HALF_LIFE_DAYS,
MAX_HALF_LIFE_DAYS]` (filters out both noise-fast and effectively-non-reverting
pairs).

**Signal** (`analytics/zscore.py`, daily): for each tradable pair, the spread
is recomputed daily and a rolling z-score over `ZSCORE_LOOKBACK_WINDOW` days
is stored per date in the `signals` table.

**Entry** (`execution/trade_pairs.py`, every 15 min during market hours):
- `zscore >= ZSCORE_ENTRY_THRESHOLD` → spread abnormally high → **short A /
  long B**.
- `zscore <= -ZSCORE_ENTRY_THRESHOLD` → spread abnormally low → **long A /
  short B**.
- Leg sizes are beta-weighted whole shares: `qty_A = floor(POSITION_SIZE_USD /
  price_A)`, `qty_B = floor(POSITION_SIZE_USD * |hedge_ratio| / price_B)`.
- Skipped if either leg isn't tradable/shortable on Alpaca, if the computed
  quantity rounds to zero, or if `MAX_CONCURRENT_PAIRS` is already open.

**Exit**, same job, evaluated before entries:
- `|zscore| <= ZSCORE_EXIT_THRESHOLD` → mean reversion achieved → close both
  legs.
- `|zscore| >= ZSCORE_STOPLOSS_THRESHOLD` → spread kept diverging → stop-loss
  close.

**Leg-failure handling**: orders are submitted leg-by-leg and polled to a
terminal state. If the second leg fails after the first filled, the engine
immediately attempts to unwind the filled leg via Alpaca's close-position
endpoint and logs a `CRITICAL` line either way — grep your logs for
`CRITICAL` to catch anything that needs manual attention.

## Risk controls & safety notes

- **Paper-only, enforced in code**: `AlpacaClient.__init__` refuses to start
  unless `ALPACA_BASE_URL` contains the string `paper`, and `TradingClient`
  is constructed with `paper=True` regardless. There is no code path in this
  project that can submit an order to a live account.
- **No naked single-leg exposure by design**: entries and exits always
  submit both legs; a failed second leg triggers an automatic unwind of the
  first (see above).
- **Bounded position count**: `MAX_CONCURRENT_PAIRS` hard-caps simultaneous
  exposure regardless of how many pairs pass the cointegration screen.
- **Bounded per-pair size**: `POSITION_SIZE_USD` is a fixed dollar amount per
  leg — there's no compounding/auto-sizing off account equity.
- **Market-hours gating**: the execution engine checks Alpaca's `/clock`
  endpoint and no-ops entirely outside regular trading hours, so a stray
  scheduler run on a holiday just logs and exits.
- **Everything is idempotent / crash-safe**: all jobs are short-lived
  processes reading/writing a WAL-mode SQLite file; if a run crashes
  mid-way, the next scheduled run picks up from the persisted `pairs` /
  `signals` / `positions` state, no in-memory state is lost.

This is a paper-trading research/automation framework, not investment
advice, and cointegration on a small, single-sector universe is a starting
point, not a validated strategy — backtest and extend the entry/exit rules
before ever considering real capital.

## Error handling & logging

- Every network call (yfinance downloads, every Alpaca API call) is wrapped
  in a shared `tenacity` retry policy (`app/utils/retry.py`): exponential
  backoff with jitter, retried only on connection/timeout-class exceptions
  (`ConnectionError`, `Timeout`, etc.) — programming errors and bad data are
  never silently retried away.
- Each ticker in the data pipeline and each pair in the analytics engine is
  wrapped in its own `try/except`, so one delisted ticker or one degenerate
  pair can't abort the whole run.
- All logs go to stdout only, UTC-timestamped, one line per event —
  `docker compose logs -f` (or `docker logs -f stockarb`) is the only place
  you need to look. Set `LOG_LEVEL=DEBUG` in `.env` for verbose output.

## Extending

- **Different sector/universe**: edit `UNIVERSE_TICKERS` in `.env` — no code
  changes needed. Note the cointegration screen is O(n²) in universe size;
  15-20 tickers (105-190 pairs) runs in seconds, keep this in mind if you
  scale up considerably.
- **Different schedule**: edit `crontab` (supercronic syntax, `CRON_TZ=`
  prefix per line) and rebuild.
- **Alerting**: `entrypoint.sh` and every job log `CRITICAL` for anything
  needing human attention (unwind failures, zero-ticker fetch, etc.) — pipe
  `docker logs` into your alerting stack of choice (e.g. a `docker logs -f
  | grep CRITICAL` webhook, Grafana Loki, etc.) if you want proactive
  paging instead of pull-based checking.
