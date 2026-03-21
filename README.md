# Whale Scanner

**Agent A** — a deterministic rule engine that scans for unusual options flow during US market hours. It polls the [Unusual Whales](https://unusualwhales.com) API, applies a configurable set of filters, scores candidates by multi-signal confluence, and emits structured alerts for downstream grading by Agent B.

No LLM. Pure Python: async HTTP, Pydantic models, SQLite persistence, structured logging.

---

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [How the Scanner Works](#how-the-scanner-works)
- [Repository Structure](#repository-structure)
- [Data Models](#data-models)
- [API Client](#api-client)
- [Rule Engine](#rule-engine)
  - [Individual Filters](#individual-filters)
  - [Confluence Scoring](#confluence-scoring)
- [State Management](#state-management)
  - [Deduplication](#deduplication)
  - [SQLite Persistence](#sqlite-persistence)
- [Configuration Reference](#configuration-reference)
- [Observability](#observability)
- [Getting Started](#getting-started)
- [Testing](#testing)
- [Deployment](#deployment)
- [License](#license)

---

## Architecture Overview

```
┌──────────────────────────────────────────────────────────┐
│                    Main Orchestrator Loop                  │
│                                                            │
│   ┌─────────┐    ┌───────────┐    ┌──────────────────┐   │
│   │ Market   │───▶│ UW API    │───▶│ Deduplication    │   │
│   │ Clock    │    │ Client    │    │ Cache            │   │
│   └─────────┘    └───────────┘    └──────────────────┘   │
│                        │                    │              │
│                        │          ┌─────────▼──────────┐  │
│               ┌────────┘          │ Rule Engine         │  │
│               │                   │ (5 filter functions) │  │
│               │                   └─────────┬──────────┘  │
│               │                             │              │
│       ┌───────▼─────────┐       ┌──────────▼───────────┐ │
│       │ Dark Pool +     │──────▶│ Confluence Enricher   │ │
│       │ Market Tide     │       │ (cross-signal scoring) │ │
│       └─────────────────┘       └──────────┬───────────┘ │
│                                            │              │
│                              ┌─────────────▼────────┐    │
│                              │ SQLite DB + Queue     │    │
│                              │ (persist + emit)      │    │
│                              └──────────────────────┘    │
└──────────────────────────────────────────────────────────┘
                                      │
                                      ▼
                              Agent B (Grader)
```

The scanner runs as a single async Python process. Every cycle it concurrently fetches three data sources from the Unusual Whales API (flow alerts, dark pool prints, market tide), deduplicates against recently seen trades, runs each alert through a chain of configurable filter functions, enriches passing candidates with cross-signal confluence data, persists everything to SQLite, and pushes candidates into an in-memory queue for Agent B consumption.

---

## How the Scanner Works

Each polling cycle (default: every 30 seconds during market hours) follows this sequence:

1. **Market hours check** — The `MarketClock` utility determines whether US equity markets are open (weekdays, 9:15 AM–4:00 PM ET by default). Outside these hours the scanner sleeps, re-checking every 5 minutes.

2. **Concurrent API polling** — Three `httpx` async requests fire in parallel via `asyncio.gather`: flow alerts, recent dark pool prints, and market tide (net call/put premium sentiment). Partial failures are handled gracefully — if dark pool data fails, the scanner still processes flow alerts.

3. **Deduplication** — Each alert is hashed by its key fields (ticker, strike, expiry, direction). If the hash exists in the TTL-based cache, the alert is skipped. This prevents the same trade from being flagged across consecutive cycles.

4. **Rule engine evaluation** — Every new alert runs through all enabled filter functions. Each filter returns a `SignalMatch` (with rule name, weight, and human-readable detail) or `None`. If the alert triggers at least `min_signals_required` filters (default: 2), it becomes a `Candidate`.

5. **Confluence enrichment** — Candidates are checked against dark pool prints (same ticker, sufficient notional, within the lookback window) and market tide direction. Confirming signals add weight to the confluence score.

6. **Persistence and output** — Candidates are written to SQLite and pushed to an in-memory queue. Cycle metadata (alerts received, candidates flagged, errors, duration) is logged as structured JSON.

---

## Repository Structure

```
whale-scanner/
├── .env.example                  # Template for secrets (UW_API_TOKEN)
├── .gitignore
├── pyproject.toml                # Project metadata + dependencies
├── README.md
├── config/
│   └── rules.yaml                # All tunable parameters — single source of truth
├── src/
│   └── scanner/
│       ├── __init__.py
│       ├── main.py               # Entry point — async orchestrator loop
│       ├── client/
│       │   ├── __init__.py
│       │   ├── uw_client.py      # Unusual Whales API wrapper (endpoint whitelist)
│       │   └── rate_limiter.py   # Token bucket rate limiter
│       ├── models/
│       │   ├── __init__.py
│       │   ├── flow_alert.py     # Pydantic model for raw flow alerts
│       │   ├── dark_pool.py      # Pydantic model for dark pool prints
│       │   ├── market_tide.py    # Pydantic model for market sentiment
│       │   └── candidate.py      # Output model — what Agent B receives
│       ├── rules/
│       │   ├── __init__.py
│       │   ├── engine.py         # Core rule engine — registry + batch evaluation
│       │   ├── filters.py        # Individual filter functions (pure, no side effects)
│       │   └── confluence.py     # Cross-signal enrichment (dark pool + market tide)
│       ├── state/
│       │   ├── __init__.py
│       │   ├── dedup.py          # TTL-based deduplication cache
│       │   └── db.py             # SQLite persistence (candidates, raw alerts, cycles)
│       ├── output/
│       │   ├── __init__.py
│       │   ├── queue.py          # In-memory async queue for Agent B
│       │   └── notifier.py       # Optional Slack/Discord webhook alerts
│       └── utils/
│           ├── __init__.py
│           ├── clock.py          # Market hours helper (timezone-aware)
│           └── logging.py        # Structured logging setup (JSON via structlog)
├── tests/
│   ├── conftest.py               # Shared fixtures
│   ├── fixtures/                 # Saved API response JSON for deterministic tests
│   ├── test_client.py
│   ├── test_filters.py
│   ├── test_engine.py
│   ├── test_confluence.py
│   ├── test_dedup.py
│   └── test_integration.py
├── scripts/
│   ├── backfill.py               # Pull historical flow for backtesting
│   └── replay.py                 # Replay saved JSON through the engine
└── docker/
    ├── Dockerfile
    └── docker-compose.yaml
```

---

## Data Models

All models use Pydantic v2 for strict validation and serialization. Field names use aliases to map directly from the Unusual Whales API JSON response.

### FlowAlert

Represents a single raw options flow alert from `/api/option-trades/flow-alerts`. Key computed properties:

| Property | Derivation |
|---|---|
| `direction` | `"bullish"` if calls, `"bearish"` if puts |
| `dte` | Calendar days from today to expiry |
| `otm_percentage` | `abs(strike - underlying_price) / underlying_price × 100` |
| `volume_oi_ratio` | `total_size / open_interest` |

### Candidate

The output model emitted to Agent B. Contains the original alert data plus the scanner's analysis:

| Field | Description |
|---|---|
| `signals` | List of `SignalMatch` objects (rule name, weight, human-readable detail) |
| `confluence_score` | Weighted sum of all matched signals |
| `dark_pool_confirmation` | Whether a matching dark pool print was found |
| `market_tide_aligned` | Whether market sentiment agrees with the signal direction |
| `raw_alert_id` | Original UW API alert ID for traceability |

### DarkPoolPrint and MarketTide

Supporting models for cross-signal confluence. `MarketTide` exposes a `direction` property (`"bullish"`, `"bearish"`, or `"neutral"`) derived from net call/put premium ratios.

---

## API Client

`UWClient` wraps the Unusual Whales API using `httpx.AsyncClient`. It enforces a strict **endpoint whitelist** — only known, validated paths are called. This prevents accidentally hitting nonexistent endpoints.

Validated endpoints:

| Endpoint | Purpose |
|---|---|
| `/api/option-trades/flow-alerts` | Primary signal source — unusual options flow |
| `/api/darkpool/recent` | Market-wide dark pool prints |
| `/api/darkpool/{ticker}` | Ticker-specific dark pool prints |
| `/api/market/market-tide` | Net call/put premium market sentiment |
| `/api/screener/option-contracts` | Options contract screener |

Authentication uses a Bearer token plus a client API ID header. The client is backed by a token bucket `RateLimiter` (default: 30 calls/minute) to stay well under API rate limits.

All API methods return validated Pydantic models. Parse failures are logged as warnings and skipped rather than crashing the cycle.

---

## Rule Engine

### Individual Filters

Filters are pure functions: they take a `FlowAlert` and a config dict, and return a `SignalMatch` or `None`. No side effects, no API calls — trivially testable.

| Filter | What it detects | Default threshold |
|---|---|---|
| `check_otm` | Deep out-of-the-money strikes | 5–50% OTM |
| `check_premium` | Large total premium | ≥ $25,000 |
| `check_volume_oi` | Volume dwarfing open interest | Size > OI, or ratio ≥ 2.0× |
| `check_expiry` | Near-term expiry (directional bets) | 1–14 DTE |
| `check_execution_type` | Sweeps and blocks (urgency/size) | Require sweep or block |

Filters are registered in `FILTER_REGISTRY`, a dict mapping config keys to functions. Adding a new filter means writing a function and adding one line to the registry.

### Confluence Scoring

The `RuleEngine` runs all enabled filters against each alert. If the number of matched signals meets the `min_signals_required` threshold (default: 2), the alert becomes a `Candidate` with a `confluence_score` computed as the weighted sum of matched signals.

Default signal weights:

| Signal | Weight |
|---|---|
| Dark pool confirmation | 2.0 |
| Premium size | 1.5 |
| OTM depth | 1.0 |
| Volume/OI ratio | 1.0 |
| Execution type | 1.0 |
| Near-term expiry | 0.5 |
| Market regime alignment | 0.5 |

The `ConfluenceEnricher` then cross-references candidates against dark pool prints (same ticker, ≥ $500K notional, within 30-minute lookback) and market tide direction, appending additional `SignalMatch` entries and adjusting the confluence score.

---

## State Management

### Deduplication

The `DedupCache` prevents the same trade from being flagged across consecutive polling cycles. It hashes alerts by configurable key fields (ticker, strike, expiry, direction) using SHA-256 and stores truncated hashes with timestamps in an in-memory dict. Entries expire after a configurable TTL (default: 60 minutes). Lazy cleanup runs on each lookup.

### SQLite Persistence

`ScannerDB` uses `aiosqlite` to store three tables:

| Table | Purpose |
|---|---|
| `candidates` | Every candidate the scanner flags, including signals JSON, confluence score, and downstream outcome fields (`graded_at`, `grade_score`, `outcome`) for Agent B to populate |
| `raw_alerts` | Raw API payloads for replay and backtesting |
| `scan_cycles` | Per-cycle metadata: start/end times, alert count, candidate count, error count |

The database is zero-config (SQLite file at `data/scanner.db`), survives restarts, and provides full traceability from raw alert to final outcome.

---

## Configuration Reference

All tunable parameters live in `config/rules.yaml`. No magic numbers in code.

### Polling

| Parameter | Default | Description |
|---|---|---|
| `flow_alerts_interval_seconds` | 30 | Seconds between flow alert polls |
| `dark_pool_interval_seconds` | 120 | Seconds between dark pool polls |
| `market_tide_interval_seconds` | 120 | Seconds between market tide polls |
| `market_open` / `market_close` | 09:30 / 16:00 | Market hours (ET) |
| `pre_market_start` | 09:15 | Start polling before open |

### Filters

Each filter section has an `enabled` flag and threshold values. Disabling a filter in YAML removes it from the evaluation pipeline without code changes.

### Confluence

| Parameter | Default | Description |
|---|---|---|
| `min_signals_required` | 2 | Minimum matched filters to produce a candidate |
| `weights` | (see table above) | Per-signal weights for confluence scoring |

### Deduplication

| Parameter | Default | Description |
|---|---|---|
| `ttl_minutes` | 60 | How long to remember seen trades |
| `key_fields` | ticker, strike, expiry, direction | Fields that define a "duplicate" |

---

## Observability

Every cycle emits a structured JSON log line via `structlog`:

```json
{
  "event": "cycle_complete",
  "cycle": 142,
  "alerts": 47,
  "new": 12,
  "candidates": 2,
  "dedup_cache_size": 89,
  "duration_ms": 1840,
  "timestamp": "2026-03-20T15:30:12Z"
}
```

Key metrics to monitor: alerts per cycle (data connectivity), dedup hit rate (trade freshness), candidates per hour (rule tightness), API error rate (rate limiting), and cycle duration (polling drift).

Individual candidate flags are also logged with ticker, direction, score, and matched signal names.

For health monitoring, the scanner can write a heartbeat timestamp to `data/heartbeat.txt` every cycle. A cron job or process monitor can restart the process if the file goes stale.

---

## Getting Started

### Requirements

- Python 3.11+
- [Unusual Whales](https://unusualwhales.com) API token

### Installation

```bash
# Clone the repository
git clone https://github.com/dliuu/agentic-trader.git
cd agentic-trader

# Create virtual environment
python3.11 -m venv .venv
source .venv/bin/activate

# Install in editable mode with dev dependencies
pip install -e ".[dev]"

# Set up environment
cp .env.example .env
# Edit .env and set UW_API_TOKEN=your_token_here
```

### Running

```bash
# Via module
python -m scanner.main

# Via entry point
whale-scanner
```

The scanner runs during US market hours (configurable in `config/rules.yaml`). Outside those hours it sleeps and re-checks periodically.

---

## Testing

Tests use `pytest` with `pytest-asyncio` for async support and `respx` for mocking `httpx` HTTP calls against saved JSON fixtures.

```bash
# Run all tests
pytest

# Run with verbose output
pytest -v

# Run a specific test file
pytest tests/test_filters.py
```

### Capturing test fixtures

Save real API responses as JSON for deterministic testing:

```bash
curl -H "Authorization: Bearer $UW_TOKEN" \
     -H "UW-CLIENT-API-ID: 100001" \
     "https://api.unusualwhales.com/api/option-trades/flow-alerts?limit=20&is_otm=true" \
     | python -m json.tool > tests/fixtures/flow_alerts_sample.json
```

### Replay script

Replay saved fixtures through the rule engine to tune thresholds without burning API calls:

```bash
python scripts/replay.py tests/fixtures/flow_alerts_sample.json
```

---

## Deployment

### Docker

```bash
docker compose -f docker/docker-compose.yaml up --build
```

The compose file mounts `data/` for SQLite persistence and `config/` for live config changes without rebuilds.

### Minimal VPS

A $5/month VPS (1 CPU, 1 GB RAM) is sufficient. The scanner is I/O-bound (waiting on HTTP responses), not CPU-bound. Run behind `systemd` or `supervisord` for process management.

---

## Dependencies

| Package | Purpose |
|---|---|
| `httpx` | Async HTTP client with clean timeout/retry semantics |
| `pydantic` | Data validation — catches API schema drift immediately |
| `pyyaml` | Configuration file parsing |
| `python-dotenv` | `.env` file loading for secrets |
| `structlog` | Structured JSON logging with context fields |
| `aiosqlite` | Async SQLite for zero-config persistence |
| `asyncio-throttle` | Rate limiting helper |

Dev dependencies: `pytest`, `pytest-asyncio`, `respx` (httpx mocking), `ruff` (linter/formatter), `mypy` (type checking).

---

## License

MIT
