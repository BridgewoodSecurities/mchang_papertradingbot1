from __future__ import annotations

from abc import ABC, abstractmethod

from tradingagents.execution.models import BrokerAccountSnapshot, BrokerOrder, BrokerPosition, OrderIntent


class BrokerError(RuntimeError):
    """Base broker exception."""


class BaseBroker(ABC):
    @abstractmethod
    def get_account(self) -> BrokerAccountSnapshot:
        raise NotImplementedError

    @abstractmethod
    def list_positions(self) -> list[BrokerPosition]:
        raise NotImplementedError

    @abstractmethod
    def list_open_orders(self) -> list[BrokerOrder]:
        raise NotImplementedError

    @abstractmethod
    def list_orders(self, *, status: str = "all", limit: int = 50) -> list[BrokerOrder]:
        raise NotImplementedError

    @abstractmethod
    def submit_order(self, intent: OrderIntent, *, client_order_id: str) -> BrokerOrder:
        raise NotImplementedError

    @abstractmethod
    def get_latest_price(self, symbol: str) -> float:
        raise NotImplementedError
