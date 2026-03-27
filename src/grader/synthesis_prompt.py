"""System and user prompts for the synthesis (final score) LLM."""

from __future__ import annotations

from shared.models import Candidate, SubScore

from grader.aggregator import AggregatedResult

SYNTHESIS_SYSTEM_PROMPT = """You are the synthesis judge for an options flow grading pipeline.

Six specialist agents have already scored this candidate. You receive their sub-scores, a \
deterministic weighted aggregate, spread (disagreement), and any cross-agent conflict flags.

Your job: produce ONE final integer score from 1–100 and a concise rationale that reconciles \
the evidence. Respect the aggregate as a strong prior, but you may adjust within the caps \
described below when conflicts demand it.

SCORING BANDS:
- 80–100: Exceptional alignment. Strong flow with supportive context and no material conflicts.
- 70–79: Good setup. Meets the bar for execution with manageable risks.
- 50–69: Mixed. Some positives but material doubts or conflicts unresolved.
- 30–49: Weak. Major contradictions or thin support.
- 1–29: Poor. Clear misalignment or structural problems.

CRITICAL RULES (the system will enforce caps after your response):
- Critical conflicts (e.g. very high flow conviction paired with very low risk score, or both \
volatility and risk scores very low) justify a ceiling around the mid‑60s unless you explicitly \
justify a lower score.
- If multiple agents score very low (<35), the final score is capped lower.
- When five or more active agents unanimously show high conviction (≥65), treat crowding as a \
caution flag — still reward quality but mention concentration risk.

RESPONSE FORMAT — respond with ONLY a JSON object (no markdown fences):
{
  "score": <integer 1-100>,
  "verdict": "pass" or "fail",
  "confidence": "low" | "medium" | "high",
  "rationale": "<2-4 sentences>",
  "conflict_resolution": "<how you weighed conflicts, or empty string>",
  "key_signal": "<short phrase>",
  "position_size_modifier": <float 0.0-1.0, suggested size vs baseline>
}

Use verdict "pass" only when your score is 70 or higher; otherwise "fail"."""


def estimate_synthesis_token_count(text: str) -> int:
    """Rough heuristic token estimate (~4 chars per token) for prompt budgeting tests."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def _truncate(s: str, max_len: int = 200) -> str:
    s = (s or "").strip()
    if len(s) <= max_len:
        return s
    return s[: max_len - 3] + "..."


def _candidate_option_type(candidate: Candidate) -> str:
    return "call" if candidate.direction == "bullish" else "put"


def _expiry_str(candidate: Candidate) -> str:
    exp = candidate.expiry
    return exp if isinstance(exp, str) else exp.strftime("%Y-%m-%d")


def build_synthesis_system_prompt() -> str:
    """Return the fixed synthesis system prompt."""
    return SYNTHESIS_SYSTEM_PROMPT


def build_synthesis_user_message(
    candidate: Candidate,
    sub_scores: dict[str, SubScore],
    aggregated: AggregatedResult,
) -> str:
    """Per-candidate user message for the synthesis LLM."""
    lines: list[str] = []

    lines.append("CANDIDATE:")
    lines.append(f"- ticker: {candidate.ticker}")
    lines.append(f"- strike: {candidate.strike}")
    lines.append(f"- expiry: {_expiry_str(candidate)}")
    lines.append(f"- option_type: {_candidate_option_type(candidate)}")
    lines.append(f"- premium_usd: {candidate.premium_usd:,.2f}")
    lines.append(f"- fill_type: {candidate.execution_type or 'N/A'}")
    if candidate.underlying_price is not None:
        lines.append(f"- spot_price: {candidate.underlying_price:.4f}")
    lines.append(f"- scanned_at: {candidate.scanned_at.isoformat()}")
    lines.append("")

    order = (
        "flow_analyst",
        "volatility_analyst",
        "risk_analyst",
        "sentiment_analyst",
        "insider_tracker",
        "sector_analyst",
    )
    lines.append("SUB-SCORES:")
    for key in order:
        sc = sub_scores.get(key)
        if sc is None:
            lines.append(f"- {key}: (missing)")
            continue
        status = "SKIPPED" if sc.skipped else "active"
        sigs = ", ".join(sc.signals[:5]) if sc.signals else "(none)"
        lines.append(
            f"- {key}: score={sc.score} ({status}) rationale={_truncate(sc.rationale)} "
            f"signals_top5=[{sigs}]"
        )
        if sc.skipped and sc.skip_reason:
            lines.append(f"  skip_reason: {_truncate(sc.skip_reason, 120)}")
    lines.append("")

    lines.append("AGGREGATION:")
    lines.append(f"- weighted_average: {aggregated.weighted_average:.2f}")
    lines.append(f"- score_stdev: {aggregated.score_stdev:.2f}")
    lines.append(f"- agent_agreement: {aggregated.agent_agreement}")
    lines.append("")

    rs = aggregated.risk_score
    lines.append("RISK PARAMETERS:")
    if rs is not None:
        lines.append(f"- recommended_position_size: {rs.recommended_position_size:.4f}")
        lines.append(f"- recommended_stop_loss_pct: {rs.recommended_stop_loss_pct:.4f}")
        lines.append(f"- max_entry_spread_pct: {rs.max_entry_spread_pct:.4f}")
    else:
        lines.append("- (risk analyst params unavailable)")
    lines.append("")

    if aggregated.conflict_flags:
        lines.append("CONFLICTS:")
        for c in aggregated.conflict_flags:
            lines.append(f"- {c.name}: severity={c.severity}")
        lines.append("")
    else:
        lines.append("CONFLICTS: (none detected)")
        lines.append("")

    if aggregated.skipped_agents:
        lines.append("SKIPPED AGENTS:")
        for name in aggregated.skipped_agents:
            sc = sub_scores.get(name)
            reason = sc.skip_reason if sc else "unknown"
            lines.append(f"- {name}: {_truncate(reason or '', 160)}")
        lines.append("")

    lines.append(
        "Return only the JSON object described in the system prompt. "
        "No markdown, no commentary."
    )
    return "\n".join(lines)
