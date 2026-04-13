"""Poll UW headlines and SEC EDGAR for catalyst events on watched tickers."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import structlog

from shared.db import get_db
from shared.uw_http import uw_get
from shared.uw_runtime import get_uw_limiter
from shared.uw_validation import uw_auth_headers
from tracker.config import NewsWatcherConfig
from tracker.models import NewsEvent, NewsEventType, NewsWatchResult, Signal

log = structlog.get_logger()

UW_BASE = "https://api.unusualwhales.com"
EDGAR_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"


def detect_catalysts(
    title: str,
    config: NewsWatcherConfig,
) -> tuple[bool, list[str], bool]:
    """Check a headline or filing title for catalyst keywords.

    Returns:
        (is_catalyst, matched_keywords, is_tier1)
    """
    title_lower = title.lower()
    tier1_matches = [kw for kw in config.tier1_catalyst_keywords if kw in title_lower]
    tier2_matches = [kw for kw in config.tier2_catalyst_keywords if kw in title_lower]

    all_matches = tier1_matches + tier2_matches
    is_catalyst = len(all_matches) > 0
    is_tier1 = len(tier1_matches) > 0

    return is_catalyst, all_matches, is_tier1


class NewsWatcher:
    """Polls UW headlines and SEC EDGAR for catalyst events on watched tickers."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        api_token: str,
        config: NewsWatcherConfig | None = None,
    ):
        self._client = client
        self._headers = uw_auth_headers(api_token)
        self._cfg = config or NewsWatcherConfig()
        self._last_headline_poll: dict[str, datetime] = {}
        self._last_edgar_poll: dict[str, datetime] = {}

    async def check(self, signal: Signal) -> NewsWatchResult:
        """Run news check for a single signal."""
        now = datetime.now(timezone.utc)
        if not self._cfg.enabled:
            return NewsWatchResult(
                signal_id=signal.id,
                ticker=signal.ticker,
                checked_at=now,
            )

        events: list[NewsEvent] = []

        last_hl = self._last_headline_poll.get(signal.id)
        if last_hl is None or (now - last_hl).total_seconds() >= self._cfg.headline_interval_seconds:
            hl_events = await self._fetch_headlines(signal, now)
            events.extend(hl_events)
            self._last_headline_poll[signal.id] = now

        last_ed = self._last_edgar_poll.get(signal.id)
        if last_ed is None or (now - last_ed).total_seconds() >= self._cfg.edgar_interval_seconds:
            ed_events = await self._fetch_edgar(signal, now)
            events.extend(ed_events)
            self._last_edgar_poll[signal.id] = now

        events = await self._dedup_events(signal.id, events)

        has_catalyst = any(e.catalyst_matched for e in events)
        catalyst_types = list({kw for e in events for kw in e.catalyst_keywords})
        filing_detected = any(e.event_type == NewsEventType.SEC_FILING for e in events)

        tier1_count = sum(
            1
            for e in events
            if any(kw in self._cfg.tier1_catalyst_keywords for kw in e.catalyst_keywords)
        )
        tier2_count = sum(
            1
            for e in events
            if any(kw in self._cfg.tier2_catalyst_keywords for kw in e.catalyst_keywords)
            and not any(kw in self._cfg.tier1_catalyst_keywords for kw in e.catalyst_keywords)
        )
        regrade_filing = any(
            e.filing_type in self._cfg.regrade_filing_types for e in events if e.filing_type
        )

        regrade_recommended = (
            tier1_count > 0
            or regrade_filing
            or tier2_count >= self._cfg.min_tier2_for_regrade
        )

        return NewsWatchResult(
            signal_id=signal.id,
            ticker=signal.ticker,
            checked_at=now,
            events=events,
            has_catalyst=has_catalyst,
            catalyst_types=catalyst_types,
            filing_detected=filing_detected,
            regrade_recommended=regrade_recommended,
        )

    async def _fetch_headlines(self, signal: Signal, now: datetime) -> list[NewsEvent]:
        try:
            resp = await uw_get(
                self._client,
                f"{UW_BASE}/api/news/headlines",
                limiter=get_uw_limiter(),
                headers=self._headers,
                params={"ticker": signal.ticker, "limit": self._cfg.headline_limit},
            )
            resp.raise_for_status()
            data = resp.json().get("data", [])
        except Exception as exc:
            log.warning("news_watcher.headlines_failed", ticker=signal.ticker, error=str(exc))
            return []

        cutoff = signal.last_polled_at or signal.created_at
        events: list[NewsEvent] = []

        for item in data:
            published_raw = item.get("published_at") or item.get("created_at") or ""
            try:
                published = datetime.fromisoformat(str(published_raw).replace("Z", "+00:00"))
                if published.tzinfo is None:
                    published = published.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue

            if published <= cutoff:
                continue

            title = item.get("headline") or item.get("title") or ""
            source_id = str(item.get("id") or item.get("news_id") or "")

            is_catalyst, keywords, _ = detect_catalysts(title, self._cfg)

            events.append(
                NewsEvent(
                    id=str(uuid.uuid4()),
                    signal_id=signal.id,
                    ticker=signal.ticker,
                    event_type=NewsEventType.HEADLINE,
                    title=title,
                    source="uw_headlines",
                    url=item.get("url"),
                    published_at=published,
                    detected_at=now,
                    catalyst_matched=is_catalyst,
                    catalyst_keywords=keywords,
                    source_id=source_id,
                )
            )

        return events

    async def _fetch_edgar(self, signal: Signal, now: datetime) -> list[NewsEvent]:
        last_poll = self._last_edgar_poll.get(signal.id)
        if last_poll:
            start_date = last_poll.strftime("%Y-%m-%d")
        else:
            lookback = now - timedelta(days=self._cfg.edgar_lookback_days)
            start_date = lookback.strftime("%Y-%m-%d")
        end_date = now.strftime("%Y-%m-%d")

        form_types = ",".join(self._cfg.edgar_filing_types)

        try:
            resp = await self._client.get(
                EDGAR_SEARCH_URL,
                params={
                    "q": f'"{signal.ticker}"',
                    "dateRange": "custom",
                    "startdt": start_date,
                    "enddt": end_date,
                    "forms": form_types,
                },
                headers={
                    "User-Agent": self._cfg.edgar_user_agent,
                    "Accept": "application/json",
                },
                timeout=15.0,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log.warning("news_watcher.edgar_failed", ticker=signal.ticker, error=str(exc))
            return []

        hits = _extract_edgar_hits(data)
        events: list[NewsEvent] = []
        t_upper = signal.ticker.upper()

        for hit in hits:
            source_data = hit.get("_source", hit) if isinstance(hit, dict) else {}
            if not isinstance(source_data, dict):
                continue
            filing_type = source_data.get("file_type") or source_data.get("form_type") or ""
            filed_date_raw = source_data.get("file_date") or source_data.get("period_of_report") or ""
            accession = source_data.get("accession_no") or hit.get("_id") or ""
            entity_name = source_data.get("entity_name") or ""
            tickers_in_filing = source_data.get("tickers") or []

            if tickers_in_filing and t_upper not in {str(t).upper() for t in tickers_in_filing}:
                continue

            try:
                filed_date = datetime.strptime(str(filed_date_raw)[:10], "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                )
            except (ValueError, TypeError):
                continue

            title = f"{filing_type}: {entity_name}" if entity_name else filing_type
            url = (
                f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&accession={accession}"
                if accession
                else None
            )

            check_text = f"{filing_type} {title}".lower()
            is_catalyst, keywords, _ = detect_catalysts(check_text, self._cfg)

            if filing_type in self._cfg.regrade_filing_types and not is_catalyst:
                is_catalyst = True
                keywords = [*keywords, filing_type.lower()]

            events.append(
                NewsEvent(
                    id=str(uuid.uuid4()),
                    signal_id=signal.id,
                    ticker=signal.ticker,
                    event_type=NewsEventType.SEC_FILING,
                    title=title,
                    source="sec_edgar",
                    url=url,
                    published_at=filed_date,
                    detected_at=now,
                    catalyst_matched=is_catalyst,
                    catalyst_keywords=keywords,
                    filing_type=filing_type,
                    source_id=str(accession),
                )
            )

        return events

    async def _dedup_events(self, signal_id: str, events: list[NewsEvent]) -> list[NewsEvent]:
        if not events:
            return []
        ids_to_check = [e.source_id for e in events if e.source_id]
        if not ids_to_check:
            return events

        db = await get_db()
        try:
            placeholders = ",".join("?" * len(ids_to_check))
            cur = await db.execute(
                f"SELECT source_id FROM news_events WHERE signal_id = ? "
                f"AND source_id IN ({placeholders})",
                (signal_id, *ids_to_check),
            )
            rows = await cur.fetchall()
            existing = {r[0] for r in rows}
        finally:
            await db.close()

        return [e for e in events if not e.source_id or e.source_id not in existing]

    async def persist_events(self, events: list[NewsEvent]) -> None:
        if not events:
            return
        db = await get_db()
        try:
            for event in events:
                await db.execute(
                    """INSERT OR IGNORE INTO news_events
                       (id, signal_id, ticker, event_type, title, source, url,
                        published_at, detected_at, catalyst_matched,
                        catalyst_keywords, filing_type, source_id)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        event.id,
                        event.signal_id,
                        event.ticker,
                        event.event_type.value,
                        event.title,
                        event.source,
                        event.url,
                        event.published_at.isoformat(),
                        event.detected_at.isoformat(),
                        int(event.catalyst_matched),
                        json.dumps(event.catalyst_keywords),
                        event.filing_type,
                        event.source_id,
                    ),
                )
            await db.commit()
        finally:
            await db.close()

    async def get_events_for_signal(self, signal_id: str) -> list[NewsEvent]:
        db = await get_db()
        try:
            cur = await db.execute(
                """SELECT id, signal_id, ticker, event_type, title, source, url,
                          published_at, detected_at, catalyst_matched, catalyst_keywords,
                          filing_type, source_id
                   FROM news_events WHERE signal_id = ? ORDER BY published_at DESC""",
                (signal_id,),
            )
            rows = await cur.fetchall()
        finally:
            await db.close()

        out: list[NewsEvent] = []
        for r in rows:
            try:
                pub = datetime.fromisoformat(r[7])
                det = datetime.fromisoformat(r[8])
                if pub.tzinfo is None:
                    pub = pub.replace(tzinfo=timezone.utc)
                if det.tzinfo is None:
                    det = det.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue
            kws = json.loads(r[10]) if r[10] else []
            if not isinstance(kws, list):
                kws = []
            out.append(
                NewsEvent(
                    id=r[0],
                    signal_id=r[1],
                    ticker=r[2],
                    event_type=NewsEventType(r[3]),
                    title=r[4],
                    source=r[5],
                    url=r[6],
                    published_at=pub,
                    detected_at=det,
                    catalyst_matched=bool(r[9]),
                    catalyst_keywords=[str(x) for x in kws],
                    filing_type=r[11],
                    source_id=r[12] or "",
                )
            )
        return out


def _extract_edgar_hits(data: dict) -> list[dict]:
    """Normalize EDGAR EFTS JSON — primary schema and simple fallbacks."""
    if not isinstance(data, dict):
        return []
    hits_obj = data.get("hits")
    if isinstance(hits_obj, dict):
        inner = hits_obj.get("hits")
        if isinstance(inner, list):
            return [h for h in inner if isinstance(h, dict)]
    if isinstance(hits_obj, list):
        return [h for h in hits_obj if isinstance(h, dict)]
    return []
