from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class MarketSession:
    timezone: str = "America/New_York"
    market_open_time: str = "09:30"
    market_close_time: str = "16:00"
    holidays: tuple[str, ...] = ()

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def open_time(self) -> time:
        hour, minute = self.market_open_time.split(":")
        return time(hour=int(hour), minute=int(minute))

    @property
    def close_time(self) -> time:
        hour, minute = self.market_close_time.split(":")
        return time(hour=int(hour), minute=int(minute))


def get_market_date(now: datetime, session: MarketSession) -> str:
    return now.astimezone(session.tzinfo).date().isoformat()


def is_trading_day(now: datetime, session: MarketSession) -> bool:
    local_now = now.astimezone(session.tzinfo)
    if local_now.weekday() >= 5:
        return False
    return local_now.date().isoformat() not in set(session.holidays)


def is_market_open(now: datetime, session: MarketSession) -> bool:
    local_now = now.astimezone(session.tzinfo)
    if not is_trading_day(now, session):
        return False
    start = local_now.replace(
        hour=session.open_time.hour,
        minute=session.open_time.minute,
        second=0,
        microsecond=0,
    )
    end = local_now.replace(
        hour=session.close_time.hour,
        minute=session.close_time.minute,
        second=0,
        microsecond=0,
    )
    return start <= local_now <= end
