from __future__ import annotations

from math import floor

from tradingagents.execution.models import (
    BrokerAccountSnapshot,
    BrokerPosition,
    ExecutionConfig,
    OrderIntent,
    OrderType,
    TradeAction,
)


class ExecutionPolicy:
    """Apply safe execution defaults after parsing but before risk checks."""

    def __init__(self, config: ExecutionConfig):
        self.config = config

    def resolve(
        self,
        intent: OrderIntent,
        *,
        account: BrokerAccountSnapshot,
        positions: dict[str, BrokerPosition],
        latest_price: float | None,
    ) -> OrderIntent:
        resolved = intent.model_copy(deep=True)

        if resolved.action == TradeAction.HOLD:
            return resolved

        if resolved.order_type is None:
            resolved.order_type = self.config.order_type

        if resolved.action == TradeAction.BUY:
            self._resolve_buy(resolved, account=account, latest_price=latest_price)
        elif resolved.action == TradeAction.SELL:
            self._resolve_sell(resolved, positions=positions, latest_price=latest_price)

        return resolved

    def _resolve_buy(
        self,
        intent: OrderIntent,
        *,
        account: BrokerAccountSnapshot,
        latest_price: float | None,
    ) -> None:
        if intent.notional_usd is None and intent.quantity is None:
            target_notional = min(
                account.equity * self.config.default_position_size_pct,
                self.config.max_order_notional_usd,
            )
            if target_notional < self.config.min_order_notional_usd:
                intent.execution_notes.append(
                    "Target notional fell below minimum order notional."
                )
            else:
                intent.notional_usd = target_notional
                intent.execution_notes.append(
                    f"Applied equity-based default buy notional of ${target_notional:.2f}."
                )

        if (
            not self.config.allow_fractional_shares
            and latest_price
            and intent.quantity is None
            and intent.notional_usd is not None
        ):
            quantity = floor(intent.notional_usd / latest_price)
            if quantity < 1:
                intent.notional_usd = None
                intent.execution_notes.append(
                    "Target notional is too small to purchase one whole share."
                )
            else:
                intent.quantity = float(quantity)
                intent.notional_usd = quantity * latest_price
                intent.execution_notes.append(
                    "Rounded buy sizing down to a whole-share quantity."
                )

        if intent.order_type == OrderType.LIMIT and intent.limit_price is None and latest_price:
            intent.limit_price = latest_price
            intent.execution_notes.append(
                f"Defaulted limit price to latest reference price ${latest_price:.2f}."
            )

        if (
            intent.order_type == OrderType.LIMIT
            and intent.quantity is None
            and intent.notional_usd is not None
            and intent.limit_price
        ):
            intent.quantity = intent.notional_usd / intent.limit_price
            intent.execution_notes.append("Converted notional sizing to share quantity for limit order.")

    def _resolve_sell(
        self,
        intent: OrderIntent,
        *,
        positions: dict[str, BrokerPosition],
        latest_price: float | None,
    ) -> None:
        position = positions.get(intent.symbol)
        if position is None or position.qty <= 0:
            return

        if intent.quantity is None:
            if (intent.source_rating or "").upper() == "UNDERWEIGHT":
                intent.quantity = position.qty * self.config.underweight_sell_fraction
                intent.execution_notes.append(
                    f"Applied underweight reduction fraction of {self.config.underweight_sell_fraction:.2f}."
                )
            elif self.config.sell_entire_position_on_sell:
                intent.quantity = position.qty
                intent.execution_notes.append("Configured policy set SELL to exit the full long position.")

        if intent.notional_usd is None and intent.quantity is not None and latest_price is not None:
            intent.notional_usd = intent.quantity * latest_price

        if not self.config.allow_fractional_shares and intent.quantity is not None:
            rounded = floor(intent.quantity)
            if rounded < 1:
                intent.quantity = None
                intent.notional_usd = None
                intent.execution_notes.append("Sell quantity rounded below one whole share.")
            else:
                intent.quantity = float(rounded)
