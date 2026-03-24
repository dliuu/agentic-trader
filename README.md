# Whale Scanner

A two-agent pipeline for unusual options flow:

- **Agent A (Scanner)** — Deterministic rule engine that scans for unusual options flow during US market hours. Polls the [Unusual Whales](https://unusualwhales.com) API, applies configurable filters, scores candidates by multi-signal confluence, and pushes them to the grader.
- **Agent B (Grader)** — LLM-powered grading layer (Claude) that scores each candidate 1–100, validates conviction, and emits passing trades to a scored queue.

**Key features:** Confluence enrichment (dark pool + market tide), `--force` to bypass market hours, `--max-cycles` for limited runs, dual logging (terminal + `scanner.json.log`), grader pass-through mode (`enabled: false`), and audit logging to SQLite.

### Quick run (full pipeline)

```bash
source .venv/bin/activate
python -m scanner.run_pipeline --force --max-cycles 3
```

See [Getting Started](#getting-started) for full venv setup and run instructions.

---

## Table of Contents

- [Getting Started](#getting-started)
- [Architecture Overview](#architecture-overview)
- [How the Scanner Works](#how-the-scanner-works)
- [How the Grader Works](#how-the-grader-works)
- [Running on Live Market Data](#running-on-live-market-data)
- [Benchmarking Results](#benchmarking-results)
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
- [Grader Configuration](#grader-configuration)
- [Observability](#observability)
- [Testing](#testing)
- [Deployment](#deployment)
- [License](#license)

---

## Getting Started

### Prerequisites

- **Python 3.11+** (required; the project does not support Python 3.9 or 3.10)
- [Unusual Whales](https://unusualwhales.com) API token
- [Anthropic](https://console.anthropic.com/) API key (for the grader)

### Step 1: Create and activate the virtual environment

```bash
cd whale-scanner

# Create venv with Python 3.11
python3.11 -m venv .venv

# Activate (macOS/Linux)
source .venv/bin/activate

# Activate (Windows)
.venv\Scripts\activate
```

### Step 2: Install the project

```bash
pip install -e ".[dev,grader]"
```

This installs the project in editable mode with dev tools (pytest, respx) and grader dependencies (anthropic SDK).

### Step 3: Configure API keys

```bash
cp .env.example .env
```

Edit `.env` and set:

```
UW_API_TOKEN=your_unusual_whales_token
ANTHROPIC_API_KEY=your_anthropic_api_key
```

### Step 4: Run the pipeline

```bash
# Full pipeline (scanner + grader) — test run, 3 cycles
python -m scanner.run_pipeline --force --max-cycles 3

# Full pipeline — live during market hours (runs indefinitely)
python -m scanner.run_pipeline

# Scanner only (no grading)
python -m scanner.main --force --max-cycles 3
```

### Stop a running pipeline

Press **`Ctrl+C`** in the terminal to interrupt the process.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                     Full Pipeline (run_pipeline)                      │
│                                                                       │
│  ┌─────────────────────────────────────┐  ┌────────────────────────┐ │
│  │         Agent A (Scanner)            │  │     Agent B (Grader)    │ │
│  │                                      │  │                         │ │
│  │  Market Clock → UW API → Dedup       │  │  Candidate Queue        │ │
│  │       → Rule Engine → Confluence     │──▶│       → Context Builder │ │
│  │       → SQLite + Queue               │  │       → Claude (LLM)    │ │
│  │                                      │  │       → Parser → Score  │ │
│  └─────────────────────────────────────┘  └───────────┬────────────┘ │
│                                                       │              │
│                                            Scored Queue → (Agent C)   │
└─────────────────────────────────────────────────────────────────────┘
```

The scanner runs as an async producer: every cycle it fetches flow alerts, dark pool prints, and market tide from the Unusual Whales API, deduplicates, runs the rule engine, enriches with confluence data, persists to SQLite, and pushes candidates into a shared queue. The grader runs concurrently as a consumer: it pulls candidates, enriches them with quote/greeks/news/insider data via the context builder, sends them to Claude for scoring, parses the JSON response, and pushes passing trades (score ≥ threshold) to the scored queue. Both agents share `src/shared/` (models, config, db).

---

## How the Scanner Works

Each polling cycle (default: every 30 seconds during market hours) follows this sequence:

1. **Market hours check** — The `MarketClock` utility determines whether US equity markets are open (weekdays, 9:15 AM–4:00 PM ET by default). Outside these hours the scanner sleeps, re-checking every 5 minutes.

2. **Concurrent API polling** — Three `httpx` async requests fire in parallel via `asyncio.gather`: flow alerts, recent dark pool prints, and market tide (net call/put premium sentiment). Partial failures are handled gracefully — if dark pool data fails, the scanner still processes flow alerts.

3. **Deduplication** — Each alert is hashed by its key fields (ticker, strike, expiry, direction). If the hash exists in the TTL-based cache, the alert is skipped. This prevents the same trade from being flagged across consecutive cycles.

4. **Rule engine evaluation** — Every new alert runs through all enabled filter functions. Each filter returns a `SignalMatch` (with rule name, weight, and human-readable detail) or `None`. If the alert triggers at least `min_signals_required` filters (default: 2), it becomes a `Candidate`.

5. **Confluence enrichment** — Candidates are checked against dark pool prints (same ticker, sufficient notional, within the lookback window) and market tide direction. Confirming signals add weight to the confluence score. Results include `dark_pool_confirmation` and `market_tide_aligned` flags in the database.

6. **Persistence and output** — Candidates are written to SQLite and pushed to an in-memory queue. Cycle metadata (alerts received, candidates flagged, errors, duration) is logged as structured JSON to both the terminal and `scanner.json.log`.

---

## How the Grader Works

Agent B (the grader) consumes candidates from the shared queue and runs them through:

1. **Context builder** — Fetches quote, greeks, news, insider/congressional trades from the Unusual Whales API (concurrent, with graceful degradation on partial failures).
2. **Prompt assembly** — Renders system + user prompts from `GradingContext`, including the `GradeResponse` JSON schema.
3. **LLM call** — Sends to Claude (default: `claude-sonnet-4-20250514`) with a 512-token limit.
4. **Parse & validate** — Strips markdown fences, extracts JSON, validates with Pydantic. On parse failure, retries once.
5. **Audit** — Writes every grading decision to the `grades` table in `data/trades.db`.
6. **Routing** — If score ≥ `score_threshold` (default 70), emits a `ScoredTrade` to the scored queue; otherwise returns `None`.

With `grader.enabled: false` in config, the grader skips LLM calls and passes candidates through as `ScoredTrade` with `grade=None`.

---

## Running on Live Market Data

Ensure the venv is activated and API keys are set (see [Getting Started](#getting-started)).

### Command-Line Options

| Flag | Description |
|------|-------------|
| `--force` | Ignore market hours and poll immediately (for testing or off-hours runs) |
| `--max-cycles N` | Run at most N polling cycles, then exit (useful for limited test runs) |

### Example Runs

```bash
# Full pipeline (scanner + grader) — test run, 5 cycles
python -m scanner.run_pipeline --force --max-cycles 5

# Full pipeline — live during market hours
python -m scanner.run_pipeline

# Scanner only (no grader)
python -m scanner.main --force --max-cycles 5
python -m scanner.main
```

### Output Locations

| Output | Location | Description |
|--------|----------|-------------|
| Scanner DB | `data/scanner.db` | Candidates, raw alerts, scan cycles |
| Grader DB | `data/trades.db` | Grades table (score, verdict, rationale, token counts) |
| Log file | `scanner.json.log` | Structured JSON logs (terminal + file) |
| Heartbeat | `data/heartbeat.txt` | UTC timestamp updated every cycle |

---

## Benchmarking Results

After running the scanner, use SQLite to analyze performance.

### Quick Stats

```bash
# Total candidates and breakdown by confluence signals
sqlite3 data/scanner.db "
SELECT 
  COUNT(*) as total_candidates,
  SUM(dark_pool_confirmation) as dark_pool_confirmed,
  SUM(market_tide_aligned) as market_tide_aligned
FROM candidates;
"

# Candidates per day (by scanned_at date)
sqlite3 data/scanner.db "
SELECT date(scanned_at) as day, COUNT(*) as candidates
FROM candidates
GROUP BY day
ORDER BY day DESC
LIMIT 10;
"

# Top tickers by candidate count
sqlite3 data/scanner.db "
SELECT ticker, COUNT(*) as n
FROM candidates
GROUP BY ticker
ORDER BY n DESC
LIMIT 20;
"
```

### Cycle Health

```bash
# Recent cycles: alerts received, candidates flagged, errors
sqlite3 data/scanner.db "
SELECT id, started_at, alerts_received, candidates_flagged, errors
FROM scan_cycles
ORDER BY id DESC
LIMIT 20;
"

# Check heartbeat freshness (should update every ~30 seconds during market hours)
cat data/heartbeat.txt
```

### Confluence Quality

```bash
# Candidates with dark pool confirmation (stronger signal)
sqlite3 data/scanner.db "
SELECT ticker, direction, premium_usd, confluence_score, scanned_at
FROM candidates
WHERE dark_pool_confirmation = 1
ORDER BY scanned_at DESC
LIMIT 20;
"

# High-confluence candidates (score >= 4)
sqlite3 data/scanner.db "
SELECT ticker, direction, premium_usd, confluence_score,
       dark_pool_confirmation, market_tide_aligned, scanned_at
FROM candidates
WHERE confluence_score >= 4
ORDER BY confluence_score DESC, scanned_at DESC;
"
```

### Log Analysis

JSON logs in `scanner.json.log` can be parsed with `jq`:

```bash
# Extract cycle_complete events
jq -c 'select(.event == "cycle_complete")' scanner.json.log | tail -20

# Average cycle duration
jq -s 'map(select(.event == "cycle_complete") | .duration_ms) | add / length' scanner.json.log
```

---

## Repository Structure

```
whale-scanner/
├── .env.example                  # Template for secrets (UW_API_TOKEN, ANTHROPIC_API_KEY)
├── .gitignore
├── .github/workflows/test.yml    # CI: pytest on push/PR
├── pyproject.toml                # Project metadata + dependencies
├── README.md
├── scanner.json.log              # Runtime: JSON logs (stdout + file; gitignored)
├── config/
│   └── rules.yaml                # Scanner + grader config — single source of truth
├── data/                         # Runtime: SQLite, heartbeat (gitignored)
│   ├── scanner.db                # Scanner candidates, raw alerts, cycles
│   ├── trades.db                 # Grader grades table
│   └── heartbeat.txt
├── src/
│   ├── shared/                   # Cross-agent code
│   │   ├── __init__.py
│   │   ├── models.py             # Candidate, SignalMatch
│   │   ├── db.py                 # SQLite connection + grades/scans/executions tables
│   │   └── config.py             # YAML loader + env injection
│   ├── scanner/
│   │   ├── __init__.py
│   │   ├── main.py               # Scanner loop
│   │   ├── run_pipeline.py       # Full pipeline: scanner + grader
│   │   ├── client/
│   │   │   ├── uw_client.py
│   │   │   └── rate_limiter.py
│   │   ├── models/
│   │   │   ├── flow_alert.py
│   │   │   ├── dark_pool.py
│   │   │   ├── market_tide.py
│   │   │   └── candidate.py      # Re-exports from shared
│   │   ├── rules/
│   │   │   ├── engine.py
│   │   │   ├── filters.py
│   │   │   └── confluence.py
│   │   ├── state/
│   │   │   ├── dedup.py
│   │   │   └── db.py             # Scanner-specific SQLite (candidates, raw_alerts)
│   │   ├── output/
│   │   │   ├── queue.py
│   │   │   └── notifier.py
│   │   └── utils/
│   │       ├── clock.py
│   │       └── logging.py
│   └── grader/
│       ├── __init__.py
│       ├── main.py               # Consumer loop: candidate_queue → scored_queue
│       ├── grader.py             # Orchestrator (context → LLM → parse → log)
│       ├── context_builder.py    # Enriches Candidate with quote, greeks, news, insider
│       ├── prompt.py             # System + user prompt templates
│       ├── llm_client.py         # Anthropic SDK wrapper
│       ├── parser.py             # JSON extract + GradeResponse validation
│       └── models.py             # GradingContext, GradeResponse, ScoredTrade
├── tests/
│   ├── conftest.py
│   ├── fixtures/
│   ├── test_client.py
│   ├── test_filters.py
│   ├── test_engine.py
│   ├── test_confluence.py
│   ├── test_dedup.py
│   ├── test_integration.py
│   ├── test_grader_models.py
│   ├── test_context_builder.py
│   ├── test_prompt.py
│   ├── test_llm_client.py
│   ├── test_parser.py
│   └── test_grader.py
├── scripts/
│   ├── backfill.py
│   └── replay.py
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

Defined in `shared/models.py`. The output model emitted from scanner to grader:

| Field | Description |
|---|---|
| `signals` | List of `SignalMatch` objects (rule name, weight, human-readable detail) |
| `confluence_score` | Weighted sum of all matched signals |
| `dark_pool_confirmation` | Whether a matching dark pool print was found |
| `market_tide_aligned` | Whether market sentiment agrees with the signal direction |
| `raw_alert_id` | Original UW API alert ID for traceability |

### ScoredTrade and GradeResponse

Defined in `grader/models.py`. A `ScoredTrade` is a candidate that passed grading (score ≥ threshold). It wraps the `Candidate`, a `GradeResponse` (score 1–100, verdict pass/fail, rationale, signals_confirmed), and metadata (model_used, latency_ms, token counts). In pass-through mode, `grade` may be `None`.

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

**Scanner DB** (`data/scanner.db`): `ScannerDB` stores candidates, raw alerts, and scan cycles.

**Grader DB** (`data/trades.db`): `shared.db.get_db()` creates the `grades` table. Every grading call writes a row (candidate_id, score, verdict, rationale, model, token counts, latency).

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

## Grader Configuration

The `grader` section in `config/rules.yaml` controls Agent B:

| Parameter | Default | Description |
|---|---|---|
| `score_threshold` | 70 | Minimum score to emit a ScoredTrade (1–100) |
| `model` | `claude-sonnet-4-20250514` | Claude model for grading |
| `max_tokens` | 512 | Max output tokens per LLM call |
| `timeout_seconds` | 15 | LLM request timeout |
| `max_parse_retries` | 1 | Retries on JSON parse failure |
| `enabled` | `true` | If `false`, pass-through mode (no LLM, grade=None) |

---

## Observability

Logs are emitted as structured JSON to **both** the terminal (stdout) and `scanner.json.log` in the project root. Each cycle produces a log line like:

```json
{
  "event": "cycle_complete",
  "cycle": 142,
  "alerts": 47,
  "new": 12,
  "candidates": 2,
  "dark_pool_confirmed": 1,
  "market_tide_aligned": 1,
  "dedup_cache_size": 89,
  "duration_ms": 1840,
  "timestamp": "2026-03-20T15:30:12Z"
}
```

Key metrics: alerts per cycle (connectivity), dedup hit rate (trade freshness), candidates per hour (rule tightness), `dark_pool_confirmed` / `market_tide_aligned` (confluence quality), and cycle duration (polling drift).

A heartbeat file at `data/heartbeat.txt` is updated every cycle with `datetime.utcnow().isoformat()`. Use it with cron or a process monitor to restart if the file goes stale (e.g. no update in 5 minutes).

---

## Getting Started (TL;DR)

```bash
cd whale-scanner
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,grader]"
cp .env.example .env   # Set UW_API_TOKEN and ANTHROPIC_API_KEY
python -m scanner.run_pipeline --force --max-cycles 3
```

See [Getting Started](#getting-started) for full instructions.

---

## Testing

Tests use `pytest` with `pytest-asyncio` for async support and `respx` for mocking HTTP calls.

```bash
# Run all tests (scanner + grader)
pytest -v

# Run grader tests only
pytest tests/test_grader.py tests/test_grader_models.py tests/test_context_builder.py tests/test_prompt.py tests/test_llm_client.py tests/test_parser.py -v
```

Ensure the venv is activated and the project is installed (`pip install -e ".[dev,grader]"`).

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
| `httpx` | Async HTTP client |
| `pydantic` | Data validation |
| `pyyaml` | Configuration parsing |
| `python-dotenv` | `.env` loading |
| `structlog` | Structured JSON logging |
| `aiosqlite` | Async SQLite |
| `asyncio-throttle` | Rate limiting |

Optional `grader` extra: `anthropic` (Claude SDK).

Dev: `pytest`, `pytest-asyncio`, `respx`, `ruff`, `mypy`.

---

## License

MIT
