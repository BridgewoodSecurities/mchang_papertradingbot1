from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Iterable

from tradingagents.dataflows.alpaca import AlpacaDataClient


def select_symbols_for_cycle(
    *,
    symbols: list[str],
    limit: int,
    as_of: datetime,
    data_client: AlpacaDataClient | None = None,
    held_symbols: set[str] | None = None,
    sector_by_symbol: dict[str, str] | None = None,
) -> list[str]:
    if limit <= 0 or not symbols:
        return []
    if len(symbols) <= limit:
        return list(symbols)

    client = data_client or AlpacaDataClient.from_env()
    ranked = _rank_symbols(
        symbols=symbols,
        as_of=as_of,
        client=client,
        held_symbols={symbol.upper() for symbol in (held_symbols or set())},
    )
    if ranked:
        return _select_diversified(
            ranked=ranked,
            limit=limit,
            sector_by_symbol={key.upper(): value for key, value in (sector_by_symbol or {}).items()},
        )
    return _rotate_symbols(symbols=symbols, limit=limit, as_of=as_of)


def _rank_symbols(
    *,
    symbols: list[str],
    as_of: datetime,
    client: AlpacaDataClient,
    held_symbols: set[str],
) -> list[tuple[str, float]]:
    start = (as_of - timedelta(days=10)).astimezone(timezone.utc)
    end = (as_of + timedelta(days=1)).astimezone(timezone.utc)
    scores: list[tuple[str, float]] = []
    news_count_by_symbol = _fetch_recent_news_counts(symbols=symbols, as_of=as_of, client=client)

    for chunk in _chunks(symbols, 100):
        bars_by_symbol = client.get_stock_bars_batch(chunk, start=start, end=end, timeframe="1Day", limit=10)
        for symbol in chunk:
            bars = bars_by_symbol.get(symbol.upper()) or []
            score = _score_symbol(
                symbol=symbol,
                bars=bars,
                held_symbols=held_symbols,
                news_count=news_count_by_symbol.get(symbol.upper(), 0),
            )
            if score is None:
                continue
            scores.append((symbol, score))

    scores.sort(key=lambda item: item[1], reverse=True)
    return scores


def _score_symbol(
    *,
    symbol: str,
    bars: list[dict],
    held_symbols: set[str],
    news_count: int,
) -> float | None:
    if len(bars) < 2:
        return None

    previous_bar = bars[-2]
    latest_bar = bars[-1]
    previous_close = float(previous_bar.get("c") or 0.0)
    latest_close = float(latest_bar.get("c") or 0.0)
    latest_volume = float(latest_bar.get("v") or 0.0)
    if previous_close <= 0 or latest_close <= 0 or latest_volume <= 0:
        return None

    lookback_bar = bars[-6] if len(bars) >= 6 else bars[0]
    lookback_close = float(lookback_bar.get("c") or 0.0)
    if lookback_close <= 0:
        return None

    one_day_return = (latest_close - previous_close) / previous_close
    five_day_return = (latest_close - lookback_close) / lookback_close
    absolute_move = abs(one_day_return)
    multi_day_move = abs(five_day_return)
    dollar_volume = latest_close * latest_volume
    liquidity_score = min(math.log10(max(dollar_volume, 1.0)) * 5.0, 60.0)
    news_score = min(news_count * 8.0, 24.0)

    directional_bonus = 0.0
    if five_day_return > 0:
        directional_bonus += 10.0
    if symbol.upper() in held_symbols:
        directional_bonus += 20.0
        if five_day_return < 0 or one_day_return < 0:
            directional_bonus += 12.0

    return (
        (absolute_move * 7_000.0)
        + (multi_day_move * 4_000.0)
        + liquidity_score
        + news_score
        + directional_bonus
    )


def _fetch_recent_news_counts(
    *,
    symbols: list[str],
    as_of: datetime,
    client: AlpacaDataClient,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    start = (as_of - timedelta(days=1)).astimezone(timezone.utc)
    end = (as_of + timedelta(days=1)).astimezone(timezone.utc)

    for chunk in _chunks(symbols, 50):
        try:
            items = client.get_news(symbols=chunk, start=start, end=end, limit=50)
        except Exception:
            continue
        for item in items:
            for raw_symbol in item.get("symbols") or []:
                symbol = str(raw_symbol).strip().upper()
                if symbol:
                    counts[symbol] = counts.get(symbol, 0) + 1
    return counts


def _select_diversified(
    *,
    ranked: list[tuple[str, float]],
    limit: int,
    sector_by_symbol: dict[str, str],
) -> list[str]:
    selected: list[str] = []
    selected_set: set[str] = set()
    seen_sectors: set[str] = set()

    for symbol, _score in ranked:
        sector = (sector_by_symbol.get(symbol.upper()) or "").strip().upper()
        if not sector or sector in seen_sectors:
            continue
        selected.append(symbol)
        selected_set.add(symbol.upper())
        seen_sectors.add(sector)
        if len(selected) >= limit:
            return selected

    for symbol, _score in ranked:
        key = symbol.upper()
        if key in selected_set:
            continue
        selected.append(symbol)
        selected_set.add(key)
        if len(selected) >= limit:
            return selected

    return selected


def _rotate_symbols(*, symbols: list[str], limit: int, as_of: datetime) -> list[str]:
    if not symbols:
        return []
    bucket_index = int(as_of.timestamp() // (15 * 60))
    offset = (bucket_index * limit) % len(symbols)
    rotated = symbols[offset:] + symbols[:offset]
    return rotated[:limit]


def _chunks(items: Iterable[str], size: int) -> Iterable[list[str]]:
    chunk: list[str] = []
    for item in items:
        chunk.append(item)
        if len(chunk) >= size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk
