"""Tests for grader LLM client."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from grader.llm_client import LLMClient, LLMResponse


@pytest.fixture
def mock_anthropic(monkeypatch):
    """Patch the Anthropic SDK to return a canned response."""
    mock_message = MagicMock()
    mock_message.content = [MagicMock(text='{"score": 75, "verdict": "pass", "rationale": "Test.", "signals_confirmed": [], "likely_directional": true}')]
    mock_message.usage = MagicMock(input_tokens=100, output_tokens=50)

    async def fake_create(*, model, max_tokens, system, messages, temperature=None, **_):
        return mock_message

    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.create = fake_create

    def fake_anthropic(*args, **kwargs):
        return mock_client

    monkeypatch.setattr("grader.llm_client.anthropic.AsyncAnthropic", fake_anthropic)
    return mock_client


@pytest.mark.asyncio
async def test_complete_returns_structured_response(mock_anthropic):
    client = LLMClient(api_key="test-key")
    resp = await client.complete("system", "user")
    assert isinstance(resp, LLMResponse)
    assert resp.text.startswith("{")
    assert resp.input_tokens == 100
    assert resp.output_tokens == 50
    assert resp.latency_ms >= 0
    assert resp.model == "claude-sonnet-4-20250514"


@pytest.mark.asyncio
async def test_client_uses_exact_model_and_max_tokens(monkeypatch):
    """LLMClient uses model claude-sonnet-4-20250514 and max_tokens 512."""
    create_called = False
    create_kwargs = {}

    async def capture_create(**kwargs):
        nonlocal create_called, create_kwargs
        create_called = True
        create_kwargs = kwargs
        mock_message = MagicMock()
        mock_message.content = [MagicMock(text="{}")]
        mock_message.usage = MagicMock(input_tokens=1, output_tokens=1)
        return mock_message

    mock_client = AsyncMock()
    mock_client.messages.create = capture_create

    def fake_anthropic(*args, **kwargs):
        return mock_client

    monkeypatch.setattr("grader.llm_client.anthropic.AsyncAnthropic", fake_anthropic)

    client = LLMClient(api_key="test-key")
    await client.complete("sys", "usr")

    assert create_called
    assert create_kwargs.get("model") == "claude-sonnet-4-20250514"
    assert create_kwargs.get("max_tokens") == 512


@pytest.mark.asyncio
async def test_complete_accepts_max_tokens_override(monkeypatch):
    create_kwargs = {}

    async def capture_create(**kwargs):
        nonlocal create_kwargs
        create_kwargs = kwargs
        mock_message = MagicMock()
        mock_message.content = [MagicMock(text="{}")]
        mock_message.usage = MagicMock(input_tokens=1, output_tokens=1)
        return mock_message

    mock_client = AsyncMock()
    mock_client.messages.create = capture_create

    def fake_anthropic(*args, **kwargs):
        return mock_client

    monkeypatch.setattr("grader.llm_client.anthropic.AsyncAnthropic", fake_anthropic)

    client = LLMClient(api_key="test-key", max_tokens=512)
    await client.complete("sys", "usr", max_tokens=300)

    assert create_kwargs.get("max_tokens") == 300


@pytest.mark.asyncio
async def test_client_timeout_configurable(monkeypatch):
    """Timeout is configurable (default 15s)."""
    captured_timeout = None

    def fake_anthropic(api_key=None, timeout=None, max_retries=None):
        nonlocal captured_timeout
        captured_timeout = timeout
        mock_client = AsyncMock()
        mock_message = MagicMock()
        mock_message.content = [MagicMock(text="{}")]
        mock_message.usage = MagicMock(input_tokens=1, output_tokens=1)

        async def fake_create(**kwargs):
            return mock_message

        mock_client.messages.create = fake_create
        return mock_client

    monkeypatch.setattr("grader.llm_client.anthropic.AsyncAnthropic", fake_anthropic)

    LLMClient(api_key="test-key")
    assert captured_timeout == 15.0

    LLMClient(api_key="test-key", timeout=30.0)
    assert captured_timeout == 30.0
