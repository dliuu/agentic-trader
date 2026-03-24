"""Thin async wrapper around the Anthropic messages API."""

import time
from dataclasses import dataclass

import anthropic
import structlog

log = structlog.get_logger()


@dataclass
class LLMResponse:
    text: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    model: str


class LLMClient:
    """Thin async wrapper around the Anthropic messages API."""

    def __init__(
        self,
        api_key: str,
        model: str = "claude-sonnet-4-20250514",
        max_tokens: int = 512,
        timeout: float = 15.0,
        max_retries: int = 2,
    ):
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key,
            timeout=timeout,
            max_retries=max_retries,
        )
        self._model = model
        self._max_tokens = max_tokens

    async def complete(self, system: str, user: str) -> LLMResponse:
        """Send a single completion request. Returns structured response."""
        start = time.monotonic()

        response = await self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )

        latency = int((time.monotonic() - start) * 1000)

        text = response.content[0].text
        usage = response.usage

        log.info(
            "llm_call_complete",
            model=self._model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            latency_ms=latency,
        )

        return LLMResponse(
            text=text,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            latency_ms=latency,
            model=self._model,
        )

    async def close(self):
        await self._client.close()
