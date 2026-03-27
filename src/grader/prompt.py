"""Prompt templates for the grader LLM and sentiment analyst."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from grader.models import GradeResponse, GradingContext, SentimentContext

if TYPE_CHECKING:
    from grader.context.insider_ctx import InsiderContext

SYSTEM_PROMPT = """You are a quantitative options flow analyst. You receive data about \
an unusual options trade that was flagged by an automated scanner. Your job is to \
assess the trade's conviction — is this likely a well-informed directional bet, or \
noise/hedging?

You MUST respond with a single JSON object matching this exact schema:
{schema}

Scoring guide:
- 90–100: Extremely high conviction. Multiple strong signals align, news supports thesis, \
  no signs of hedging.
- 70–89: High conviction. Clear directional intent with supporting context.
- 50–69: Moderate. Some supporting signals but significant ambiguity.
- 30–49: Low. Likely hedging, mixed signals, or insufficient context.
- 1–29: Very low. Contradictory signals, likely noise or routine hedging.

Respond ONLY with the JSON object. No markdown fences, no preamble, no explanation \
outside the JSON."""

USER_TEMPLATE = """Grade this unusual options trade:

## Option flow
- Ticker: {ticker}
- Strike: ${strike} {option_type}
- Expiry: {expiry}
- Premium paid: ${premium:,.0f}
- Fill type: {fill_type}
{flow_optional_block}

## Current market data
- Spot price: ${current_spot:.2f}
- Distance from strike: {otm_pct:.1f}% OTM
{market_optional_block}

## Greeks
{greeks_block}

## Recent news (last 48h)
{news_block}

## Insider / congressional trading (last 30d)
{insider_block}

## Scanner signals triggered
{signals_block}

Assess this trade. Is it directional or hedging? Does the context support the thesis?

CRITICAL FORMAT RULES:
- "verdict" must be EXACTLY the string "pass" or "fail" (lowercase, no other values)
- "score" must be an integer from 1 to 100
- Respond with ONLY the JSON object, no markdown fences, no explanation outside the JSON

Example valid response:
{{"score": 72, "verdict": "pass", "rationale": "...", "signals_confirmed": ["premium_size", "otm_depth"]}}

Example valid response:
{{"score": 35, "verdict": "fail", "rationale": "...", "signals_confirmed": []}}"""


def build_system_prompt() -> str:
    schema = json.dumps(GradeResponse.model_json_schema(), indent=2)
    return SYSTEM_PROMPT.format(schema=schema)


def build_user_prompt(ctx: GradingContext) -> str:
    c = ctx.candidate

    # OTM percentage
    otm_pct = abs(c.strike - ctx.current_spot) / ctx.current_spot * 100

    # Greeks block
    if ctx.greeks:
        greeks_block = (
            f"- Delta: {ctx.greeks.delta}\n"
            f"- Gamma: {ctx.greeks.gamma}\n"
            f"- Theta: {ctx.greeks.theta}\n"
            f"- Vega: {ctx.greeks.vega}\n"
            f"- IV: {ctx.greeks.iv}"
        )
    else:
        greeks_block = "- Not available"

    # News block
    if ctx.recent_news:
        news_block = "\n".join(
            f"- [{n.source}] {n.headline}" for n in ctx.recent_news
        )
    else:
        news_block = "- No recent headlines"

    # Insider block
    all_insider = ctx.insider_trades + ctx.congressional_trades
    if all_insider:
        insider_block = "\n".join(
            f"- {t.name} ({t.title}): {t.trade_type} "
            f"${t.value:,.0f} on {t.filed_at:%Y-%m-%d}"
            for t in all_insider
        )
    else:
        insider_block = "- No recent insider activity"

    # Signals block (Candidate.signals is list[SignalMatch])
    signals_block = "\n".join(f"- {s.rule_name}: {s.detail}" for s in c.signals)

    # option_type: Call for bullish, Put for bearish
    option_type = "CALL" if c.direction == "bullish" else "PUT"
    fill_type = c.execution_type or "N/A"
    expiry_str = c.expiry if isinstance(c.expiry, str) else c.expiry.strftime("%Y-%m-%d")

    # Avoid walls of "N/A" — omit sections when data is missing.
    flow_optional_lines: list[str] = []
    # Candidate currently doesn't carry volume / OI / OI change; omit them entirely.
    flow_optional_block = "\n".join(flow_optional_lines) if flow_optional_lines else "- (Additional flow fields unavailable)"

    market_optional_lines: list[str] = []
    if ctx.daily_volume:
        market_optional_lines.append(f"- Daily volume: {ctx.daily_volume:,}")
    if ctx.avg_daily_volume:
        market_optional_lines.append(f"- Avg daily volume: {ctx.avg_daily_volume:,}")
    if ctx.sector:
        market_optional_lines.append(f"- Sector: {ctx.sector}")
    if ctx.market_cap:
        market_optional_lines.append(f"- Market cap: ${ctx.market_cap:,.0f}")
    market_optional_block = "\n".join(market_optional_lines) if market_optional_lines else "- (Additional market fields unavailable)"

    return USER_TEMPLATE.format(
        ticker=c.ticker,
        strike=c.strike,
        option_type=option_type,
        expiry=expiry_str,
        premium=c.premium_usd,
        fill_type=fill_type,
        flow_optional_block=flow_optional_block,
        current_spot=ctx.current_spot,
        otm_pct=otm_pct,
        market_optional_block=market_optional_block,
        greeks_block=greeks_block,
        news_block=news_block,
        insider_block=insider_block,
        signals_block=signals_block,
    )


SENTIMENT_ANALYST_SYSTEM = """You are a sentiment analyst for an options trading system.

