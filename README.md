# Whale Scanner

A multi-gate pipeline for unusual options flow:

- **Agent A (Scanner)** — Deterministic rule engine that scans for unusual options flow during US market hours. Polls the [Unusual Whales](https://unusualwhales.com) API, applies configurable filters, scores candidates by multi-signal confluence, and pushes them to the grader.
- **Gate 0 (Ticker universe)** — Hard pre-filter before Gate 1: static block lists (mega-caps, meme names, China ADRs, plus the same ETF/index exclusions used downstream) and one cached UW `/api/stock/{ticker}/info` call for market cap ($250M–$20B) and `issue_type` (common stock only). API failures **fail open**; static matches **fail closed**. Optional allow list via `GATE0_ALLOW_LIST` (see [Gate 0 and the interesting universe](#gate-0-and-the-interesting-universe)).
- **Gate 1 (Flow Analyst)** — Deterministic post-scanner filter (no LLM, no external API calls). Scores each candidate 1–100 from the in-memory `Candidate` object only, logs every decision to SQLite, and discards candidates below threshold before any LLM tokens are spent.
- **Gate 2 (Volatility Analyst + Risk Analyst)** — Deterministic “is the buyer getting a good deal?” layer. The **Volatility Analyst** fetches 4 UW volatility/chain endpoints per candidate (no LLM), and the **Risk Analyst** scores structural conviction from buyer risk accepted (premium, DTE, spread, OTM distance, move ratio, liquidity, earnings proximity). Gate 2 passes when the average of (flow + vol + risk) meets the configured threshold.
- **Gate 3 (Specialists + synthesis)** — Runs **Sentiment Analyst**, **Insider Tracker**, and a deterministic **Sector Analyst** in parallel (each emits a `SubScore`). Those scores are merged with Gate 1–2 sub-scores into a **deterministic aggregator** (weighted average, disagreement, six conflict detectors). A final **Synthesis** step makes **one** Claude call to produce the 1–100 score, applies deterministic caps, merges position sizing with the risk analyst, and emits a passing `ScoredTrade` if the score meets the threshold. See [Synthesis layer (Gate 3)](#synthesis-layer-gate-3) and the specialist sections below.

**Key features:** Confluence enrichment (dark pool + market tide), **Gate 0** universe filter (static lists + cached UW stock info), deterministic Gates 1–2 before most LLM spend, Gate 3 uses three specialist calls plus one synthesis call (not the legacy single-shot context grader in production), optional `GATE0_ALLOW_LIST` for a focused ticker set, `--force` to bypass market hours, `--max-cycles` for limited runs, dual logging (terminal + `scanner.json.log`), grader pass-through mode (`grader.enabled: false`), audit logging to SQLite (`flow_scores` + `grades`), and a **signal tracker** package (`src/tracker/`) with YAML-backed `TrackerConfig`, Pydantic models (`Signal`, snapshots, chain/flow result types), and `SignalStore` persistence on **`signals`** / **`signal_snapshots`** in `data/trades.db` (see [Signal Tracker](#signal-tracker)).

### Quick run (full pipeline)

```bash
cd whale-scanner
source .venv/bin/activate          # or: .venv\Scripts\activate on Windows
pip install -e ".[dev,grader]"     # once per venv

# Option A — module (recommended in docs)
python -m scanner.run_pipeline --force --max-cycles 3

# Option B — console script (same entrypoint as run_pipeline)
whale-pipeline --force --max-cycles 3
```

Scanner-only (no grader): `python -m scanner.main --force --max-cycles 3` or `whale-scanner --force --max-cycles 3`.

See [Getting Started](#getting-started) for venv setup, `.env`, and all run modes.

---

## Table of Contents

- [Getting Started](#getting-started)
- [Architecture Overview](#architecture-overview)
- [End-to-End Flow Summary](#end-to-end-flow-summary)
- [How the Scanner Works](#how-the-scanner-works)
- [Sector Benchmark Cache (Market/Sector Vol Benchmarks)](#sector-benchmark-cache-marketsector-vol-benchmarks)
- [Gate 0 and the interesting universe](#gate-0-and-the-interesting-universe)
- [How the Grader Works](#how-the-grader-works)
- [Synthesis layer (Gate 3)](#synthesis-layer-gate-3)
- [Sentiment Analyst (Gate 3)](#sentiment-analyst-gate-3)
- [Insider Tracker (Gate 3)](#insider-tracker-gate-3)
- [Sector Analyst (Gate 3)](#sector-analyst-gate-3)
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
- [Signal Tracker](#signal-tracker)
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

# Optional: restrict Gate 0 to these tickers (comma-separated). ETFs in EXCLUDED_TICKERS still blocked.
# GATE0_ALLOW_LIST=ACME,XYZ
```

### Step 4: Run the pipeline

From the repo root, with the venv activated:

```bash
# Full pipeline (scanner + grader) — test run, 3 cycles
python -m scanner.run_pipeline --force --max-cycles 3
# equivalent:
whale-pipeline --force --max-cycles 3

# Full pipeline — live during market hours (runs until Ctrl+C)
python -m scanner.run_pipeline
whale-pipeline

# Scanner only (no Gate 2/3, no grading)
python -m scanner.main --force --max-cycles 3
whale-scanner --force --max-cycles 3
```

| Entry | What it runs |
|-------|----------------|
| `scanner.run_pipeline` / `whale-pipeline` | `run_scanner` ∥ `run_grader` (full queue pipeline) |
| `scanner.main` / `whale-scanner` | Scanner loop only (no consumer grading) |

### Stop a running pipeline

Press **`Ctrl+C`** in the terminal to interrupt the process.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                     Full Pipeline (run_pipeline)                      │
│                                                                       │
│  ┌─────────────────────────────────────┐  ┌────────────────────────┐ │
│  │         Agent A (Scanner)            │  │ Gate 0 → Gate 1         │ │
│  │                                      │  │                         │ │
│  │  Market Clock → UW API → Dedup       │  │  Queue → Universe +     │ │
│  │       → Rule Engine → Confluence     │──▶│  flow score + log       │ │
│  │       → SQLite + Queue               │  │       → Pass/Reject     │ │
│  └─────────────────────────────────────┘  └───────────┬────────────┘ │
│                                                       │              │
│                                            ┌──────────▼───────────┐ │
│                                            │  Gate 2 (Deterministic)│ │
│                                            │  Vol + Risk → Pass/Reject│ │
│                                            └──────────┬───────────┘ │
│                                                       │              │
│                                            ┌──────────▼──────────────┐ │
│                                            │ Gate 3 specialists     │ │
│                                            │ Sentiment ∥ Insider ∥   │ │
│                                            │ Sector (→ SubScores)    │ │
│                                            └──────────┬──────────────┘ │
│                                                       │                │
│                                            ┌──────────▼──────────────┐ │
│                                            │ Aggregator (deterministic)│ │
│                                            │ weights · stdev · conflicts│ │
│                                            └──────────┬──────────────┘ │
│                                                       │                │
│                                            ┌──────────▼──────────────┐ │
│                                            │ Synthesis (1× Claude)   │ │
│                                            │ caps · verdict · risk   │ │
│                                            └──────────┬──────────────┘ │
│                                                       │                │
│                                            Scored Queue → (Agent C)     │
└───────────────────────────────────────────────────────────────────────┘
```

The scanner runs as an async producer: every cycle it fetches flow alerts, dark pool prints, and market tide from the Unusual Whales API, deduplicates, runs the rule engine, enriches with confluence data, persists to SQLite, and pushes candidates into a shared queue. The grader consumer runs **Gate 0** (universe filter), then **Gates 1–3**: deterministic flow, then vol + risk, then parallel specialists, deterministic aggregation, and a single synthesis LLM call (when grading is enabled). Shared code lives in `src/shared/` (models, config, db, filters).

A **signal tracker** peer module in `src/tracker/` is designed to turn passing `ScoredTrade` outputs into long-lived **`Signal`** rows, poll option chains and flow over time, and move conviction through states (`pending` → `accumulating` → `actionable`, or terminal `expired` / `decayed` / `executed`). The **models, configuration loader, SQLite schema, and `SignalStore`** are implemented; pipeline wiring in `run_pipeline` (intake task + monitor loop) is optional follow-up work on top of this layer.

---

## End-to-End Flow Summary

At a high level, the pipeline is:

1. **Scanner (Agent A)** polls the Unusual Whales API for raw flow and confluence signals, deduplicates, applies the rule engine, and emits `Candidate` objects.
2. **Sector Benchmark Cache** (daily-refresh, in-memory) fetches market/sector volatility benchmarks used for "cheap vs market/sector" context. This cache is designed to be warmed once per trading day and reused across all candidates graded that day.
3. **Gate 0 (universe filter)** runs first in the grader: static lists plus optional UW stock info for market cap and issue type (see [Gate 0 and the interesting universe](#gate-0-and-the-interesting-universe)).
4. **Filter agent (Gate 1 Flow Analyst)** deterministically scores each `Candidate` from in-memory fields only (no LLM, no external API calls) and rejects low-quality candidates before any token spend.
5. **Gate 2 (Volatility Analyst + Risk Analyst)** runs in parallel and determines whether the buyer is paying fair implied volatility relative to the ticker’s own history, sector peers, and the broader market. Gate 2 is deterministic and designed to be low-latency.
6. **Gate 3** runs three specialists in parallel (**Sentiment**, **Insider**, **Sector**), each producing a `SubScore` (failures become skipped neutral scores so the pipeline continues). Insider Tracker may skip its LLM when there is no qualifying data (see [Insider Tracker (Gate 3)](#insider-tracker-gate-3)).
7. **Aggregation + synthesis** (when `grader.enabled` is true) merges all six sub-scores (flow, vol, risk, sentiment, insider, sector), computes a renormalized weighted average, population stdev, agreement label, and conflict flags (`grader.aggregator`). **Synthesis** (`grader.synthesis`) builds a dedicated prompt (`grader.synthesis_prompt`), calls Claude once, parses JSON, applies deterministic score caps, sets verdict from the final score (≥70 pass), sets `TradeRiskParams.recommended_position_size` to `min(LLM modifier, risk analyst size)`, logs every outcome to `grades` in `data/trades.db`, and enqueues a `ScoredTrade` only if the final score ≥ `grader.score_threshold`.
8. **Signal tracker (optional path)** — High-scoring trades can be materialized as **`Signal`** records in SQLite and monitored over days via **`SignalSnapshot`** history, using thresholds and scoring weights from the **`tracker:`** section in `config/rules.yaml` (`load_tracker_config` in `tracker.config`). The persistence API is **`tracker.signal_store.SignalStore`**; chain polling, flow watching, and conviction scoring consume the Pydantic types in `tracker.models`.
9. **Legacy `Grader` class** (`grader.grader`) — older single-shot “context builder → one LLM” path kept for unit tests; **production** `grader.main` uses `run_gate3` + `SynthesisAgent` instead.

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

## Gate 0 and the interesting universe

**Interesting universe** means: **US common stocks** where unusual options flow is more likely to reflect **stock-specific information** than **index/ETF hedging**, **mega-cap noise**, **retail/meme crowding**, or **China ADR / non-equity structures**. Concretely, after static lists, a symbol must show UW `issue_type` **Common Stock** and **market capitalization between $250M and $20B** (inclusive). Below the floor, listed options are often too thin to trade; above the ceiling, flow is dominated by hedging and macro. Symbols on hard-coded mega-cap, meme, and China ADR lists are dropped without relying on the API.

**Allow list (`GATE0_ALLOW_LIST`)** — Comma-separated tickers in the environment (e.g. `ACME,BRK.B`). When set, **only** those symbols are eligible for Gate 0's **dynamic** checks (market cap and issue type). They are **not** exempt from `EXCLUDED_TICKERS`: e.g. `SPY` in the allow list is still blocked as an ETF. You can also set `shared.filters.ALLOW_LIST` in code or tests. Empty allow list = no extra restriction (default).

Implementation: `src/grader/gate0.py` (`run_gate0`), lists and `is_universe_blocked()` in `src/shared/filters.py`. Structured logs: `gate0.*`, `pipeline.gate0_reject`.

---

## How the Grader Works

The grader runs as `grader.main.run_grader`: it drains the same `asyncio.Queue` the scanner fills. When `grader.enabled` is **true** (default), each candidate goes through **Gate 0** → Gates 1 → 2 → 3 (specialists + synthesis). When **false**, **Gate 0 and Gate 1** still run; survivors are forwarded with `grade=None` (pass-through).

1. **Gate 0 (universe filter)** — Static block lists (`is_universe_blocked`) then optional cached `GET /api/stock/{ticker}/info` for market cap and issue type. UW errors → **fail open** (candidate continues); static hits → **fail closed**.

2. **Gate 1 (flow analyst, deterministic)** — Converts `Candidate` → `FlowCandidate`, applies exclusions and flow scoring from `shared.filters`, logs to `flow_scores`. Below `GATE_THRESHOLDS.flow_analyst_min` → discard.

3. **Gate 2 (volatility + risk, deterministic)** — Volatility analyst (UW vol/chain context) and **RiskConvictionScore** from the risk analyst run in parallel. Short-circuit if untradeable / zero position size. Otherwise pass if mean(flow, vol, risk) ≥ `GATE_THRESHOLDS.deterministic_avg_min` (same spirit as `gate2_avg_threshold` in config comments).

4. **Gate 3 (`run_gate3` in `grader.gate3`)** — Runs sentiment, insider, and sector `score()` coroutines in parallel. Exceptions → skipped `SubScore(score=50)`. Builds the six-agent map, runs **`Aggregator`**, then **`SynthesisAgent.synthesize`**. Successful synthesis always writes a row to **`grades`**; a **`ScoredTrade`** is pushed to the scored queue only if final score ≥ `grader.score_threshold`.

5. **Legacy path** — `grader.grader.Grader` + `context_builder` + `build_user_prompt`/`parse_grade_response` remains for tests; it is **not** used by `run_grader` today.

With `grader.enabled: false`, the pipeline applies **Gate 0 + Gate 1** and forwards survivors as `ScoredTrade` with `grade=None` and `risk=None` (no Gate 2, Gate 3, or LLM calls).

---

## Synthesis layer (Gate 3)

The synthesis step is the **last** graded stage: one structured Claude call that turns six `SubScore` rows plus deterministic aggregate metadata into a final **1–100** score and execution hints.

### Data flow

1. **Inputs** — `dict[str, SubScore]` for `flow_analyst`, `volatility_analyst`, `risk_analyst`, `sentiment_analyst`, `insider_tracker`, `sector_analyst`. Skipped agents are excluded from the weighted average; weights are **renormalized** over active agents (`AgentWeights` in `shared.filters`).
2. **Aggregator** (`src/grader/aggregator.py`) — Computes `weighted_average`, population **stdev** of active scores, `agent_agreement` (`strong` if stdev &lt; 10, `moderate` if ≤ 20, else `weak`), and **conflict flags** (e.g. high flow + low risk, sentiment vs flow, insider vs flow, sector headwind, vol+risk both low, unanimous high conviction). Extracts **`RiskConvictionScore`** when present for prompt risk fields and position sizing.
3. **Prompt** (`src/grader/synthesis_prompt.py`) — Fixed system prompt (bands, rules, JSON shape) + per-candidate user block: candidate fields, each agent’s score/rationale/top signals, aggregation lines, risk parameters, conflicts, skipped agents.
4. **LLM** — `SynthesisAgent` uses `LLMClient.complete` with `grader.model`, `grader.max_tokens`, `grader.timeout_seconds` from `config/rules.yaml`.
5. **Post-processing** (`src/grader/synthesis.py`) — Parse JSON (markdown fences tolerated via `grader.parser._extract_json`). **Deterministic caps:** e.g. flow ≥75 with risk &lt;40 → score capped at 65; vol &lt;40 and risk &lt;40 → cap 65; two or more non-skipped agents with score &lt;35 → cap 55; then clamp 1–100. **Verdict** is always derived from the capped score (≥70 `pass`). **`recommended_position_size`** = `min(position_size_modifier from LLM, risk analyst size)` into `TradeRiskParams` (stop/spread copied from risk analyst when available). Retries on parse failure: **`max_parse_retries` + 1** attempts total.

### Approximate cost / latency

Roughly **one** medium-sized prompt + **~150–250** tokens JSON out per candidate that reaches synthesis (plus three specialist calls earlier in Gate 3). Order-of-magnitude: **~$0.003** and **~0.5–1s** for synthesis alone, depending on model pricing and network (not a guarantee).

### Key files

| File | Role |
|------|------|
| `src/grader/aggregator.py` | `Aggregator`, `AggregatedResult`, conflict detectors |
| `src/grader/synthesis_prompt.py` | System + user prompt for synthesis |
| `src/grader/synthesis.py` | `SynthesisAgent`, caps, `log_synthesis_grade`, `SynthesisParseError` |
| `src/grader/gate3.py` | `run_gate3` — specialists ∥ → aggregate → synthesize → threshold |
| `tests/test_synthesis.py` | Unit/integration coverage for aggregator, prompts, synthesis, gate3 |

Structured log events to watch: `gate3.llm_agents.start` / `gate3.llm_agents.complete`, `gate3.aggregated`, `synthesis.complete`, `synthesis.score_capped`, `synthesis.parse_retry`, `gate3.passed`, `gate3.filtered`, `gate3.synthesis_failed`.

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
- Gate 3: specialists are invoked from `run_gate3` in `src/grader/gate3.py` (aggregation + synthesis in the same module’s pipeline)

Any source failure degrades gracefully to neutral defaults; sentiment never blocks the rest of the pipeline.

---

## Insider Tracker (Gate 3)

The **Insider Tracker** answers whether corporate insiders and congressional holders are aligned with the unusual options flow. It is LLM-powered (same Claude model family as the rest of the grader) and runs **in parallel** with the sentiment and sector agents inside `run_gate3` (`src/grader/gate3.py`).

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

## Sector Analyst (Gate 3)

The **Sector Analyst** is a **fully deterministic** Gate 3 agent (no LLM). It answers whether macro and sector option-flow context supports the trade: **sector option tide** (call/put ratio and net premium direction), **broad market tide**, a small **economic calendar** modifier for imminent high-impact releases, and **sector ETF** same-day performance. For **Healthcare / Health Care** tickers it also fetches the **FDA calendar** and surfaces upcoming PDUFA/ADCOM-style dates as **signals only** — these never change the numeric score (see `tests/test_sector_analyst.py::TestFDAFlag::test_fda_flag_does_not_change_score`).

### Scoring weights (defaults)

| Component | Weight |
|-----------|--------|
| Sector tide (+ ETF 1d modifier) | 0.50 |
| Market tide | 0.35 |
| Economic calendar | 0.15 |

Baseline score is **50**, then a weighted sum of raw point deltas is applied and the result is clamped to **1–100**. Thresholds and point tables live in **`SectorScoringConfig`** (`src/grader/agents/sector_scoring_config.py`, singleton `SECTOR_SCORING`).

### UW API usage

Context is built by **`build_sector_context`** in `src/grader/context/sector_ctx.py`: `GET /api/stock/{ticker}/info` (if sector is not pre-supplied), then `GET /api/market/{sector}/sector-tide`, `GET /api/market/market-tide`, `GET /api/market/economic-calendar`, `GET /api/market/sector-etfs`, and `GET /api/market/fda-calendar` **only** for healthcare-sector names. Fetches run in parallel with `asyncio.gather(..., return_exceptions=True)`; partial failures are logged and recorded in `SectorContext.fetch_errors` without crashing.

### Implementation map

- Context: `src/grader/context/sector_ctx.py` (`SectorContext`, parsers, `build_sector_context`)
- Scoring config: `src/grader/agents/sector_scoring_config.py`
- Engine: `src/grader/agents/sector_analyst.py` (`score_sector`, `SectorAnalyst`)
- Gate 3 wiring: `src/grader/gate3.py`, `src/grader/main.py`
- Tests: `tests/test_sector_analyst.py`

---

## Running on Live Market Data

Ensure the venv is activated and API keys are set (see [Getting Started](#getting-started)).

### End-to-end: see volatility scoring results

Run a short forced pipeline, then inspect the structured logs for per-candidate volatility scoring:

```bash
# Run end-to-end for a few cycles (scanner -> Gate 1 -> Gate 2 -> Gate 3 -> synthesis)
python -m scanner.run_pipeline --force --max-cycles 3
# or: whale-pipeline --force --max-cycles 3

# Volatility analyst score summaries (structlog event: vol_analyst.scored)
jq -c 'select(.event == "vol_analyst.scored") | {ticker, score, abs_score, hist_score, mkt_score, signal_count}' scanner.json.log

# Gate 3: after specialists + aggregate + synthesis
jq -c 'select(.event == "gate3.aggregated") | {ticker, weighted_avg, stdev, agreement, conflicts}' scanner.json.log
jq -c 'select(.event == "synthesis.complete") | {ticker, score, verdict, key_signal}' scanner.json.log
jq -c 'select(.event == "gate3.passed" or .event == "gate3.filtered") | {event, ticker, score, threshold}' scanner.json.log
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
whale-pipeline --force --max-cycles 5

# Full pipeline — live during market hours
python -m scanner.run_pipeline
whale-pipeline

# Scanner only (no grader)
python -m scanner.main --force --max-cycles 5
whale-scanner --force --max-cycles 5
```

### Output Locations

| Output | Location | Description |
|--------|----------|-------------|
| Scanner DB | `data/scanner.db` | Candidates, raw alerts, scan cycles |
| Grader DB | `data/trades.db` | Gate 1 flow scores (`flow_scores`), LLM grades (`grades`), and signal tracker tables (`signals`, `signal_snapshots`) |
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
│   └── rules.yaml                # Scanner + grader + `tracker` config — single source of truth
├── data/                         # Runtime: SQLite, heartbeat (gitignored)
│   ├── scanner.db                # Scanner candidates, raw alerts, cycles
│   ├── trades.db                 # Grader grades + signal tracker (`signals`, `signal_snapshots`)
│   └── heartbeat.txt
├── src/
│   ├── shared/                   # Cross-agent code
│   │   ├── __init__.py
│   │   ├── filters.py            # Gate thresholds, AgentWeights, InsiderScoringConfig, flow/vol/risk configs
│   │   ├── models.py             # Candidate, SignalMatch, FlowCandidate, SubScore, RiskConvictionScore
│   │   ├── finnhub_client.py     # Async Finnhub REST (insider transactions + MSPR)
│   │   ├── db.py                 # SQLite connection + grades/scans/executions/flow_scores/signals tables
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
│   ├── tracker/                  # Signal tracker — post-grade monitoring (models, config, SQLite)
│   │   ├── __init__.py
│   │   ├── config.py             # TrackerConfig + load_tracker_config (rules.yaml `tracker:`)
│   │   ├── models.py             # Signal, SignalSnapshot, ChainPollResult, FlowWatchResult, etc.
│   │   └── signal_store.py       # SignalStore CRUD for signals + snapshots
│   └── grader/
│       ├── __init__.py
│       ├── main.py               # Consumer loop: candidate_queue → scored_queue
│       ├── gate0.py              # Gate 0: ticker universe (static lists + UW stock info)
│       ├── gate1.py              # Gate 1: deterministic flow analyst + SQLite logging
│       ├── gate2.py              # Gate 2: deterministic volatility + risk (parallel) + threshold
│       ├── gate3.py              # Gate 3: specialists ∥ → Aggregator → SynthesisAgent → threshold
│       ├── aggregator.py         # Deterministic merge of six SubScores + conflicts
│       ├── synthesis.py          # Final Claude call, caps, TradeRiskParams, grade logging
│       ├── synthesis_prompt.py   # System + user prompts for synthesis
│       ├── grader.py             # Legacy single-shot grader (tests; not used by run_grader)
│       ├── context/
│       │   ├── __init__.py
│       │   ├── sector_cache.py   # Daily-refresh market/sector vol benchmarks (in-memory)
│       │   ├── sector_ctx.py     # Gate 3 sector analyst UW context (tide, econ, FDA)
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
│       │   ├── insider_tracker.py     # Gate 3 LLM insider + congressional alignment
│       │   ├── sector_scoring_config.py  # Deterministic sector analyst thresholds
│       │   └── sector_analyst.py      # Gate 3 deterministic sector / macro scorer
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
│   ├── test_sector_analyst.py
│   ├── test_grader.py
│   ├── test_synthesis.py         # Aggregator, synthesis prompts, SynthesisAgent, run_gate3
│   ├── test_tracker_models.py    # Tracker config + SignalState / Signal helpers
│   └── test_signal_store.py      # SignalStore SQLite (uses temp DB via fixture)
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

### ScoredTrade, GradeResponse, and TradeRiskParams

Defined in `grader/models.py`. A **`ScoredTrade`** is emitted when the final synthesis score ≥ `grader.score_threshold` (or in pass-through mode when Gate 0 + Gate 1 run). It includes:

- **`Candidate`** — original scanner payload.
- **`grade`** — `GradeResponse`: score 1–100, `verdict` (`pass` if score ≥ 70 after caps), `rationale`, `signals_confirmed`, optional synthesis fields (`confidence`, `conflict_resolution`, `key_signal`, `position_size_modifier`). In pass-through mode, `grade` is `None`.
- **`risk`** — `TradeRiskParams` when synthesis ran: `recommended_position_size` (capped by risk analyst), `recommended_stop_loss_pct`, `max_entry_spread_pct` (from risk analyst). `None` in pass-through.
- **Metadata** — `model_used`, `latency_ms`, `input_tokens`, `output_tokens`, `graded_at`.

### DarkPoolPrint and MarketTide

Supporting models for cross-signal confluence. `MarketTide` exposes a `direction` property (`"bullish"`, `"bearish"`, or `"neutral"`) derived from net call/put premium ratios.

### Signal tracker (`tracker.models`)

Defined in `src/tracker/models.py`. Core types:

- **`Signal`** — Persistent tracked contract: ticker/strike/expiry/option side, `SignalState` (`pending`, `accumulating`, `actionable`, `executed`, `expired`, `decayed`), grading provenance (`grade_id`, `initial_score`, OI/volume/premium baselines), rolling conviction fields (`conviction_score`, `confirming_flows`, `oi_high_water`, etc.), and optional `risk_params_json` / `anomaly_fingerprint` for downstream execution.
- **`SignalSnapshot`** — One row per poll cycle: contract quotes/OI, neighborhood aggregates, new-flow counts/premium, and conviction engine output (`conviction_delta`, `conviction_after`, `signals_fired`).
- **`ChainPollResult`**, **`FlowWatchResult`**, **`NeighborStrike`**, **`AdjacentExpiryOI`**, **`FlowEvent`** — Structured outputs for chain polling and flow watching (used by the conviction layer once wired).

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
- `grades` — every synthesis outcome (and legacy grader runs in tests): candidate_id, score, verdict, rationale, model, token counts, latency
- `signals` — one row per tracked anomaly (contract, conviction, flow/OI aggregates, lifecycle state); `grade_id` references `grades(id)`
- `signal_snapshots` — time-series observations per signal (contract/neighborhood metrics, flow deltas, conviction delta/after, `signals_fired` JSON)

---

## Configuration Reference

Scanner rule thresholds live in `config/rules.yaml`. Deterministic grading tunables (ticker exclusions, Gate 0 block lists and `UniverseConfig`, Gate 1 thresholds, and scoring weights) live in `src/shared/filters.py` as the single source of truth for the grading pipeline. Optional environment variable **`GATE0_ALLOW_LIST`** (comma-separated tickers) seeds `ALLOW_LIST` at import for a restricted universe.

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
| `max_parse_retries` | 1 | Synthesis only: extra attempts after a bad JSON response (**total** attempts = `max_parse_retries + 1`) |
| `enabled` | `true` | If `false`, pass-through mode (no LLM, grade=None) |

---

## Signal Tracker

The **signal tracker** is a peer package under `src/tracker/` for **stateful monitoring** of candidates that have already cleared the grading pipeline. Instead of treating a high synthesis score as a one-shot go/no-go, the design stores a **`Signal`**, records periodic **`SignalSnapshot`** rows, and (when the full loop is connected) refreshes conviction from option-chain and unusual-flow evidence against tunables in YAML.

### What is implemented today

| Piece | Location | Role |
|-------|----------|------|
| Configuration | `config/rules.yaml` → `tracker:` | Polling cadence, monitoring window, capacity caps, actionable/decay thresholds, neighbor radii, per-cycle scoring weights |
| Typed config | `tracker.config` | `TrackerConfig`, `ConvictionScoringConfig`, `load_tracker_config(dict)` |
| Domain models | `tracker.models` | `Signal`, `SignalSnapshot`, enums/constants, plus chain/flow DTOs for upcoming poller + engine |
| Persistence | `tracker.signal_store.SignalStore` | Async SQLite CRUD: create/update signals, append snapshots, list actives, duplicate check |
| Schema | `shared.db._ensure_tables` | `signals` + `signal_snapshots` (+ indexes) alongside existing grader tables |

Load the tracker section after parsing YAML (same `load_config()` flow as the rest of the app): pass the top-level dict into `load_tracker_config`.

### Integration note

**`scanner.run_pipeline`** does not yet spawn signal-intake or monitor tasks; grader behavior is unchanged. Wiring scored-queue consumption and the poll loop is a separate step on top of this storage/API layer.

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
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev,grader]"
cp .env.example .env        # Set UW_API_TOKEN, ANTHROPIC_API_KEY, FINNHUB_API_KEY (for sentiment/insider)
python -m scanner.run_pipeline --force --max-cycles 3
# or: whale-pipeline --force --max-cycles 3
```

See [Getting Started](#getting-started) for full instructions.

---

## Testing

Tests use `pytest` with `pytest-asyncio` for async support and `respx` for mocking HTTP calls.

```bash
# Run all tests (scanner + grader + synthesis)
python -m pytest tests/ -v

# Synthesis pipeline (aggregator, prompts, SynthesisAgent, run_gate3)
python -m pytest tests/test_synthesis.py -v --tb=short

# Grader unit tests (legacy Grader class + integration)
python -m pytest tests/test_grader.py tests/test_grader_models.py tests/test_context_builder.py tests/test_prompt.py tests/test_llm_client.py tests/test_parser.py -v

# Deterministic risk analyst suite
python -m pytest tests/test_risk_analyst.py -v --tb=short

# Gate 0 universe filter
python -m pytest tests/test_gate0.py -v --tb=short

# Signal tracker (models + SignalStore); set PYTHONPATH=src if imports fail
PYTHONPATH=src python -m pytest tests/test_tracker_models.py tests/test_signal_store.py -v
```

Ensure the venv is activated and the project is installed (`pip install -e ".[dev,grader]"`). Pytest is configured with `pythonpath = ["."]` in `pyproject.toml` so imports like `tests.fixtures.*` resolve when running from the repo root.

Notable suites: `tests/test_gate0.py`, `tests/test_sector_cache.py`, `tests/test_vol_analyst.py`, `tests/test_risk_analyst.py`, `tests/test_sector_analyst.py`, `tests/test_sentiment_analyst.py`, `tests/test_insider_tracker.py`, `tests/test_flow_analyst.py`, `tests/test_tracker_models.py`, `tests/test_signal_store.py`.

If your default `python` is not 3.11+, run tests with `python3.11 -m pytest ...` (project requires Python 3.11+).

```bash
python3.11 -m pytest tests/test_sector_analyst.py -v --tb=short
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

The bundled **`docker/Dockerfile`** default **`CMD`** runs **`python -m scanner.main`** (scanner loop **only** — no grader consumer). To run the **full pipeline** (scanner + grading + synthesis) in a container, override the command, for example:

```bash
docker compose -f docker/docker-compose.yaml run --rm scanner python -m scanner.run_pipeline
```

Ensure `UW_API_TOKEN`, `ANTHROPIC_API_KEY`, and (for Gate 3 specialists) `FINNHUB_API_KEY` are set in `.env` or the compose environment.

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

Optional **`grader`** extra: `anthropic` (Claude SDK).

**Console scripts** (after `pip install -e .`): `whale-scanner` → `scanner.main:main`, `whale-pipeline` → `scanner.run_pipeline:cli`.

Dev: `pytest`, `pytest-asyncio`, `respx`, `ruff`, `mypy`.

---

## License

MIT
