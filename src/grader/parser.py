"""Parse raw LLM output into validated GradeResponse."""

import json
import re

import structlog

from grader.models import GradeResponse

log = structlog.get_logger()


class ParseError(Exception):
    """Raised when the LLM response cannot be parsed after all attempts."""

    pass


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
        return GradeResponse.model_validate_json(cleaned)
    except Exception as e:
        log.warning("parse_failed", raw=raw[:200], error=str(e))
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
RETRY_PROMPT = (
    "Your previous response was not valid JSON. "
    "Respond with ONLY a JSON object matching this schema, "
    "no other text:\n{schema}"
)
