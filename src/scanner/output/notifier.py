"""Optional Slack/Discord webhook notifications for flagged candidates."""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from tracker.guardrails import PositionSizing
    from tracker.models import Signal

logger = structlog.get_logger()


def format_actionable_signal(signal: "Signal", position: "PositionSizing") -> str:
    """Format an ACTIONABLE signal + sizing for Slack / manual execution (pilot)."""
    return (
        f"ACTIONABLE: {signal.ticker} {signal.direction.upper()}\n"
        f"Strike: {signal.strike} {signal.option_type} exp {signal.expiry}\n"
        f"Conviction: {signal.conviction_score:.0f}/100\n"
        f"Confirming flows: {signal.confirming_flows}\n"
        f"Position size: ${position.dollar_size:,.0f} ({position.contracts} contracts)\n"
        f"Max loss: ${position.max_loss_usd:,.0f}\n"
        f"Fingerprint: {signal.anomaly_fingerprint}"
    )


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
