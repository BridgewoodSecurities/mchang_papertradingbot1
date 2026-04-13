from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

import pandas as pd
import requests
from stockstats import wrap

from tradingagents.execution.logging_utils import redact_secrets


DEFAULT_DATA_URL = "https://data.alpaca.markets"
DEFAULT_TRADING_URL = "https://paper-api.alpaca.markets"
DEFAULT_DATA_FEED = "iex"
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

logger = logging.getLogger(__name__)


class AlpacaDataError(RuntimeError):
    """Raised when Alpaca market-data requests fail."""


class AlpacaDataClient:
    def __init__(
        self,
        *,
        api_key: str,
        secret_key: str,
        base_url: str = DEFAULT_DATA_URL,
        feed: str = DEFAULT_DATA_FEED,
        timeout: float = 10.0,
        max_retries: int = 3,
        session: requests.Session | None = None,
        client_logger: logging.Logger | None = None,
    ):
        if not api_key or not secret_key:
            raise AlpacaDataError("Alpaca API credentials are required for market data.")

        self.api_key = api_key
        self.secret_key = secret_key
        self.base_url = base_url.rstrip("/")
        self.feed = feed
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = session or requests.Session()
        self.logger = client_logger or logger

    @classmethod
    def from_env(cls) -> "AlpacaDataClient":
        api_key = os.getenv("ALPACA_API_KEY", "")
        secret_key = os.getenv("ALPACA_SECRET_KEY", "")
        base_url = os.getenv("ALPACA_DATA_URL", DEFAULT_DATA_URL)
        feed = os.getenv("ALPACA_MARKET_DATA_FEED", DEFAULT_DATA_FEED)
        timeout = float(os.getenv("ALPACA_REQUEST_TIMEOUT_SECONDS", "10"))
        max_retries = int(os.getenv("ALPACA_REQUEST_MAX_RETRIES", "3"))
        return cls(
            api_key=api_key,
            secret_key=secret_key,
            base_url=base_url,
            feed=feed,
            timeout=timeout,
            max_retries=max_retries,
        )

    def get_stock_bars(
        self,
        symbol: str,
        *,
        start: datetime,
        end: datetime,
        timeframe: str = "1Day",
        limit: int = 10000,
    ) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            "/v2/stocks/bars",
            params={
                "symbols": symbol.upper(),
                "timeframe": timeframe,
                "start": _to_rfc3339(start),
                "end": _to_rfc3339(end),
                "limit": limit,
                "adjustment": "raw",
                "sort": "asc",
                "feed": self.feed,
            },
        )
        bars = payload.get("bars", {})
        if isinstance(bars, dict):
            return bars.get(symbol.upper(), []) or []
        return bars or []

    def get_stock_bars_batch(
        self,
        symbols: list[str],
        *,
        start: datetime,
        end: datetime,
        timeframe: str = "1Day",
        limit: int = 10000,
    ) -> dict[str, list[dict[str, Any]]]:
        normalized_symbols = [symbol.strip().upper() for symbol in symbols if symbol.strip()]
        if not normalized_symbols:
            return {}
        payload = self._request(
            "GET",
            "/v2/stocks/bars",
            params={
                "symbols": ",".join(normalized_symbols),
                "timeframe": timeframe,
                "start": _to_rfc3339(start),
                "end": _to_rfc3339(end),
                "limit": limit,
                "adjustment": "raw",
                "sort": "asc",
                "feed": self.feed,
            },
        )
        bars = payload.get("bars", {})
        if not isinstance(bars, dict):
            return {}
        return {symbol: bars.get(symbol, []) or [] for symbol in normalized_symbols}

    def get_latest_trade(self, symbol: str) -> dict[str, Any]:
        payload = self._request(
            "GET",
            "/v2/stocks/trades/latest",
            params={"symbols": symbol.upper(), "feed": self.feed},
        )
        return (payload.get("trades") or {}).get(symbol.upper()) or {}

    def get_latest_trades(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        normalized_symbols = [symbol.strip().upper() for symbol in symbols if symbol.strip()]
        if not normalized_symbols:
            return {}
        payload = self._request(
            "GET",
            "/v2/stocks/trades/latest",
            params={"symbols": ",".join(normalized_symbols), "feed": self.feed},
        )
        trades = payload.get("trades") or {}
        if not isinstance(trades, dict):
            return {}
        return {symbol: trades.get(symbol) or {} for symbol in normalized_symbols}

    def get_latest_quote(self, symbol: str) -> dict[str, Any]:
        payload = self._request(
            "GET",
            "/v2/stocks/quotes/latest",
            params={"symbols": symbol.upper(), "feed": self.feed},
        )
        return (payload.get("quotes") or {}).get(symbol.upper()) or {}

    def get_news(
        self,
        *,
        symbols: list[str] | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        news: list[dict[str, Any]] = []
        page_token: str | None = None

        while len(news) < limit:
            params: dict[str, Any] = {
                "limit": min(50, limit - len(news)),
                "sort": "desc",
            }
            if symbols:
                params["symbols"] = ",".join(symbol.upper() for symbol in symbols)
            if start is not None:
                params["start"] = _to_rfc3339(start)
            if end is not None:
                params["end"] = _to_rfc3339(end)
            if page_token:
                params["page_token"] = page_token

            payload = self._request("GET", "/v1beta1/news", params=params)
            batch = payload.get("news") or []
            if not batch:
                break

            news.extend(batch)
            page_token = payload.get("next_page_token")
            if not page_token:
                break

        return news[:limit]

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        headers = {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.secret_key,
        }

        for attempt in range(1, self.max_retries + 1):
            try:
                self.logger.info(
                    "alpaca_data_request",
                    extra={
                        "url": url,
                        "method": method,
                        "params": redact_secrets(params),
                    },
                )
                response = self.session.request(
                    method=method,
                    url=url,
                    headers=headers,
                    params=params,
                    timeout=self.timeout,
                )
                if response.status_code in RETRYABLE_STATUS_CODES and attempt < self.max_retries:
                    time.sleep(2 ** (attempt - 1))
                    continue
                response.raise_for_status()
                if not response.text:
                    return {}
                try:
                    return response.json()
                except ValueError as exc:
                    raise AlpacaDataError(f"Alpaca market-data response was not valid JSON: {exc}") from exc
            except requests.RequestException as exc:
                if _is_retryable(exc) and attempt < self.max_retries:
                    time.sleep(2 ** (attempt - 1))
                    continue
                raise AlpacaDataError(f"Alpaca market-data request failed: {exc}") from exc

        raise AlpacaDataError("Alpaca market-data request exhausted all retries.")


def get_stock_data_alpaca(
    symbol: Annotated[str, "ticker symbol of the company"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
) -> str:
    start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_dt = (datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)).replace(
        tzinfo=timezone.utc
    )
    client = AlpacaDataClient.from_env()
    bars = client.get_stock_bars(symbol, start=start_dt, end=end_dt)
    if not bars:
        return f"No data found for symbol '{symbol}' between {start_date} and {end_date}"

    data = _bars_to_dataframe(bars)
    csv_string = data.to_csv(index=False)
    header = f"# Stock data for {symbol.upper()} from {start_date} to {end_date}\n"
    header += f"# Total records: {len(data)}\n"
    header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    return header + csv_string


def get_fundamentals_alpaca(
    ticker: Annotated[str, "ticker symbol of the company"],
    curr_date: Annotated[str, "current date (optional)"] = None,
) -> str:
    lines = _build_company_profile_lines(ticker)
    header = f"# Company Fundamentals for {ticker.upper()}\n"
    header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    return header + "\n".join(lines)


def get_balance_sheet_alpaca(
    ticker: Annotated[str, "ticker symbol of the company"],
    freq: Annotated[str, "frequency of data: 'annual' or 'quarterly'"] = "quarterly",
    curr_date: Annotated[str, "current date in YYYY-MM-DD format"] = None,
) -> str:
    return _statement_unavailable("Balance Sheet", ticker, freq)


def get_cashflow_alpaca(
    ticker: Annotated[str, "ticker symbol of the company"],
    freq: Annotated[str, "frequency of data: 'annual' or 'quarterly'"] = "quarterly",
    curr_date: Annotated[str, "current date in YYYY-MM-DD format"] = None,
) -> str:
    return _statement_unavailable("Cash Flow", ticker, freq)


def get_income_statement_alpaca(
    ticker: Annotated[str, "ticker symbol of the company"],
    freq: Annotated[str, "frequency of data: 'annual' or 'quarterly'"] = "quarterly",
    curr_date: Annotated[str, "current date in YYYY-MM-DD format"] = None,
) -> str:
    return _statement_unavailable("Income Statement", ticker, freq)


def get_insider_transactions_alpaca(
    ticker: Annotated[str, "ticker symbol of the company"]
) -> str:
    lines = _build_company_profile_lines(ticker)
    header = f"# Insider Transactions data for {ticker.upper()}\n"
    header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    header += "Alpaca does not expose insider transaction filings in its API.\n"
    header += "Use this company profile context instead.\n\n"
    return header + "\n".join(lines)


def get_news_alpaca(
    ticker: str,
    start_date: str,
    end_date: str,
) -> str:
    client = AlpacaDataClient.from_env()
    start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_dt = (datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)).replace(
        tzinfo=timezone.utc
    )
    news = client.get_news(symbols=[ticker], start=start_dt, end=end_dt, limit=20)
    if not news:
        return f"No news found for {ticker}"

    lines: list[str] = []
    for article in news:
        headline = article.get("headline") or "Untitled"
        source = article.get("source") or "Unknown"
        summary = article.get("summary") or ""
        url = article.get("url") or ""
        lines.append(f"### {headline} (source: {source})")
        if summary:
            lines.append(summary)
        if url:
            lines.append(f"Link: {url}")
        lines.append("")

    return f"## {ticker.upper()} News, from {start_date} to {end_date}:\n\n" + "\n".join(lines)


def get_global_news_alpaca(
    curr_date: str,
    look_back_days: int = 7,
    limit: int = 10,
) -> str:
    client = AlpacaDataClient.from_env()
    end_dt = (datetime.strptime(curr_date, "%Y-%m-%d") + timedelta(days=1)).replace(
        tzinfo=timezone.utc
    )
    start_dt = end_dt - timedelta(days=look_back_days)
    news = client.get_news(symbols=None, start=start_dt, end=end_dt, limit=limit)
    if not news:
        return f"No global news found for {curr_date}"

    lines: list[str] = []
    for article in news:
        headline = article.get("headline") or "Untitled"
        source = article.get("source") or "Unknown"
        summary = article.get("summary") or ""
        url = article.get("url") or ""
        symbols = article.get("symbols") or []
        symbol_suffix = f" [symbols: {', '.join(symbols)}]" if symbols else ""
        lines.append(f"### {headline} (source: {source}){symbol_suffix}")
        if summary:
            lines.append(summary)
        if url:
            lines.append(f"Link: {url}")
        lines.append("")

    start_date = start_dt.astimezone(timezone.utc).strftime("%Y-%m-%d")
    return f"## Global Market News, from {start_date} to {curr_date}:\n\n" + "\n".join(lines)


def fetch_symbol_news_items(symbol: str, *, limit: int = 10) -> list[dict[str, Any]]:
    client = AlpacaDataClient.from_env()
    return client.get_news(symbols=[symbol], limit=limit)


def fetch_global_news_items(*, limit: int = 10) -> list[dict[str, Any]]:
    client = AlpacaDataClient.from_env()
    return client.get_news(symbols=None, limit=limit)


def load_ohlcv_alpaca(symbol: str, curr_date: str) -> pd.DataFrame:
    client = AlpacaDataClient.from_env()
    curr_date_dt = pd.to_datetime(curr_date)
    start_date = curr_date_dt - pd.DateOffset(years=5)
    start_dt = pd.Timestamp(start_date).to_pydatetime().replace(tzinfo=timezone.utc)
    end_dt = (pd.Timestamp(curr_date_dt) + pd.DateOffset(days=1)).to_pydatetime().replace(
        tzinfo=timezone.utc
    )
    bars = client.get_stock_bars(symbol, start=start_dt, end=end_dt)
    if not bars:
        return pd.DataFrame(columns=["Date", "Open", "High", "Low", "Close", "Volume"])
    return _normalize_price_dataframe(_bars_to_dataframe(bars))


def _bars_to_dataframe(bars: list[dict[str, Any]]) -> pd.DataFrame:
    frame = pd.DataFrame(
        [
            {
                "Date": pd.to_datetime(item.get("t")),
                "Open": item.get("o"),
                "High": item.get("h"),
                "Low": item.get("l"),
                "Close": item.get("c"),
                "Adj Close": item.get("c"),
                "Volume": item.get("v"),
                "TradeCount": item.get("n"),
                "VWAP": item.get("vw"),
            }
            for item in bars
        ]
    )
    frame = frame.sort_values("Date").reset_index(drop=True)
    if getattr(frame["Date"].dt, "tz", None) is not None:
        frame["Date"] = frame["Date"].dt.tz_convert(None)
    return frame


def _normalize_price_dataframe(data: pd.DataFrame) -> pd.DataFrame:
    data["Date"] = pd.to_datetime(data["Date"], errors="coerce")
    data = data.dropna(subset=["Date"])

    price_cols = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in data.columns]
    data[price_cols] = data[price_cols].apply(pd.to_numeric, errors="coerce")
    data = data.dropna(subset=["Close"])
    data[price_cols] = data[price_cols].ffill().bfill()
    return data


