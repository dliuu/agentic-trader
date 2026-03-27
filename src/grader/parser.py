"""Parse raw LLM output into validated Pydantic response models."""

import json
import re

import structlog
from pydantic import BaseModel

from grader.models import GradeResponse

log = structlog.get_logger()

_PASS_SIGNALS = {"pass", "high conviction", "high_conviction", "strong conviction"}
_FAIL_SIGNALS = {
    "fail",
    "low conviction",
    "low_conviction",
    "skip",
    "insufficient",
    "no conviction",
    "neutral",
    "hedge",
}


class ParseError(Exception):
    """Raised when the LLM response cannot be parsed after all attempts."""

    pass


def normalize_verdict(raw: str) -> str:
    lower = str(raw).strip().lower()
    if lower in ("pass", "fail"):
        return lower
    for signal in _PASS_SIGNALS:
        if signal in lower:
            return "pass"
    for signal in _FAIL_SIGNALS:
        if signal in lower:
            return "fail"
    return "fail"


def parse_grade_response(raw: str) -> GradeResponse:
    """
    Parse raw LLM text into a validated GradeResponse.

    Handles common LLM output issues:
    - Markdown code fences (```json ... ```)
    - Preamble text before the JSON
    - Trailing text after the JSON
    """
    cleaned = _extract_json(raw)

    try:
        parsed = json.loads(cleaned)
        if not isinstance(parsed, dict):
            raise ValueError(f"Expected JSON object, got {type(parsed)}")
        if "verdict" in parsed:
            parsed["verdict"] = normalize_verdict(parsed["verdict"])
        return GradeResponse.model_validate(parsed)
    except Exception as e:
        log.warning("parse_failed", raw=raw[:200], error=str(e))
        raise ParseError(f"Failed to parse LLM response: {e}") from e


def parse_llm_response(raw: str, model_cls: type[BaseModel]) -> BaseModel:
    """Parse raw LLM text into any Pydantic model."""
    cleaned = _extract_json(raw)
    try:
        parsed = json.loads(cleaned)
        if not isinstance(parsed, dict):
            raise ValueError(f"Expected JSON object, got {type(parsed)}")
        if "verdict" in parsed:
            parsed["verdict"] = normalize_verdict(parsed["verdict"])
        return model_cls.model_validate(parsed)
    except Exception as e:
        log.warning("parse_failed", raw=raw[:200], error=str(e), model=model_cls.__name__)
        raise ParseError(f"Failed to parse LLM response: {e}") from e


def _extract_json(text: str) -> str:
    """Extract a JSON object from potentially messy LLM output."""
    # Strip markdown fences
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"```\s*$", "", text)
    text = text.strip()

    # If it starts with {, try to find the matching }
    if text.startswith("{"):
        # Find the last } — handles cases where LLM adds trailing text
        last_brace = text.rfind("}")
        if last_brace != -1:
            return text[: last_brace + 1]

    # Try to find a JSON object anywhere in the text
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        return match.group(0)

    # Last resort: return as-is and let Pydantic fail with a clear error
    return text


# Retry prompt for when parsing fails
RETRY_PROMPT = """Your previous response could not be parsed. Error: {error}

Respond with ONLY a JSON object matching this exact schema:
{{
  "score": <integer 1-100>,
  "verdict": "pass" or "fail",
  "rationale": "<1-2 sentence explanation>",
  "signals_confirmed": ["<signal_name>", ...],
  "risk_factors": ["<risk>", ...],
  "likely_directional": <true|false>
}}

No markdown fences. No text outside the JSON. verdict must be exactly "pass" or "fail".
"""
