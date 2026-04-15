from __future__ import annotations

from datetime import timezone
import logging
from typing import Any
from urllib.parse import urlparse, urlunparse

import requests

from tradingagents.execution.models import BrokerOrder, ExecutionConfig


class BridgewoodReporterError(Exception):
    """Raised when Bridgewood reporting fails."""


def normalize_bridgewood_api_base(value: str) -> str:
    parsed = urlparse(value.strip())
    if not parsed.scheme or not parsed.netloc:
        raise BridgewoodReporterError("BRIDGEWOOD_API_BASE must be a full URL.")

    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        normalized_path = path
    elif path:
        normalized_path = f"{path}/v1"
    else:
        normalized_path = "/v1"

    return urlunparse(
        parsed._replace(path=normalized_path, params="", query="", fragment="")
    )


class BridgewoodReporter:
    def __init__(
        self,
        *,
        api_base: str,
        agent_api_key: str,
        timeout: float = 10.0,
        session: requests.Session | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        if not api_base:
            raise BridgewoodReporterError("BRIDGEWOOD_API_BASE is required.")
        if not agent_api_key:
            raise BridgewoodReporterError("BRIDGEWOOD_AGENT_API_KEY is required.")

        self.api_base = normalize_bridgewood_api_base(api_base)
        self.agent_api_key = agent_api_key
        self.timeout = timeout
        self.session = session or requests.Session()
        self.logger = logger or logging.getLogger(__name__)

    @classmethod
    def is_configured(cls, config: ExecutionConfig) -> bool:
        return bool(config.bridgewood_api_base and config.bridgewood_agent_api_key)

    @classmethod
    def from_execution_config(
        cls,
        config: ExecutionConfig,
        *,
        session: requests.Session | None = None,
        logger: logging.Logger | None = None,
    ) -> "BridgewoodReporter":
        return cls(
            api_base=config.bridgewood_api_base or "",
            agent_api_key=config.bridgewood_agent_api_key or "",
            timeout=config.bridgewood_request_timeout_seconds,
            session=session,
            logger=logger,
        )

    def verify_agent(self) -> dict[str, Any]:
        response = self.session.get(
            f"{self.api_base}/me",
            headers=self._headers(),
            timeout=self.timeout,
        )
        return self._parse_response(response, action="verify agent")

    def report_filled_order(self, order: BrokerOrder) -> dict[str, Any]:
        execution = self._build_execution(order)
        response = self.session.post(
            f"{self.api_base}/executions",
            headers=self._headers(include_json=True),
            json={"executions": [execution]},
            timeout=self.timeout,
        )
        payload = self._parse_response(response, action="report execution")
        self.logger.info(
            "bridgewood_execution_reported",
            extra={
                "symbol": order.symbol,
                "order_id": order.id,
                "client_order_id": order.client_order_id,
            },
        )
        return payload

    def _build_execution(self, order: BrokerOrder) -> dict[str, Any]:
        status = order.status.lower()
        if status != "filled":
            raise BridgewoodReporterError(
                "Bridgewood only accepts fully filled orders, "
                f"got status={order.status!r}."
            )
        if not order.filled_qty or not order.filled_avg_price:
            raise BridgewoodReporterError(
                "Bridgewood reporting requires filled_qty and filled_avg_price."
            )

        external_order_id = order.id or order.client_order_id
        if not external_order_id:
            raise BridgewoodReporterError(
                "Bridgewood reporting requires an Alpaca order id or client_order_id."
            )

        return {
            "external_order_id": external_order_id,
            "symbol": order.symbol,
            "side": order.side.value.lower(),
            "quantity": order.filled_qty,
            "price": order.filled_avg_price,
            "fees": 0,
            "executed_at": self._resolve_executed_at(order),
        }

    def _resolve_executed_at(self, order: BrokerOrder) -> str:
        raw = order.raw or {}
        for candidate in ("filled_at", "updated_at", "submitted_at"):
            value = raw.get(candidate)
            if value:
                return str(value)

        if order.submitted_at is None:
            raise BridgewoodReporterError(
                "Bridgewood reporting requires a fill timestamp from Alpaca."
            )

        submitted_at = order.submitted_at
        if submitted_at.tzinfo is None:
            submitted_at = submitted_at.replace(tzinfo=timezone.utc)
        return submitted_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _headers(self, *, include_json: bool = False) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.agent_api_key}"}
        if include_json:
            headers["Content-Type"] = "application/json"
        return headers

    def _parse_response(
        self,
        response: requests.Response,
        *,
        action: str,
    ) -> dict[str, Any]:
        try:
            response.raise_for_status()
        except requests.RequestException as exc:
            detail = ""
            try:
                payload = response.json()
                detail = payload.get("detail") or payload.get("code") or response.text
            except ValueError:
                detail = response.text
            raise BridgewoodReporterError(
                f"Bridgewood {action} failed ({response.status_code}): {detail}"
            ) from exc

        try:
            return response.json()
        except ValueError as exc:
            raise BridgewoodReporterError(
                f"Bridgewood {action} returned invalid JSON."
            ) from exc
