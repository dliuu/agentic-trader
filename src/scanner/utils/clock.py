"""Market hours helper.

The scanner only runs during market hours (with a small
buffer for pre-market). Outside those hours it sleeps.
"""
import datetime as dt
from datetime import datetime, time

from zoneinfo import ZoneInfo


class MarketClock:
    def __init__(self, config: dict):
        self._tz = ZoneInfo(config["timezone"])
        self._open = time.fromisoformat(config["pre_market_start"])
        self._close = time.fromisoformat(config["market_close"])

    def is_market_hours(self) -> bool:
        now = datetime.now(self._tz)
        if now.weekday() >= 5:
            return False
        current_time = now.time()
        return self._open <= current_time <= self._close

    def seconds_until_open(self) -> float:
        """Seconds until next market open. Returns 0 if currently open."""
        now = datetime.now(self._tz)
        if self.is_market_hours():
            return 0
        open_today = now.replace(
            hour=self._open.hour, minute=self._open.minute, second=0, microsecond=0
        )
        if now < open_today:
            return (open_today - now).total_seconds()
        days_ahead = 1
        next_day = now + dt.timedelta(days=days_ahead)
        while next_day.weekday() >= 5:
            next_day += dt.timedelta(days=1)
        next_open = next_day.replace(
            hour=self._open.hour, minute=self._open.minute, second=0, microsecond=0
        )
        return (next_open - now).total_seconds()