def _statement_unavailable(statement_name: str, ticker: str, freq: str) -> str:
    lines = _build_company_profile_lines(ticker)
    header = f"# {statement_name} data for {ticker.upper()} ({freq})\n"
    header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    header += (
        "Alpaca's API does not expose GAAP financial statements such as balance sheet, "
        "cash flow, or income statement tables.\n"
        "Use the available company profile and live market context below.\n\n"
    )
    return header + "\n".join(lines)


def _build_company_profile_lines(ticker: str) -> list[str]:
    lines: list[str] = []
    try:
        asset = _get_asset_metadata(ticker)
    except Exception as exc:
        asset = {}
        lines.append(f"Asset metadata unavailable: {exc}")

    try:
        client = AlpacaDataClient.from_env()
        latest_trade = client.get_latest_trade(ticker)
        latest_quote = client.get_latest_quote(ticker)
        recent_news = client.get_news(symbols=[ticker], limit=3)
    except Exception as exc:
        latest_trade = {}
        latest_quote = {}
        recent_news = []
        lines.append(f"Live market snapshot unavailable: {exc}")

    fields = [
        ("Symbol", ticker.upper()),
        ("Name", asset.get("name")),
        ("Exchange", asset.get("exchange")),
        ("Asset Class", asset.get("class")),
        ("Status", asset.get("status")),
        ("Tradable", asset.get("tradable")),
        ("Marginable", asset.get("marginable")),
        ("Shortable", asset.get("shortable")),
        ("Easy to Borrow", asset.get("easy_to_borrow")),
        ("Fractionable", asset.get("fractionable")),
        ("Latest Trade Price", latest_trade.get("p")),
        ("Latest Bid", latest_quote.get("bp")),
        ("Latest Ask", latest_quote.get("ap")),
        ("Latest Trade Time", latest_trade.get("t")),
    ]
    for label, value in fields:
        if value not in (None, "", []):
            lines.append(f"{label}: {value}")

    if recent_news:
        lines.append("Recent News Headlines:")
        for article in recent_news:
            headline = article.get("headline") or "Untitled"
            source = article.get("source") or "Unknown"
            lines.append(f"- {headline} ({source})")

    if not lines:
        lines.append(f"No Alpaca company profile data found for symbol '{ticker}'.")
    return lines


def _to_rfc3339(value: datetime) -> str:
    normalized = value.astimezone(timezone.utc)
    return normalized.isoformat().replace("+00:00", "Z")


def _is_retryable(exc: requests.RequestException) -> bool:
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return exc.response.status_code in RETRYABLE_STATUS_CODES
    return True


def _get_asset_metadata(symbol: str) -> dict[str, Any]:
    api_key = os.getenv("ALPACA_API_KEY", "")
    secret_key = os.getenv("ALPACA_SECRET_KEY", "")
    if not api_key or not secret_key:
        raise AlpacaDataError("Alpaca API credentials are required for asset metadata.")

    base_url = os.getenv("ALPACA_BASE_URL", DEFAULT_TRADING_URL).rstrip("/")
    timeout = float(os.getenv("ALPACA_REQUEST_TIMEOUT_SECONDS", "10"))
    headers = {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": secret_key,
    }
    response = requests.get(
        f"{base_url}/v2/assets/{symbol.upper()}",
        headers=headers,
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json() if response.text else {}
