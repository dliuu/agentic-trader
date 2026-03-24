"""Tests for grader response parser."""

import pytest

from grader.parser import ParseError, parse_grade_response

# Minimal valid GradeResponse JSON (all required fields)
VALID_82 = '{"score": 82, "verdict": "pass", "rationale": "Strong signal.", "signals_confirmed": ["otm"], "likely_directional": true}'
VALID_75 = '{"score": 75, "verdict": "pass", "rationale": "Moderate conviction.", "signals_confirmed": [], "likely_directional": true}'
VALID_60 = '{"score": 60, "verdict": "pass", "rationale": "Some ambiguity.", "signals_confirmed": ["premium"], "likely_directional": false}'


def test_parses_clean_json():
    result = parse_grade_response(VALID_82)
    assert result.score == 82
    assert result.verdict == "pass"
    assert result.rationale == "Strong signal."
    assert result.likely_directional is True


def test_strips_markdown_fences():
    raw = f"```json\n{VALID_75}\n```"
    result = parse_grade_response(raw)
    assert result.score == 75


def test_strips_markdown_fences_without_json_tag():
    raw = f"```\n{VALID_75}\n```"
    result = parse_grade_response(raw)
    assert result.score == 75


def test_handles_preamble():
    raw = f"Here is my analysis:\n\n{VALID_60}"
    result = parse_grade_response(raw)
    assert result.score == 60


def test_handles_trailing_text():
    raw = f"{VALID_82}\n\nLet me know if you need more detail."
    result = parse_grade_response(raw)
    assert result.score == 82


def test_rejects_out_of_range_score():
    raw = '{"score": 150, "verdict": "pass", "rationale": "X", "signals_confirmed": [], "likely_directional": true}'
    with pytest.raises(ParseError):
        parse_grade_response(raw)


def test_rejects_invalid_verdict():
    raw = '{"score": 80, "verdict": "maybe", "rationale": "X", "signals_confirmed": [], "likely_directional": true}'
    with pytest.raises(ParseError):
        parse_grade_response(raw)


def test_rejects_garbage():
    with pytest.raises(ParseError):
        parse_grade_response("I think this trade looks good!")


def test_rejects_empty_string():
    with pytest.raises(ParseError):
        parse_grade_response("")


def test_rejects_score_zero():
    raw = '{"score": 0, "verdict": "fail", "rationale": "X", "signals_confirmed": [], "likely_directional": false}'
    with pytest.raises(ParseError):
        parse_grade_response(raw)
