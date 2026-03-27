"""Sample insider / congressional payloads for tests."""

from __future__ import annotations

from datetime import datetime, timezone

from grader.context.insider_ctx import DerivedInsiderSignals, InsiderContext

# Data-rich ticker (AAPL-style)
AAPL_FIXTURE = {
    "form4_filings": [
        {
            "insider_name": "Cook Tim",
            "insider_title": "CEO",
            "transaction_type": "P",
            "filing_date": "2024-03-01",
            "shares": 1000,
            "value": 150000.0,
        },
    ],
    "buy_sell_summary": {"buy_count": 5, "sell_count": 12},
    "political_holders": [{"politician": "Nancy Pelosi", "party": "D", "chamber": "House"}],
}

# No insider or congressional data
SMALLCAP_FIXTURE = {
    "form4_filings": [],
    "buy_sell_summary": None,
    "political_holders": [],
    "congressional_trades": [],
}

# Cluster-style buys
BIOTECH_FIXTURE = {
    "form4_filings": [
        {
            "insider_name": "Alpha CEO",
            "transaction_type": "P",
            "filing_date": "2024-03-01",
            "value": 2_000_000,
        },
        {
            "insider_name": "Beta CMO",
            "transaction_type": "P",
            "filing_date": "2024-03-04",
            "value": 500_000,
        },
    ],
}


def minimal_insider_context(
    *,
    derived: DerivedInsiderSignals | None = None,
    data_availability: dict[str, bool] | None = None,
    form4: list[dict] | None = None,
    political_holders: list[dict] | None = None,
    congressional_trades: list[dict] | None = None,
    finnhub_transactions: list[dict] | None = None,
) -> InsiderContext:
    """Build a small InsiderContext for unit tests."""
    da = data_availability or {
        "uw_form4": False,
        "uw_buy_sells": False,
        "uw_insider_flow": False,
        "uw_political_holders": False,
        "uw_congressional_trades": False,
        "finnhub_transactions": False,
        "finnhub_mspr": False,
    }
    return InsiderContext(
        ticker="TEST",
        option_type="call",
        trade_direction="bullish",
        scanned_at=datetime(2024, 3, 15, 16, 0, 0, tzinfo=timezone.utc),
        form4_filings=form4 or [],
        buy_sell_summary=None,
        insider_flow=[],
        political_holders=political_holders or [],
        congressional_trades=congressional_trades or [],
        finnhub_transactions=finnhub_transactions or [],
        finnhub_mspr=None,
        derived=derived or DerivedInsiderSignals(),
        data_availability=da,
    )
