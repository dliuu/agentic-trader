"""Operations settings from ``rules.yaml`` ``operations`` (cleanup, circuit breaker)."""

from __future__ import annotations

from dataclasses import dataclass

from tracker.cleanup import CleanupConfig


@dataclass(frozen=True)
class CircuitBreakerConfig:
    max_consecutive_failures: int = 10
    backoff_multiplier: int = 4


@dataclass(frozen=True)
class OperationsConfig:
    cleanup: CleanupConfig
    circuit_breaker: CircuitBreakerConfig


def load_operations_config(raw: dict) -> OperationsConfig:
    ops = raw.get("operations") or {}
    c_raw = ops.get("cleanup") or {}
    cb_raw = ops.get("circuit_breaker") or {}
    cleanup = CleanupConfig(
        ledger_retention_days=int(c_raw.get("ledger_retention_days", 30)),
        snapshot_retention_days=int(c_raw.get("snapshot_retention_days", 30)),
        news_retention_days=int(c_raw.get("news_retention_days", 30)),
        regrade_retention_days=int(c_raw.get("regrade_retention_days", 60)),
        terminal_signal_retention_days=int(c_raw.get("terminal_signal_retention_days", 90)),
        purge_terminal_signals=bool(c_raw.get("purge_terminal_signals", True)),
        size_warning_mb=int(c_raw.get("db_size_warning_mb", 500)),
    )
    circuit = CircuitBreakerConfig(
        max_consecutive_failures=int(cb_raw.get("max_consecutive_failures", 10)),
        backoff_multiplier=int(cb_raw.get("backoff_multiplier", 4)),
    )
    return OperationsConfig(cleanup=cleanup, circuit_breaker=circuit)
