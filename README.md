# Whale Scanner

A multi-gate pipeline for unusual options flow:

- **Agent A (Scanner)** — Deterministic rule engine that scans for unusual options flow during US market hours. Polls the [Unusual Whales](https://unusualwhales.com) API, applies configurable filters, scores candidates by multi-signal confluence, and pushes them to the grader.
- **Gate 1 (Flow Analyst)** — Deterministic post-scanner filter (no LLM, no external API calls). Scores each candidate 1–100 from the in-memory `Candidate` object only, logs every decision to SQLite, and discards candidates below threshold before any LLM tokens are spent.
- **Gate 2 (Volatility Analyst + Risk Analyst)** — Deterministic “is the buyer getting a good deal?” layer. The **Volatility Analyst** fetches 4 UW volatility/chain endpoints per candidate (no LLM), and the **Risk Analyst** scores structural conviction from buyer risk accepted (premium, DTE, spread, OTM distance, move ratio, liquidity, earnings proximity). Gate 2 passes when the average of (flow + vol + risk) meets the configured threshold.
- **Gate 3 (Sentiment + Insider + Sector + LLM layer)** — Runs **Sentiment Analyst**, **Insider Tracker**, and a placeholder **Sector Analyst** in parallel (each can emit a `SubScore`), then runs the main LLM grader to score conviction and emit passing trades. The Insider Tracker scores whether insiders and congressional holders align with the flow (UW + Finnhub; see [Insider Tracker (Gate 3)](#insider-tracker-gate-3)).

**Key features:** Confluence enrichment (dark pool + market tide), deterministic multi-gate filtering (Gate 1 + Gate 2) before any LLM spend, `--force` to bypass market hours, `--max-cycles` for limited runs, dual logging (terminal + `scanner.json.log`), grader pass-through mode (`enabled: false`), and audit logging to SQLite.

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
- [End-to-End Flow Summary](#end-to-end-flow-summary)
- [How the Scanner Works](#how-the-scanner-works)
- [Sector Benchmark Cache (Market/Sector Vol Benchmarks)](#sector-benchmark-cache-marketsector-vol-benchmarks)
- [How the Grader Works](#how-the-grader-works)
- [Sentiment Analyst (Gate 3)](#sentiment-analyst-gate-3)
- [Insider Tracker (Gate 3)](#insider-tracker-gate-3)
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
FINNHUB_API_KEY=your_finnhub_api_key_here
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
│  │         Agent A (Scanner)            │  │ Gate 1 (Flow Analyst)   │ │
│  │                                      │  │                         │ │
│  │  Market Clock → UW API → Dedup       │  │  Candidate Queue        │ │
│  │       → Rule Engine → Confluence     │──▶│       → Score + Log     │ │
│  │       → SQLite + Queue               │  │       → Pass/Reject     │ │
│  └─────────────────────────────────────┘  └───────────┬────────────┘ │
│                                                       │              │
│                                            ┌──────────▼───────────┐ │
│                                            │  Gate 2 (Deterministic)│ │
│                                            │  Vol + Risk → Pass/Reject│ │
│                                            └──────────┬───────────┘ │
│                                                       │              │
│                                            ┌──────────▼───────────┐ │
│                                            │   Agent B (Grader)    │ │
│                                            │ Context → Claude → DB  │ │
│                                            └──────────┬───────────┘ │
│                                                       │              │
│                                            Scored Queue → (Agent C)   │
└─────────────────────────────────────────────────────────────────────┘
```

The scanner runs as an async producer: every cycle it fetches flow alerts, dark pool prints, and market tide from the Unusual Whales API, deduplicates, runs the rule engine, enriches with confluence data, persists to SQLite, and pushes candidates into a shared queue. The flow analyst runs as a deterministic Gate 1 consumer: it converts the scanner `Candidate` into a normalized `FlowCandidate`, applies a deterministic scoring algorithm (no LLM, no external APIs), logs every decision to SQLite, and rejects candidates below threshold. Only then does the LLM grader run (when enabled) to spend tokens on the highest-quality subset. Shared code lives in `src/shared/` (models, config, db, filters).

---

## End-to-End Flow Summary

At a high level, the pipeline is:

1. **Scanner (Agent A)** polls the Unusual Whales API for raw flow and confluence signals, deduplicates, applies the rule engine, and emits `Candidate` objects.
2. **Sector Benchmark Cache** (daily-refresh, in-memory) fetches market/sector volatility benchmarks used for "cheap vs market/sector" context. This cache is designed to be warmed once per trading day and reused across all candidates graded that day.
3. **Filter agent (Gate 1 Flow Analyst)** deterministically scores each `Candidate` from in-memory fields only (no LLM, no external API calls) and rejects low-quality candidates before any token spend.
4. **Gate 2 (Volatility Analyst + Risk Analyst)** runs in parallel and determines whether the buyer is paying fair implied volatility relative to the ticker’s own history, sector peers, and the broader market. Gate 2 is deterministic and designed to be low-latency.
5. **Gate 3** runs three analysts in parallel: **Sentiment Analyst** (UW headlines + Finnhub buzz + Reddit), **Insider Tracker** (UW insider Form 4 / buy-sell / flow + congressional holders & trades + Finnhub insider transactions + optional MSPR), and a **Sector Analyst** placeholder (neutral skip until implemented). Insider Tracker skips the LLM with a neutral `SubScore` when there is no insider and no congressional signal; otherwise it calls Claude with a dedicated prompt (see [token budget](#insider-tracker-token-budget)).
6. **Grader (Agent B)** (when enabled) builds enriched context via UW endpoints, calls the main grader LLM, parses/validates output, audits to SQLite, and emits passing `ScoredTrade` results.

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

## Sector Benchmark Cache (Market/Sector Vol Benchmarks)

The **Sector Benchmark Cache** is a lightweight, **in-memory** data layer that fetches a small set of liquid benchmark tickers once per trading day (or on first use), computes per-sector IV/RV ratio percentiles, and exposes simple lookups for downstream scoring agents.

- **What it’s for**: letting an analyst answer “Is this option cheap vs the market and vs its sector?” by comparing a candidate’s implied/realized vol relationship to sector/market baselines.
- **What it is not**: not a database; not persisted across process restarts; intentionally small benchmark universe (~39 tickers including SPY).
- **Refresh policy**: cache is considered stale after **8 hours** and will auto-refresh when requested.

### Where it lives

- `src/grader/context/sector_cache.py`

### Public API

- `refresh_sector_cache(client, api_token) -> SectorBenchmarkCache`: fetch everything and compute benchmarks.
- `get_sector_cache(client, api_token, force_refresh=False) -> SectorBenchmarkCache`: cached accessor (refreshes if missing/stale/forced).
- `get_cached_benchmarks() -> SectorBenchmarkCache | None`: sync accessor (may be `None` before first refresh).

### Lookups

- `cache.get_sector(sector_name)`: exact match
- `cache.get_sector_fuzzy(sector_name)`: exact → case-insensitive → substring → fallback to `_all_sectors`

### Minimal usage example

```python
import httpx

from grader.context.sector_cache import get_sector_cache


async def example(api_token: str) -> None:
    async with httpx.AsyncClient() as client:
        cache = await get_sector_cache(client, api_token)
        tech = cache.get_sector_fuzzy("technology")
        market_rank = cache.market_iv_rank
```

### Notes on robustness

- Field extraction helpers handle **multiple possible UW response field names** and emit `structlog` warnings when using fallback keys.
- Partial per-ticker failures are **logged and excluded** from benchmarks.
- If the market proxy (`SPY`) fetch fails, market values default to neutral: `iv_rank=50.0`, `iv=0.20`, `ratio=1.0`.

---

## How the Grader Works

Before any LLM call, candidates pass through **Gate 1 (flow analyst)**:

1. **Gate 1 scoring (deterministic)** — Converts the scanner `Candidate` into `FlowCandidate` (`grader.agents.flow_analyst.candidate_to_flow`), checks ticker exclusion lists (ETFs/index/VIX products), scores trade mechanics 1–100 using constants in `shared.filters`, and logs every decision to the `flow_scores` table in `data/trades.db`. Candidates below `GATE_THRESHOLDS.flow_analyst_min` are discarded.

After Gate 1, Agent B (the grader) consumes the remaining candidates and runs them through:

2. **Gate 2 scoring (deterministic)** — Runs the volatility analyst (4 UW endpoints: IV rank, vol stats, term structure, option chains) and the risk analyst (conviction via structural risk accepted) in parallel, then checks the average of (flow + vol + risk) against `GATE_THRESHOLDS.gate2_avg_threshold`. The risk path fetches option chains, realized volatility, and earnings date context, then computes a pure deterministic score (no LLM, no API calls inside scoring).
3. **Gate 3 (parallel LLM / context analysts)** — **Sentiment Analyst** builds `SentimentContext` from UW/Finnhub/Reddit and calls the sentiment prompt. **Insider Tracker** builds `InsiderContext` (7 parallel data sources; see below) and either skips with a neutral score or calls Claude with `max_tokens=300` for that agent only. **Sector Analyst** is still a neutral placeholder. Failures in any Gate 3 agent are non-fatal and return neutral `SubScore(score=50, skipped=True)`.
4. **Context builder** — Fetches quote, greeks, news, insider/congressional trades from the Unusual Whales API (concurrent, with graceful degradation on partial failures).
5. **Prompt assembly** — Renders system + user prompts from `GradingContext`, including the `GradeResponse` JSON schema.
6. **LLM call (main grader)** — Sends to Claude (default: `claude-sonnet-4-20250514`) with the grader `max_tokens` (default 512).
7. **Parse & validate** — Strips markdown fences, extracts JSON, validates with Pydantic. On parse failure, retries once.
8. **Audit** — Writes every grading decision to the `grades` table in `data/trades.db`.
9. **Routing** — If score >= `score_threshold` (default 70), emits a `ScoredTrade` to the scored queue; otherwise returns `None`.

With `grader.enabled: false` in config, the pipeline applies **Gate 1 only** and forwards survivors as `ScoredTrade` with `grade=None` (no Gate 2, no Gate 3, no LLM calls).

---

## Sentiment Analyst (Gate 3)

The sentiment analyst is a crowd/noise filter, not a "bullish news" engine. Core thesis: **silence is signal**.

- No news + no Reddit mentions => neutral (~50), not negative.
- Catalyst in news + low social chatter => positive (informed flow ahead of crowd).
- Strong Reddit presence (especially `r/wallstreetbets` / `r/Shortsqueeze`) => negative (crowded edge).

### Data sources

- UW headlines: `/api/news/headlines?ticker={symbol}&limit=20`
- Finnhub buzz/sentiment: `/api/v1/news-sentiment?symbol={ticker}`
- Reddit public `.json` search for 7 trading subreddits:
  `wallstreetbets`, `options`, `stocks`, `investing`, `thetagang`, `Shortsqueeze`, `unusual_whales`

### Runtime behavior

- Builder file: `src/grader/context/sentiment_ctx.py`
- Agent file: `src/grader/agents/sentiment_analyst.py`
- Prompt/template: `src/grader/prompt.py`
- Model types: `src/grader/models.py` (`SentimentContext`, `SentimentGrade`, etc.)
- Gate 3 orchestrator: `src/grader/gate3.py`

Any source failure degrades gracefully to neutral defaults; sentiment never blocks the rest of the pipeline.

---

## Insider Tracker (Gate 3)

The **Insider Tracker** answers whether corporate insiders and congressional holders are aligned with the unusual options flow. It is LLM-powered (same Claude model family as the rest of the grader) and runs **in parallel** with the sentiment and sector agents (`src/grader/gate3.py`).

### Data sources (API call budget)

All sources are fetched concurrently (`asyncio.gather(return_exceptions=True)`); failures are logged and treated as empty data.

| Tier | Source | Endpoints / usage |
|------|--------|---------------------|
| UW insider | Primary | `GET /api/insider/{ticker}`, `GET /api/stock/{ticker}/insider-buy-sells`, `GET /api/insider/{ticker}/ticker-flow` |
| UW congressional | Primary | `GET /api/politician-portfolios/holders/{ticker}`, `GET /api/congress/recent-trades` (filtered to ticker client-side) |
| Finnhub | Cross-reference / MSPR | `GET /api/v1/stock/insider-transactions`, `GET /api/v1/stock/insider-sentiment` (optional; `401`/`403` → no MSPR) |

Implementation uses **`shared/finnhub_client.py`** (async HTTP to Finnhub REST) and **`src/grader/context/insider_ctx.py`** for normalization, deterministic **cluster detection** (2+ distinct insiders, same buy/sell direction, within `InsiderScoringConfig.cluster_window_days`), **UW vs Finnhub direction** cross-check, and **merge/dedup** of overlapping filings between UW and Finnhub.

### Skip behavior

If there is **no qualifying insider activity** in the configured lookback **and** no congressional holders or recent congressional trades in derived signals, the agent **does not call the LLM** and returns a neutral `SubScore` with `skipped=True`, `score=50`, and a short rationale (absence of data is not scored as bearish).

### Confidence adjustment

When fewer than `InsiderScoringConfig.min_sources_for_full_confidence` data sources are present, raw LLM scores are **compressed toward 50** so sparse-data extremes are not over-weighted.

### Configuration

Tuning lives in **`InsiderScoringConfig`** in `src/shared/filters.py` (cluster window, lookbacks, max rows in prompt, MSPR thresholds, etc.).

### Runtime behavior

- Context builder: `src/grader/context/insider_ctx.py`
- Agent: `src/grader/agents/insider_tracker.py`
- Prompts: `INSIDER_TRACKER_*` in `src/grader/prompt.py`
- Tests: `tests/test_insider_tracker.py`, fixtures in `tests/fixtures/insider_fixtures.py`

### Insider Tracker token budget

Approximate Claude usage **for this agent only** (per candidate that actually calls the LLM; skipped candidates use **no** insider LLM tokens):

| Component | Tokens (approx.) |
|-----------|------------------|
| System prompt | ~350 |
| User prompt (typical) | ~400–700 |
| User prompt (data-rich ticker) | ~1,000 |
| Response (`max_tokens=300`) | ~150–200 |
| **Total per graded candidate** | **~900–1,500** |

Rough cost order-of-magnitude (same model/pricing as the main grader): **~$0.002–0.004** per insider call when the pipeline is configured for Sonnet-class pricing; the user prompt is capped at **20** merged insider rows (`InsiderScoringConfig.max_transactions_in_prompt`) to keep context bounded.

---

## Running on Live Market Data

Ensure the venv is activated and API keys are set (see [Getting Started](#getting-started)).

### End-to-end: see volatility scoring results

Run a short forced pipeline, then inspect the structured logs for per-candidate volatility scoring:

```bash
# Run end-to-end for a few cycles (scanner -> Gate 1 -> Gate 2 -> Gate 3 sentiment -> grader)
python -m scanner.run_pipeline --force --max-cycles 3

# See volatility analyst score summaries (structlog event: vol_analyst.scored)
jq -c 'select(.event == "vol_analyst.scored") | {ticker, score, abs_score, hist_score, mkt_score, signal_count}' scanner.json.log

# See Gate 3 sentiment summaries
jq -c 'select(.event == "gate3.result") | {ticker, scores}' scanner.json.log
```

Notes:

- Gate 2 uses the **Sector Benchmark Cache** (SPY + per-sector benchmark tickers). The cache auto-refreshes when stale.
- If the volatility analyst can’t fetch required UW data for a candidate, it returns a neutral `SubScore(score=50, skipped=True)` with a skip reason, and the pipeline continues.

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
| Grader DB | `data/trades.db` | Gate 1 flow scores (`flow_scores`) + LLM grades (`grades`) |
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
├── .env.example                  # Template for secrets (UW_API_TOKEN, ANTHROPIC_API_KEY, FINNHUB_API_KEY)
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
│   │   ├── filters.py            # Gate thresholds, InsiderScoringConfig, ticker exclusions
│   │   ├── models.py             # Candidate, SignalMatch, FlowCandidate, SubScore
│   │   ├── finnhub_client.py     # Async Finnhub REST (insider transactions + MSPR)
│   │   ├── db.py                 # SQLite connection + grades/scans/executions/flow_scores tables
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
│       ├── gate1.py              # Gate 1: deterministic flow analyst + SQLite logging
│       ├── gate2.py              # Gate 2: deterministic volatility + risk (parallel) + threshold
│       ├── gate3.py              # Gate 3: sentiment + insider + sector SubScores in parallel
│       ├── grader.py             # Orchestrator (context → LLM → parse → log)
│       ├── context/
│       │   ├── __init__.py
│       │   ├── sector_cache.py   # Daily-refresh market/sector vol benchmarks (in-memory)
│       │   ├── vol_ctx.py        # Normalized volatility context builder for scoring
│       │   ├── risk_ctx.py       # Risk analyst context fetcher (option chains, vol stats, earnings)
│       │   ├── sentiment_ctx.py  # Gate 3 sentiment context (UW + Finnhub + Reddit)
│       │   └── insider_ctx.py    # Gate 3 insider + congressional + Finnhub context
│       ├── context_builder.py    # Enriches Candidate with quote, greeks, news, insider
│       ├── agents/
│       │   ├── __init__.py
│       │   ├── flow_analyst.py        # Deterministic Gate 1 scoring engine
│       │   ├── volatility_analyst.py  # Deterministic Gate 2 volatility scorer (4 UW endpoints)
│       │   ├── risk_analyst.py        # Deterministic Gate 2 risk conviction scorer
│       │   ├── sentiment_analyst.py   # Gate 3 LLM sentiment / crowding
│       │   └── insider_tracker.py     # Gate 3 LLM insider + congressional alignment
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
│   ├── test_flow_analyst.py
│   ├── test_vol_analyst.py
│   ├── test_risk_analyst.py
│   ├── test_sentiment_analyst.py
│   ├── test_sentiment_ctx.py
│   ├── test_insider_tracker.py
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

**Grader DB** (`data/trades.db`): `shared.db.get_db()` creates:

- `flow_scores` — every Gate 1 decision (candidate_id, score, skipped/skip_reason, rationale, signals, scored_at)
- `grades` — every LLM grading decision (candidate_id, score, verdict, rationale, model, token counts, latency)

---

## Configuration Reference

Scanner rule thresholds live in `config/rules.yaml`. Deterministic grading tunables (ticker exclusions, Gate 1 thresholds, and scoring weights) live in `src/shared/filters.py` as the single source of truth for the grading pipeline.

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
python -m pytest -v

# Run grader tests only
python -m pytest tests/test_grader.py tests/test_grader_models.py tests/test_context_builder.py tests/test_prompt.py tests/test_llm_client.py tests/test_parser.py -v

# Run the new deterministic risk analyst suite
python -m pytest tests/test_risk_analyst.py -v --tb=short
```

Ensure the venv is activated and the project is installed (`pip install -e ".[dev,grader]"`).

Sector benchmark cache tests live in `tests/test_sector_cache.py`.
Volatility analyst tests live in `tests/test_vol_analyst.py`.
Risk analyst tests live in `tests/test_risk_analyst.py`.

If your default `python` is not 3.11+, run tests with `python3.11 -m pytest ...` (project requires Python 3.11+).

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
