"""Optional Slack/Discord webhook notifications for flagged candidates."""
from __future__ import annotations
import os

import structlog

logger = structlog.get_logger()


class Notifier:
    def __init__(self, webhook_url: str | None = None):
        self._url = webhook_url or os.environ.get("SLACK_WEBHOOK_URL")

    async def send(self, candidate, message: str | None = None) -> bool:
        """Send candidate alert to configured webhook. Returns True if sent."""
        if not self._url:
            return False
        try:
            try:
                from slack_sdk.webhook import WebhookClient

                client = WebhookClient(self._url)
                text = message or f"*{candidate.ticker}* {candidate.direction} — "
                text += f"${candidate.premium_usd:,.0f} premium, score {candidate.confluence_score:.1f}"
                client.send(text=text)
                return True
            except ImportError:
                import httpx

                async with httpx.AsyncClient() as c:
                    payload = {
                        "text": message
                        or f"{candidate.ticker} {candidate.direction} — "
                        f"${candidate.premium_usd:,.0f} premium",
                    }
                    r = await c.post(self._url, json=payload)
                    r.raise_for_status()
                return True
        except Exception as e:
            logger.warning("notifier_failed", error=str(e))
            return False
