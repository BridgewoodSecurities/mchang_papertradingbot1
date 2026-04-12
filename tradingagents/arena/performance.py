from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from tradingagents.execution.models import (
    BrokerAccountSnapshot,
    BrokerPosition,
    ClosedTrade,
    PerformanceSnapshot,
)
from tradingagents.persistence.sqlite_store import SQLitePersistence


class PerformanceTracker:
    def __init__(
        self,
        *,
        store: SQLitePersistence,
        execution_config,
        broker=None,
    ):
        self.store = store
        self.execution_config = execution_config
        self.broker = broker

    def capture_snapshot(
        self,
        *,
        run_id: str,
        cycle_bucket: str | None,
        trade_date: str,
        account: BrokerAccountSnapshot,
        starting_positions: list[BrokerPosition],
        current_positions: list[BrokerPosition],
    ) -> PerformanceSnapshot:
        now = datetime.now(timezone.utc)
        closed_trades = self._detect_closed_trades(
            starting_positions=starting_positions,
            current_positions=current_positions,
            closed_at=now,
        )
        for trade in closed_trades:
            self.store.record_closed_trade(
                agent_id=self.execution_config.agent_id,
                run_id=run_id,
                symbol=trade.symbol,
                realized_pnl=trade.realized_pnl,
                qty=trade.qty,
                entry_price=trade.entry_price,
                exit_price=trade.exit_price,
                payload=trade.model_dump(mode="json"),
                closed_at=trade.exit_time.isoformat(),
            )

        valuations = [self._conservative_position_value(position) for position in current_positions]
        gross_exposure = sum(item["market_value"] for item in valuations)
        unrealized_pnl = sum(item["unrealized_pnl"] for item in valuations)
        realized_total = self._sum_realized_pnl()
        account_value = float(account.cash) + gross_exposure
        total_pnl = realized_total + unrealized_pnl
        stats = self._trade_statistics()
        max_drawdown = self._compute_max_drawdown(account_value)

        payload = {
            "trade_date": trade_date,
            "cycle_bucket": cycle_bucket,
            "account_value": account_value,
            "cash": float(account.cash),
            "gross_exposure": gross_exposure,
            "realized_pnl": realized_total,
            "unrealized_pnl": unrealized_pnl,
            "total_pnl": total_pnl,
            "win_rate": stats["win_rate"],
            "average_win": stats["average_win"],
            "average_loss": stats["average_loss"],
            "max_drawdown": max_drawdown,
            "trade_frequency": stats["trade_frequency"],
            "open_positions": len(current_positions),
            "closed_trades": [trade.model_dump(mode="json") for trade in closed_trades],
        }
        snapshot = PerformanceSnapshot(**payload, payload=payload)
        self.store.record_performance_snapshot(
            agent_id=self.execution_config.agent_id,
            run_id=run_id,
            cycle_bucket=cycle_bucket,
            trade_date=trade_date,
            account_value=account_value,
            cash=float(account.cash),
            gross_exposure=gross_exposure,
            realized_pnl=realized_total,
            unrealized_pnl=unrealized_pnl,
            total_pnl=total_pnl,
            win_rate=stats["win_rate"],
            average_win=stats["average_win"],
            average_loss=stats["average_loss"],
            max_drawdown=max_drawdown,
            trade_frequency=stats["trade_frequency"],
            open_positions=len(current_positions),
            payload=snapshot.model_dump(mode="json"),
        )
        self.store.record_daily_pnl_summary(
            run_id=run_id,
            trade_date=trade_date,
            equity=account_value,
            cash=float(account.cash),
            realized_pnl=realized_total,
            unrealized_pnl=unrealized_pnl,
            gross_exposure=gross_exposure,
            payload=snapshot.model_dump(mode="json"),
        )
        return snapshot

    def _detect_closed_trades(
        self,
        *,
        starting_positions: list[BrokerPosition],
        current_positions: list[BrokerPosition],
        closed_at: datetime,
    ) -> list[ClosedTrade]:
        current_map = {position.symbol: position for position in current_positions}
        trades: list[ClosedTrade] = []
        for starting in starting_positions:
            current_qty = current_map.get(starting.symbol).qty if starting.symbol in current_map else 0.0
            closed_qty = max(0.0, starting.qty - current_qty)
            if closed_qty <= 0:
                continue
            exit_price = self._conservative_price(starting.symbol)
            entry_price = float(starting.avg_entry_price or 0.0)
            if entry_price <= 0 or exit_price <= 0:
                continue
            trades.append(
                ClosedTrade(
                    symbol=starting.symbol,
                    qty=closed_qty,
                    entry_price=entry_price,
                    exit_price=exit_price,
                    realized_pnl=(exit_price - entry_price) * closed_qty,
                    exit_time=closed_at,
                    notes=["Closed trade inferred from position change across the cycle."],
                )
            )
        return trades

    def _conservative_position_value(self, position: BrokerPosition) -> dict[str, float]:
        price = self._conservative_price(position.symbol)
        market_value = price * position.qty
        entry_price = float(position.avg_entry_price or 0.0)
        unrealized = (price - entry_price) * position.qty if entry_price > 0 else 0.0
        return {
            "symbol": position.symbol,
            "market_value": market_value,
            "unrealized_pnl": unrealized,
        }

    def _conservative_price(self, symbol: str) -> float:
        bid_loader = getattr(self.broker, "get_latest_bid_price", None)
        if callable(bid_loader):
            try:
                bid = bid_loader(symbol)
                if bid and bid > 0:
                    return float(bid)
            except Exception:
                pass

        latest_price = 0.0
        if self.broker is not None:
            try:
                latest_price = float(self.broker.get_latest_price(symbol))
            except Exception:
                latest_price = 0.0
        spread_discount = 1.0 - (self.execution_config.conservative_spread_bps / 10000.0)
        return max(0.0, latest_price * spread_discount)

    def _sum_realized_pnl(self) -> float:
        trades = self.store.get_recent_closed_trades(
            agent_id=self.execution_config.agent_id,
            limit=1000,
        )
        return float(sum(item.get("realized_pnl", 0.0) for item in trades))

    def _trade_statistics(self) -> dict[str, float | None]:
        trades = self.store.get_recent_closed_trades(
            agent_id=self.execution_config.agent_id,
            limit=1000,
        )
        if not trades:
            return {
                "win_rate": None,
                "average_win": None,
                "average_loss": None,
                "trade_frequency": 0.0,
            }
        wins = [item["realized_pnl"] for item in trades if item.get("realized_pnl", 0.0) > 0]
        losses = [item["realized_pnl"] for item in trades if item.get("realized_pnl", 0.0) <= 0]
        trade_dates = {str(item.get("closed_at", ""))[:10] for item in trades if item.get("closed_at")}
        return {
            "win_rate": len(wins) / len(trades),
            "average_win": (sum(wins) / len(wins)) if wins else None,
            "average_loss": (sum(losses) / len(losses)) if losses else None,
            "trade_frequency": len(trades) / max(len(trade_dates), 1),
        }

    def _compute_max_drawdown(self, latest_account_value: float) -> float | None:
        history = self.store.get_recent_performance_snapshots(
            agent_id=self.execution_config.agent_id,
            limit=500,
        )
        values = [float(item.get("account_value", 0.0)) for item in reversed(history)]
        values.append(latest_account_value)
        peak = None
        max_drawdown = 0.0
        for value in values:
            if peak is None or value > peak:
                peak = value
            if peak and peak > 0:
                drawdown = (peak - value) / peak
                max_drawdown = max(max_drawdown, drawdown)
        return max_drawdown if peak is not None else None
