"""Database cleanup for terminal signals."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import shared.db as db_mod
from tracker.cleanup import CleanupConfig, run_cleanup


@pytest.mark.asyncio
async def test_cleanup_purges_old_terminal_preserves_active(tmp_path, monkeypatch):
    monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "trades.db")
    db = await db_mod.get_db()
    old_terminal = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    created = datetime.now(timezone.utc).isoformat()

    await db.execute(
        """INSERT INTO grades (id, candidate_id, score, verdict, graded_at)
           VALUES ('g-old', 'c-old', 80, 'pass', ?)""",
        (created,),
    )
    await db.execute(
        """INSERT INTO grades (id, candidate_id, score, verdict, graded_at)
           VALUES ('g-act', 'c-act', 80, 'pass', ?)""",
        (created,),
    )
    await db.execute(
        """INSERT INTO signals
           (id, ticker, strike, expiry, option_type, direction, state,
            initial_score, initial_premium, initial_oi, initial_volume,
            initial_contract_adv, grade_id, conviction_score,
            snapshots_taken, confirming_flows, oi_high_water,
            chain_spread_count, cumulative_premium, days_without_flow,
            created_at, terminal_at, terminal_reason)
           VALUES ('sig-old', 'OLD', 10, '2030-01-01', 'call', 'bullish', 'expired',
            80, 100000, 1000, 500, 0, 'g-old', 50.0,
            0, 0, 0, 0, 0.0, 0, ?, ?, 'expired')""",
        (created, old_terminal),
    )
    await db.execute(
        """INSERT INTO signals
           (id, ticker, strike, expiry, option_type, direction, state,
            initial_score, initial_premium, initial_oi, initial_volume,
            initial_contract_adv, grade_id, conviction_score,
            snapshots_taken, confirming_flows, oi_high_water,
            chain_spread_count, cumulative_premium, days_without_flow,
            created_at, terminal_at, terminal_reason)
           VALUES ('sig-act', 'ACT', 20, '2030-01-01', 'call', 'bullish', 'pending',
            80, 100000, 1000, 500, 0, 'g-act', 50.0,
            0, 0, 0, 0, 0.0, 0, ?, NULL, NULL)""",
        (created,),
    )
    await db.execute(
        """INSERT INTO signal_snapshots
           (id, signal_id, snapshot_at, contract_oi)
           VALUES ('snap1', 'sig-old', ?, 100)""",
        (created,),
    )
    await db.execute(
        """INSERT INTO flow_ledger
           (id, signal_id, alert_id, ticker, strike, expiry, option_type, direction,
            premium, volume, is_same_contract, is_same_expiry, source, created_at, recorded_at)
           VALUES ('led1', 'sig-old', 'a1', 'OLD', 10, '2030-01-01', 'call', 'bullish',
            50000, 100, 0, 0, 'scanner', ?, ?)""",
        (old_terminal, old_terminal),
    )
    await db.commit()
    await db.close()

    cfg = CleanupConfig(
        ledger_retention_days=1,
        snapshot_retention_days=1,
        news_retention_days=1,
        regrade_retention_days=1,
        terminal_signal_retention_days=1,
        purge_terminal_signals=True,
        size_warning_mb=5000,
    )
    summary = await run_cleanup(cfg)

    assert summary.get("signals_purged", 0) >= 1
    assert summary.get("vacuumed") is True

    db2 = await db_mod.get_db()
    cur = await db2.execute("SELECT COUNT(*) FROM signals WHERE id = 'sig-act'")
    row = await cur.fetchone()
    assert row is not None and row[0] == 1
    cur2 = await db2.execute("SELECT COUNT(*) FROM signals WHERE id = 'sig-old'")
    row2 = await cur2.fetchone()
    assert row2 is not None and row2[0] == 0
    await db2.close()
