from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from typing import Any

import requests
import yfinance as yf

from tradingagents.brokers.base import BaseBroker, BrokerError
from tradingagents.execution.logging_utils import redact_secrets
from tradingagents.execution.models import (
    BrokerAccountSnapshot,
    BrokerOrder,
    BrokerPosition,
    OrderIntent,
    OrderType,
    TradeAction,
)


DEFAULT_PAPER_URL = "https://paper-api.alpaca.markets"
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class BrokerConfigurationError(BrokerError):
    """Raised when the broker is misconfigured."""


class AlpacaPaperBroker(BaseBroker):
    def __init__(
        self,
        *,
        api_key: str,
        secret_key: str,
        base_url: str = DEFAULT_PAPER_URL,
        timeout: float = 10.0,
        max_retries: int = 3,
        logger: logging.Logger | None = None,
        session: requests.Session | None = None,
    ):
        if not api_key or not secret_key:
            raise BrokerConfigurationError("Alpaca API credentials are required.")

        normalized_url = base_url.rstrip("/")
        if "paper-api.alpaca.markets" not in normalized_url:
            raise BrokerConfigurationError(
                "Refusing to initialize Alpaca broker with a non-paper endpoint."
            )

        self.api_key = api_key
        self.secret_key = secret_key
        self.base_url = normalized_url
        self.timeout = timeout
        self.max_retries = max_retries
        self.logger = logger or logging.getLogger(__name__)
        self.session = session or requests.Session()

    @classmethod
    def from_env(cls, *, logger: logging.Logger | None = None) -> "AlpacaPaperBroker":
        api_key = os.getenv("ALPACA_API_KEY", "")
        secret_key = os.getenv("ALPACA_SECRET_KEY", "")
        base_url = os.getenv("ALPACA_BASE_URL", DEFAULT_PAPER_URL)
        return cls(
            api_key=api_key,
            secret_key=secret_key,
            base_url=base_url,
            logger=logger,
        )

    def get_account(self) -> BrokerAccountSnapshot:
        payload = self._request("GET", "/v2/account")
        return BrokerAccountSnapshot(
            account_id=payload.get("id"),
            status=payload.get("status"),
            currency=payload.get("currency") or "USD",
            cash=float(payload.get("cash", 0.0)),
            equity=float(payload.get("equity", 0.0)),
            buying_power=float(payload.get("buying_power", 0.0)),
            portfolio_value=float(payload.get("portfolio_value", payload.get("equity", 0.0))),
            daytrade_count=int(payload.get("daytrade_count", 0)),
            paper=bool(payload.get("account_blocked") is False),
            raw=payload,
        )

    def list_positions(self) -> list[BrokerPosition]:
        payload = self._request("GET", "/v2/positions")
        return [
            BrokerPosition(
                symbol=item["symbol"],
                qty=float(item["qty"]),
                avg_entry_price=float(item.get("avg_entry_price", 0.0)),
                market_value=float(item.get("market_value", 0.0)),
                cost_basis=float(item.get("cost_basis", 0.0)),
                unrealized_pl=float(item.get("unrealized_pl", 0.0)),
                side=item.get("side", "long"),
                raw=item,
            )
            for item in payload
        ]

    def list_open_orders(self) -> list[BrokerOrder]:
        return self.list_orders(status="open", limit=100)

    def list_orders(self, *, status: str = "all", limit: int = 50) -> list[BrokerOrder]:
        payload = self._request(
            "GET",
            "/v2/orders",
            params={"status": status, "limit": limit, "direction": "desc"},
        )
        return [self._map_order(item) for item in payload]

    def submit_order(self, intent: OrderIntent, *, client_order_id: str) -> BrokerOrder:
        body: dict[str, Any] = {
            "symbol": intent.symbol,
            "side": "buy" if intent.action == TradeAction.BUY else "sell",
            "type": intent.order_type.value,
            "time_in_force": "day",
            "client_order_id": client_order_id,
        }

        if intent.order_type == OrderType.LIMIT:
            if intent.limit_price is None:
                raise BrokerError("Limit orders require a limit_price.")
            body["limit_price"] = f"{intent.limit_price:.2f}"
            if intent.quantity is None:
                raise BrokerError("Limit orders require share quantity.")
            body["qty"] = self._format_number(intent.quantity)
        else:
            if intent.quantity is not None:
                body["qty"] = self._format_number(intent.quantity)
            elif intent.notional_usd is not None:
                body["notional"] = f"{intent.notional_usd:.2f}"
            else:
                raise BrokerError("Market orders require quantity or notional sizing.")

        payload = self._request("POST", "/v2/orders", json_body=body)
        return self._map_order(payload)

    def get_latest_price(self, symbol: str) -> float:
        history = yf.Ticker(symbol).history(period="5d", interval="1d", auto_adjust=False)
        if history.empty:
            raise BrokerError(f"Unable to retrieve a latest price for {symbol}.")
        close_price = history["Close"].dropna()
        if close_price.empty:
            raise BrokerError(f"Latest close price for {symbol} is unavailable.")
        return float(close_price.iloc[-1])

    def get_latest_bid_price(self, symbol: str) -> float | None:
        ticker = yf.Ticker(symbol)
        fast_info = getattr(ticker, "fast_info", None)
        bid = None
        if fast_info is not None:
            try:
                bid = fast_info.get("bid")
            except Exception:
                bid = None
        if bid is None:
            info = getattr(ticker, "info", {}) or {}
            bid = info.get("bid")
        if bid in (None, 0):
            return None
        return float(bid)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        headers = {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.secret_key,
        }

        for attempt in range(1, self.max_retries + 1):
            try:
                self.logger.info(
                    "alpaca_request",
                    extra={
                        "url": url,
                        "method": method,
                        "params": redact_secrets(params),
                        "json": redact_secrets(json_body),
                    },
                )
                response = self.session.request(
                    method=method,
                    url=url,
                    headers=headers,
                    params=params,
                    json=json_body,
                    timeout=self.timeout,
                )
                if response.status_code in RETRYABLE_STATUS_CODES and attempt < self.max_retries:
                    time.sleep(2 ** (attempt - 1))
                    continue
                response.raise_for_status()
                if not response.text:
                    return {}
                return response.json()
            except requests.RequestException as exc:
                if self._is_retryable(exc) and attempt < self.max_retries:
                    time.sleep(2 ** (attempt - 1))
                    continue
                raise BrokerError(f"Alpaca request failed: {exc}") from exc

        raise BrokerError("Alpaca request exhausted all retries.")

    def _is_retryable(self, exc: requests.RequestException) -> bool:
        if isinstance(exc, requests.HTTPError) and exc.response is not None:
            return exc.response.status_code in RETRYABLE_STATUS_CODES
        return True

    def _map_order(self, payload: dict[str, Any]) -> BrokerOrder:
        return BrokerOrder(
            id=payload.get("id"),
            client_order_id=payload.get("client_order_id"),
            symbol=payload["symbol"],
            side=TradeAction.BUY if payload.get("side") == "buy" else TradeAction.SELL,
            order_type=OrderType(payload.get("type", "market")),
            status=payload.get("status", "unknown"),
            qty=float(payload["qty"]) if payload.get("qty") else None,
            notional_usd=float(payload["notional"]) if payload.get("notional") else None,
            limit_price=float(payload["limit_price"]) if payload.get("limit_price") else None,
            filled_qty=float(payload["filled_qty"]) if payload.get("filled_qty") else None,
            filled_avg_price=float(payload["filled_avg_price"]) if payload.get("filled_avg_price") else None,
            submitted_at=self._parse_datetime(payload.get("submitted_at")),
            raw=payload,
        )

    def _parse_datetime(self, value: str | None) -> datetime | None:
        if not value:
            return None
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    def _format_number(self, value: float) -> str:
        return f"{value:.6f}".rstrip("0").rstrip(".")