Your job: determine whether the information environment around a trade is
FAVORABLE, NEUTRAL, or UNFAVORABLE.

CRITICAL SCORING RULE — silence is golden:
- A ticker with NO news and NO Reddit mentions is NEUTRAL (score 50).
  This means the unusual flow has not been noticed. That's fine.
- A ticker trending on Reddit trading subs (especially r/wallstreetbets,
  r/Shortsqueeze) is a STRONG NEGATIVE signal. Retail crowd attention
  means the "edge" from unusual flow is likely already priced in or
  is a crowded trade. Deduct 15-30 points.
- A ticker with a catalyst in the news BUT low social chatter is the
  BEST case — informed money moving before the crowd notices. Add 10-20 points.

Respond ONLY with valid JSON matching this schema:
{
  "score": <int 1-100>,
  "verdict": "pass" | "fail",
  "rationale": "<2-3 sentences>",
  "signals_confirmed": ["<signal1>", ...],
  "risk_factors": ["<risk1>", ...],
  "crowd_exposure": "none" | "low" | "moderate" | "high"
}
"""


def build_sentiment_prompt(ctx: SentimentContext) -> str:
    """Build compact user prompt for sentiment analyst."""
    lines = [
        f"TICKER: {ctx.ticker}",
        f"TRADE DIRECTION: {ctx.trade_direction} ({ctx.option_type})",
        "",
        "=== NEWS ===",
        f"Headlines in last 48h: {ctx.headline_count_48h}",
    ]
    if ctx.headlines:
        lines.append("Recent headlines:")
        for h in ctx.headlines[:5]:
            lines.append(f"  - [{h.source}] {h.title}")
    else:
        lines.append("No recent headlines found.")

    lines += [
        "",
        "=== BUZZ METRICS (Finnhub) ===",
        f"Articles last week: {ctx.buzz.articles_last_week}",
        f"Weekly average: {ctx.buzz.weekly_average:.1f}",
        f"Buzz ratio: {ctx.buzz.buzz_ratio:.2f}x normal",
        f"Bullish %: {ctx.buzz.bullish_pct:.1f}%",
        f"Bearish %: {ctx.buzz.bearish_pct:.1f}%",
        "",
        "=== REDDIT TRADING SUBS (last 7 days) ===",
        f"Subreddits with mentions: {ctx.reddit.total_subreddits_with_mentions}/7",
        f"Total posts found: {ctx.reddit.total_post_count}",
    ]
    if ctx.reddit.total_post_count > 0:
        for rp in ctx.reddit.subreddits:
            if rp.post_count <= 0:
                continue
            snippet = ""
            if rp.top_post_title:
                snippet = f' (top: "{rp.top_post_title[:60]}..." score={rp.top_post_score})'
            lines.append(f"  r/{rp.subreddit}: {rp.post_count} posts{snippet}")
    else:
        lines.append("  No mentions found in any trading subreddit.")

    lines += [
        "",
        "=== PRE-COMPUTED FLAGS ===",
        f"Has catalyst: {ctx.has_catalyst}",
        f"Is quiet (no news + no reddit): {ctx.is_quiet}",
        f"News aligns with trade direction: {ctx.news_aligns_with_direction}",
        f"Meme candidate (WSB/Shortsqueeze): {ctx.reddit.is_meme_candidate}",
        f"Crowded (4+ sub mentions): {ctx.reddit.is_crowded}",
    ]
    return "\n".join(lines)


INSIDER_TRACKER_SYSTEM_PROMPT = """You are the Insider Tracker agent in an options trading grading pipeline.

Your job: Determine whether corporate insider behavior and congressional trading activity
support or contradict the unusual options flow signal.

You will receive pre-computed signals and raw transaction data. Score the insider alignment
on a 1-100 scale:

SCORING GUIDE:
- 80-100: Strong insider alignment. Cluster buying in the same direction as the flow,
          recent large purchases, or notable congressional accumulation.
