from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timezone

import pandas as pd
import yfinance as yf
from zoneinfo import ZoneInfo

from tradingagents.execution.logging_utils import AuditTrail, setup_logging
from tradingagents.execution.models import (
    BrokerAccountSnapshot,
    BrokerOrder,
    BrokerPosition,
    ExecutionConfig,
    OrderIntent,
    RunMode,
    SymbolExecutionResult,
    TradeAction,
    TradingCycleResult,
)
from tradingagents.execution.parser import DecisionParser
from tradingagents.execution.policy import ExecutionPolicy
from tradingagents.persistence.sqlite_store import SQLitePersistence
from tradingagents.risk.engine import RiskEngine


class ReplayRunner:
    def __init__(
        self,
        *,
        execution_config: ExecutionConfig,
        risk_config,
        store: SQLitePersistence,
        analysis_engine,
        parser: DecisionParser | None = None,
        risk_engine: RiskEngine | None = None,
    ):
        self.execution_config = execution_config
        self.store = store
        self.analysis_engine = analysis_engine
        self.parser = parser or DecisionParser()
        self.risk_engine = risk_engine or RiskEngine(risk_config)
        self.policy = ExecutionPolicy(execution_config)

    def run(self, *, symbols: list[str], from_date: str, to_date: str) -> TradingCycleResult:
        started_at = datetime.now(timezone.utc)
        run_id = f"replay-{from_date}-{to_date}"
        logger, log_path = setup_logging(self.execution_config, run_id=run_id)
        audit_path = self.store.db_path.parent / "audit" / f"{run_id}.jsonl"
        audit = AuditTrail(audit_path)

        cash = self.execution_config.simulated_starting_equity
        positions: dict[str, dict[str, float]] = {}
        results: list[SymbolExecutionResult] = []
        realized_pnl = 0.0

        history = {
            symbol: self._load_history(symbol, from_date=from_date, to_date=to_date)
            for symbol in symbols
        }

        for current_day in pd.date_range(from_date, to_date, freq="B"):
            trade_date = current_day.date().isoformat()
            for symbol in symbols:
                frame = history[symbol]
                if current_day.normalize() not in frame.index:
                    continue

                close_price = float(frame.loc[current_day.normalize(), "Close"])
                account = self._build_account_snapshot(cash=cash, positions=positions, history=history, trade_day=current_day)
                broker_positions = self._build_positions(positions=positions, history=history, trade_day=current_day)
                raw_state, raw_decision = self.analysis_engine.generate(symbol, trade_date)
                parsed = self.parser.parse(raw_decision, default_symbol=symbol)
                intent = self._select_intent(parsed, symbol=symbol)
                if intent is None:
                    results.append(
                        SymbolExecutionResult(
                            symbol=symbol,
                            raw_decision_text=raw_decision,
                            parsed_decision=parsed,
                            execution_status="replay-skip",
                            warnings=["Parser produced no actionable replay intent."],
                            latest_price=close_price,
                        )
                    )
                    continue

                mapped_positions = {position.symbol: position for position in broker_positions}
                intent = self.policy.resolve(intent, positions=mapped_positions, latest_price=close_price)
                risk = self.risk_engine.evaluate(
                    intent,
                    account=account,
                    positions=broker_positions,
                    open_orders=[],
                    latest_price=close_price,
                    existing_daily_pnl=0.0,
                    new_positions_today=0,
                    now=datetime.combine(
                        current_day.date(),
                        time(hour=15, minute=45),
                        tzinfo=ZoneInfo("America/New_York"),
                    ).astimezone(timezone.utc),
                )

                order = None
                status = "replay-rejected"
                if risk.approved:
                    fill_price, fill_date = self._resolve_fill(frame, current_day.normalize())
                    if fill_price is not None:
                        cash, realized_change = self._apply_fill(
                            positions=positions,
                            symbol=symbol,
                            intent=intent,
                            fill_price=fill_price,
                            cash=cash,
                        )
                        realized_pnl += realized_change
                        order = BrokerOrder(
                            id=f"replay-{symbol}-{trade_date}",
                            client_order_id=f"replay-{symbol.lower()}-{trade_date}",
                            symbol=symbol,
                            side=intent.action,
                            status="simulated_filled",
                            qty=intent.quantity,
                            notional_usd=intent.notional_usd,
                            limit_price=intent.limit_price,
                            filled_qty=intent.quantity,
                            filled_avg_price=fill_price,
                            submitted_at=datetime.combine(fill_date, time.min, tzinfo=timezone.utc),
                            raw={"fill_assumption": self.execution_config.replay_fill_assumption},
                        )
                        status = "replay-filled"

                results.append(
                    SymbolExecutionResult(
                        symbol=symbol,
                        raw_decision_text=raw_decision,
                        parsed_decision=parsed,
                        risk_decision=risk,
                        submitted_order=order,
                        execution_status=status,
                        latest_price=close_price,
                    )
                )

            daily_positions = self._build_positions(positions=positions, history=history, trade_day=current_day)
            unrealized = sum(position.unrealized_pl or 0.0 for position in daily_positions)
            gross_exposure = sum(abs(position.market_value or 0.0) for position in daily_positions)
            equity = cash + gross_exposure
            self.store.record_daily_pnl_summary(
                run_id=run_id,
                trade_date=trade_date,
                equity=equity,
                cash=cash,
                realized_pnl=realized_pnl,
                unrealized_pnl=unrealized,
                gross_exposure=gross_exposure,
                payload={
                    "positions": [position.model_dump(mode="json") for position in daily_positions],
                    "fill_assumption": self.execution_config.replay_fill_assumption,
                    "slippage_bps": self.execution_config.slippage_bps,
                    "commission_per_order": self.execution_config.commission_per_order,
                },
            )
            audit.write(
                "replay_day_complete",
                trade_date=trade_date,
                equity=equity,
                cash=cash,
                gross_exposure=gross_exposure,
            )
            logger.info("replay_day_complete", extra={"trade_date": trade_date, "equity": equity})

        finished_at = datetime.now(timezone.utc)
        result_path = self.store.db_path.parent / "replay" / f"{run_id}.json"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result = TradingCycleResult(
            run_id=run_id,
            mode=RunMode.REPLAY,
            trade_date=to_date,
            symbols=symbols,
            symbol_results=results,
            db_path=str(self.store.db_path),
            log_path=str(log_path),
            audit_path=str(audit_path),
            result_path=str(result_path),
            started_at=started_at,
            finished_at=finished_at,
        )
        result_path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        return result

    def _load_history(self, symbol: str, *, from_date: str, to_date: str) -> pd.DataFrame:
        frame = yf.download(
            tickers=symbol,
            start=from_date,
            end=(pd.Timestamp(to_date) + pd.Timedelta(days=7)).date().isoformat(),
            interval="1d",
            auto_adjust=False,
            progress=False,
        )
        if frame.empty:
            raise ValueError(f"No historical price data available for {symbol}.")
        frame.index = pd.to_datetime(frame.index).normalize()
        return frame

    def _select_intent(self, parsed, *, symbol: str) -> OrderIntent | None:
        for intent in parsed.intents:
            if intent.symbol == symbol:
                return intent
        return parsed.intents[0] if parsed.intents else None

    def _resolve_fill(self, history: pd.DataFrame, trade_day: pd.Timestamp) -> tuple[float | None, date]:
        if self.execution_config.replay_fill_assumption == "same_day_close":
            return float(history.loc[trade_day, "Close"]), trade_day.date()
        next_rows = history.loc[history.index > trade_day]
        if next_rows.empty:
            return None, trade_day.date()
        fill_row = next_rows.iloc[0]
        fill_price = float(fill_row["Open"])
        return fill_price, next_rows.index[0].date()

    def _apply_fill(
        self,
        *,
        positions: dict[str, dict[str, float]],
        symbol: str,
        intent: OrderIntent,
        fill_price: float,
        cash: float,
    ) -> tuple[float, float]:
        slippage_multiplier = 1 + (self.execution_config.slippage_bps / 10_000.0)
        commission = self.execution_config.commission_per_order
        realized_change = 0.0

        if intent.action == TradeAction.BUY:
            quantity = intent.quantity
            if quantity is None and intent.notional_usd is not None:
                quantity = intent.notional_usd / fill_price
            if quantity is None:
                return cash, realized_change
            buy_price = fill_price * slippage_multiplier
            total_cost = quantity * buy_price + commission
            cash -= total_cost
            current = positions.get(symbol, {"qty": 0.0, "avg_cost": 0.0})
            new_qty = current["qty"] + quantity
            current_cost = current["qty"] * current["avg_cost"]
            positions[symbol] = {
                "qty": new_qty,
                "avg_cost": (current_cost + quantity * buy_price) / new_qty,
            }
            return cash, realized_change

        if intent.action == TradeAction.SELL and symbol in positions:
            quantity = intent.quantity or positions[symbol]["qty"]
            quantity = min(quantity, positions[symbol]["qty"])
            sell_price = fill_price * (1 - (self.execution_config.slippage_bps / 10_000.0))
            proceeds = quantity * sell_price - commission
            cash += proceeds
            realized_change = quantity * (sell_price - positions[symbol]["avg_cost"]) - commission
            positions[symbol]["qty"] -= quantity
            if positions[symbol]["qty"] <= 1e-9:
                positions.pop(symbol, None)
            return cash, realized_change

        return cash, realized_change

    def _build_account_snapshot(
        self,
        *,
        cash: float,
        positions: dict[str, dict[str, float]],
        history: dict[str, pd.DataFrame],
        trade_day: pd.Timestamp,
    ) -> BrokerAccountSnapshot:
        market_value = 0.0
        for symbol, position in positions.items():
            if trade_day.normalize() in history[symbol].index:
                market_value += position["qty"] * float(history[symbol].loc[trade_day.normalize(), "Close"])
        equity = cash + market_value
        return BrokerAccountSnapshot(
            account_id="REPLAY",
            status="ACTIVE",
            cash=cash,
            equity=equity,
            buying_power=cash,
            portfolio_value=equity,
            paper=True,
            raw={"mode": "replay"},
        )

    def _build_positions(
        self,
        *,
        positions: dict[str, dict[str, float]],
        history: dict[str, pd.DataFrame],
        trade_day: pd.Timestamp,
    ) -> list[BrokerPosition]:
        broker_positions: list[BrokerPosition] = []
        for symbol, position in positions.items():
            if trade_day.normalize() not in history[symbol].index:
                continue
            close_price = float(history[symbol].loc[trade_day.normalize(), "Close"])
            market_value = position["qty"] * close_price
            cost_basis = position["qty"] * position["avg_cost"]
            broker_positions.append(
                BrokerPosition(
                    symbol=symbol,
                    qty=position["qty"],
                    avg_entry_price=position["avg_cost"],
                    market_value=market_value,
                    cost_basis=cost_basis,
                    unrealized_pl=market_value - cost_basis,
                    side="long",
                )
            )
        return broker_positions
