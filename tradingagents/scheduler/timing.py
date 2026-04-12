from __future__ import annotations

from datetime import datetime, timedelta

from tradingagents.scheduler.market import MarketSession


def align_to_bucket_start(
    now: datetime,
    *,
    interval_minutes: int,
    session: MarketSession,
) -> datetime:
    local_now = now.astimezone(session.tzinfo)
    total_minutes = local_now.hour * 60 + local_now.minute
    market_open_minutes = session.open_time.hour * 60 + session.open_time.minute
    if total_minutes < market_open_minutes:
        aligned_local = local_now.replace(
            hour=session.open_time.hour,
            minute=session.open_time.minute,
            second=0,
            microsecond=0,
        )
        return aligned_local.astimezone(now.tzinfo)

    elapsed = max(0, total_minutes - market_open_minutes)
    bucket_offset = (elapsed // interval_minutes) * interval_minutes
    bucket_total = market_open_minutes + bucket_offset
    hour = bucket_total // 60
    minute = bucket_total % 60
    aligned_local = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return aligned_local.astimezone(now.tzinfo)


def next_bucket_start(
    now: datetime,
    *,
    interval_minutes: int,
    session: MarketSession,
) -> datetime:
    current_bucket = align_to_bucket_start(now, interval_minutes=interval_minutes, session=session)
    local_bucket = current_bucket.astimezone(session.tzinfo)
    next_local = local_bucket + timedelta(minutes=interval_minutes)
    return next_local.astimezone(now.tzinfo)