- 60-79:  Moderate alignment. Some insider buying, no contradictory signals, or
          congressional interest without strong timing.
- 40-59:  Neutral / insufficient data. Few transactions, mixed signals, or data only
          from one source with no cross-validation.
- 20-39:  Moderate misalignment. Insiders selling while flow is bullish, or
          congressional positions being liquidated.
- 1-19:   Strong misalignment. Cluster selling opposite to the flow direction,
          especially by C-suite insiders, right before the flow signal.

IMPORTANT CONTEXT:
- Not all tickers have insider or congressional data. Missing data = neutral, not negative.
- Insider sales are common for compensation/diversification. Only flag sales as negative
  if they are unusually large, clustered, or timed suspiciously.
- Option exercises followed by immediate sales ("M" then "S") are routine. These are
  less meaningful than open-market purchases ("P").
- Congressional trades are disclosed with a delay (up to 45 days). Weight them less for
  timing analysis but they still indicate conviction.

You MUST respond with valid JSON matching this schema:
{
  "score": <int 1-100>,
  "verdict": "pass" | "fail",
  "rationale": "<2-3 sentences explaining your assessment>",
  "signals_confirmed": ["<signal1>", "<signal2>"],
  "risk_factors": ["<risk1>"],
  "likely_directional": <bool>
}"""


INSIDER_TRACKER_USER_PROMPT = """## Candidate
- Ticker: {ticker}
- Option type: {option_type}
- Trade direction: {trade_direction}
- Scanned at: {scanned_at}

## Data Availability
{data_availability_section}

## Pre-Computed Signals
- Buy/sell ratio (90d): {buy_sell_ratio}
- Net insider value (90d): ${net_insider_value_90d:,.0f}
- Days since last insider buy: {days_since_last_buy}
- Days since last insider sell: {days_since_last_sell}
- Cluster buys detected: {num_cluster_buys}
- Cluster sells detected: {num_cluster_sells}
- UW/Finnhub agreement: {uw_finnhub_agreement}
- Finnhub MSPR (current month): {mspr_current}
- MSPR trend: {mspr_trend}
- Congressional holders: {num_political_holders}
- Congressional direction (90d): {congressional_direction}

{cluster_details_section}

## Recent Insider Transactions (last 180 days, up to 20 most recent)
{insider_transactions_section}

## Insider Trades Relative to Flow Signal
Trades filed BEFORE the flow signal ({num_before} trades):
{trades_before_section}

Trades filed AFTER the flow signal ({num_after} trades):
{trades_after_section}

## Congressional Data
{congressional_section}

Grade this candidate's insider alignment with the unusual options flow."""


def build_insider_tracker_user_prompt(ctx: InsiderContext) -> str:
    """Render insider tracker user prompt from built context."""
    from grader.context.insider_ctx import (
        build_cluster_details_section,
        build_congressional_section,
        build_data_availability_section,
        build_insider_transactions_section,
        format_trades_list,
    )
    from shared.filters import InsiderScoringConfig

    cfg = InsiderScoringConfig()
    d = ctx.derived
    ratio = d.buy_sell_ratio
    ratio_s = f"{ratio:.2f}" if ratio is not None else "n/a"
    mspr_c = f"{d.mspr_current:.2f}" if d.mspr_current is not None else "n/a"
    agr = d.uw_finnhub_agreement
    agr_s = "n/a" if agr is None else ("true" if agr else "false")

    return INSIDER_TRACKER_USER_PROMPT.format(
        ticker=ctx.ticker,
        option_type=ctx.option_type,
        trade_direction=ctx.trade_direction,
        scanned_at=ctx.scanned_at.isoformat(),
        data_availability_section=build_data_availability_section(ctx),
        buy_sell_ratio=ratio_s,
        net_insider_value_90d=d.net_insider_value_90d,
        days_since_last_buy=d.days_since_last_insider_buy,
        days_since_last_sell=d.days_since_last_insider_sell,
        num_cluster_buys=len(d.cluster_buys),
        num_cluster_sells=len(d.cluster_sells),
        uw_finnhub_agreement=agr_s,
        mspr_current=mspr_c,
        mspr_trend=d.mspr_trend or "n/a",
        num_political_holders=d.num_political_holders,
        congressional_direction=d.congressional_direction or "n/a",
        cluster_details_section=build_cluster_details_section(ctx),
        insider_transactions_section=build_insider_transactions_section(ctx, cfg.max_transactions_in_prompt),
        num_before=len(d.insider_trades_before_flow),
        trades_before_section=format_trades_list(d.insider_trades_before_flow),
        num_after=len(d.insider_trades_after_flow),
        trades_after_section=format_trades_list(d.insider_trades_after_flow),
        congressional_section=build_congressional_section(ctx),
    )
