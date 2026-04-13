from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path

import pandas as pd
import requests


SP500_WIKIPEDIA_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
CACHE_TTL = timedelta(days=1)
logger = logging.getLogger(__name__)


def load_sp500_symbols(
    *,
    cache_path: str | Path,
    refresh: bool = True,
) -> list[str]:
    cache_file = Path(cache_path)
    cache_file.parent.mkdir(parents=True, exist_ok=True)

    if not refresh:
        cached = _read_cache(cache_file)
        return cached or []

    cached_payload = _read_cache_payload(cache_file)
    if cached_payload is not None:
        fetched_at = _parse_datetime(cached_payload.get("fetched_at"))
        if (
            fetched_at is not None
            and datetime.now(timezone.utc) - fetched_at < CACHE_TTL
            and cached_payload.get("constituents")
        ):
            return _normalize_symbols(cached_payload.get("symbols") or [])

    try:
        constituents = _fetch_sp500_constituents()
        symbols = _normalize_symbols([item["symbol"] for item in constituents])
        _write_cache(cache_file, constituents)
        return symbols
    except Exception as exc:
        logger.warning("sp500_fetch_failed", extra={"error": str(exc), "cache_path": str(cache_file)})
        cached = _read_cache(cache_file)
        if cached:
            return cached
        return []


def load_sp500_metadata(
    *,
    cache_path: str | Path,
    refresh: bool = False,
) -> dict[str, dict[str, str]]:
    cache_file = Path(cache_path)
    if refresh:
        load_sp500_symbols(cache_path=cache_file, refresh=True)
    payload = _read_cache_payload(cache_file)
    if payload is None:
        return {}

    metadata: dict[str, dict[str, str]] = {}
    for item in payload.get("constituents") or []:
        symbol = str(item.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        metadata[symbol] = {
            "security": str(item.get("security") or "").strip(),
            "sector": str(item.get("sector") or "").strip(),
            "sub_industry": str(item.get("sub_industry") or "").strip(),
        }
    return metadata


def _fetch_sp500_constituents() -> list[dict[str, str]]:
    response = requests.get(
        SP500_WIKIPEDIA_URL,
        headers={"User-Agent": "Mozilla/5.0 (compatible; TradingAgentsBot/1.0)"},
        timeout=20,
    )
    response.raise_for_status()
    table = pd.read_html(StringIO(response.text), match="Symbol")[0]
    normalized: list[dict[str, str]] = []
    for _, row in table.iterrows():
        symbol = str(row.get("Symbol") or "").strip().upper().replace("-", ".")
        if not symbol:
            continue
        normalized.append(
            {
                "symbol": symbol,
                "security": str(row.get("Security") or "").strip(),
                "sector": str(row.get("GICS Sector") or "").strip(),
                "sub_industry": str(row.get("GICS Sub-Industry") or "").strip(),
            }
        )
    return normalized


def _normalize_symbols(symbols: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_symbol in symbols:
        symbol = str(raw_symbol).strip().upper().replace("-", ".")
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        normalized.append(symbol)
    return normalized


def _write_cache(cache_path: Path, constituents: list[dict[str, str]]) -> None:
    symbols = _normalize_symbols([item["symbol"] for item in constituents])
    payload = {
        "symbols": symbols,
        "constituents": constituents,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": SP500_WIKIPEDIA_URL,
    }
    cache_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _read_cache(cache_path: Path) -> list[str] | None:
    payload = _read_cache_payload(cache_path)
    if payload is None:
        return None
    return _normalize_symbols(payload.get("symbols") or [])


def _read_cache_payload(cache_path: Path) -> dict | None:
    if not cache_path.exists():
        return None
    try:
        return json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
