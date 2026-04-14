"""Periodic database cleanup — purges old data from terminal signals.

Runs as a background task in the monitor loop, triggered once per day.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import structlog

from shared.db import get_db

log = structlog.get_logger()


@dataclass
class CleanupConfig:
    """Cleanup thresholds."""

    ledger_retention_days: int = 30
    snapshot_retention_days: int = 30
    news_retention_days: int = 30
    regrade_retention_days: int = 60
    terminal_signal_retention_days: int = 90
    purge_terminal_signals: bool = True
    size_warning_mb: int = 500


async def run_cleanup(config: CleanupConfig | None = None) -> dict:
    """Run all cleanup operations. Returns a summary dict.

    Call this once per day from the monitor loop.
    """
    cfg = config or CleanupConfig()
    summary: dict = {}
    db = await get_db()
    now = datetime.now(timezone.utc)

    try:
        # 1. Purge old flow_ledger entries
        try:
            cutoff = (now - timedelta(days=cfg.ledger_retention_days)).isoformat()
            cursor = await db.execute(
                "DELETE FROM flow_ledger WHERE created_at < ?",
                (cutoff,),
            )
            summary["ledger_purged"] = cursor.rowcount
            await db.commit()
        except Exception as exc:
            log.warning("cleanup.ledger_failed", error=str(exc))
            summary["ledger_purged"] = 0

        # 2. Purge snapshots for terminal signals older than retention
        try:
            cutoff = (now - timedelta(days=cfg.snapshot_retention_days)).isoformat()
            cursor = await db.execute(
                """DELETE FROM signal_snapshots
                   WHERE signal_id IN (
                       SELECT id FROM signals
                       WHERE state IN ('expired', 'decayed', 'executed')
                       AND terminal_at < ?
                   )""",
                (cutoff,),
            )
            summary["snapshots_purged"] = cursor.rowcount
            await db.commit()
        except Exception as exc:
            log.warning("cleanup.snapshots_failed", error=str(exc))
            summary["snapshots_purged"] = 0

        # 3. Purge old news_events for terminal signals
        try:
            cutoff = (now - timedelta(days=cfg.news_retention_days)).isoformat()
            cursor = await db.execute(
                """DELETE FROM news_events
                   WHERE signal_id IN (
                       SELECT id FROM signals
                       WHERE state IN ('expired', 'decayed', 'executed')
                       AND terminal_at < ?
                   )""",
                (cutoff,),
            )
            summary["news_purged"] = cursor.rowcount
            await db.commit()
        except Exception as exc:
            log.warning("cleanup.news_failed", error=str(exc))
            summary["news_purged"] = 0

        # 4. Purge old regrades for terminal signals
        try:
            cutoff = (now - timedelta(days=cfg.regrade_retention_days)).isoformat()
            cursor = await db.execute(
                """DELETE FROM regrades
                   WHERE signal_id IN (
                       SELECT id FROM signals
                       WHERE state IN ('expired', 'decayed', 'executed')
                       AND terminal_at < ?
                   )""",
                (cutoff,),
            )
            summary["regrades_purged"] = cursor.rowcount
            await db.commit()
        except Exception as exc:
            log.warning("cleanup.regrades_failed", error=str(exc))
            summary["regrades_purged"] = 0

        # 5. Purge very old terminal signals themselves
        if cfg.purge_terminal_signals:
            try:
                cutoff = (now - timedelta(days=cfg.terminal_signal_retention_days)).isoformat()
                cursor = await db.execute(
                    """DELETE FROM signals
                       WHERE state IN ('expired', 'decayed', 'executed')
                       AND terminal_at < ?""",
                    (cutoff,),
                )
                summary["signals_purged"] = cursor.rowcount
                await db.commit()
            except Exception as exc:
                log.warning("cleanup.signals_failed", error=str(exc))
                summary["signals_purged"] = 0
        else:
            summary["signals_purged"] = 0

        # 6. VACUUM to reclaim disk space
        try:
            await db.execute("VACUUM")
            summary["vacuumed"] = True
        except Exception as exc:
            log.warning("cleanup.vacuum_failed", error=str(exc))
            summary["vacuumed"] = False

        # 7. Check database file sizes
        for db_name, db_path in [
            ("trades.db", "data/trades.db"),
            ("scanner.db", "data/scanner.db"),
        ]:
            try:
                size_bytes = os.path.getsize(db_path)
                size_mb = size_bytes / (1024 * 1024)
                summary[f"{db_name}_size_mb"] = round(size_mb, 1)
                if size_mb > cfg.size_warning_mb:
                    log.warning(
                        "cleanup.db_size_warning",
                        db=db_name,
                        size_mb=round(size_mb, 1),
                        threshold_mb=cfg.size_warning_mb,
                    )
            except FileNotFoundError:
                summary[f"{db_name}_size_mb"] = 0

        total_purged = sum(
            v for k, v in summary.items() if k.endswith("_purged") and isinstance(v, int)
        )
        log.info("cleanup.complete", **summary, total_purged=total_purged)

        return summary
    finally:
        await db.close()
