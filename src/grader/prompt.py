"""Prompt templates for the grader LLM."""

import json

from grader.models import GradingContext, GradeResponse

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
