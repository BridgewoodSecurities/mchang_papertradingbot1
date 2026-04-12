from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from tradingagents.arena import AgentMemoryService, ArenaDecisionEngine, PerformanceTracker
from tradingagents.brokers.base import BaseBroker
from tradingagents.execution.config import build_analysis_config
from tradingagents.execution.logging_utils import AuditTrail, setup_logging
from tradingagents.execution.models import (
    AgentReflection,
    BrokerAccountSnapshot,
    BrokerOrder,
    BrokerPosition,
    ExecutionConfig,
    OrderIntent,
    PerformanceSnapshot,
    RunMode,
    SymbolExecutionResult,
    TradeAction,
    TradingCycleResult,
)
from tradingagents.execution.parser import DecisionParser
from tradingagents.execution.policy import ExecutionPolicy
from tradingagents.persistence.sqlite_store import SQLitePersistence
from tradingagents.risk.engine import RiskEngine


class TradingAgentsAnalysisEngine:
    def __init__(self, execution_config: ExecutionConfig):
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        analysis_config = build_analysis_config(execution_config)
        self.graph = TradingAgentsGraph(
            selected_analysts=execution_config.selected_analysts,
            debug=False,
            config=analysis_config,
        )

    def generate(self, symbol: str, analysis_date: str) -> tuple[dict, str]:
        final_state, _ = self.graph.propagate(symbol, analysis_date)
        return final_state, final_state["final_trade_decision"]


class TradingCycleRunner:
    def __init__(
        self,
        *,
        execution_config: ExecutionConfig,
        risk_config,
        store: SQLitePersistence,
        analysis_engine: TradingAgentsAnalysisEngine,
        parser: DecisionParser | None = None,
        risk_engine: RiskEngine | None = None,
        broker: BaseBroker | None = None,
    ):
        self.execution_config = execution_config
        self.store = store
        self.analysis_engine = analysis_engine
        self.parser = parser or DecisionParser()
        self.risk_config = risk_config
        self.risk_engine = risk_engine or RiskEngine(risk_config)
        self.broker = broker
        self.policy = ExecutionPolicy(execution_config)
        self.memory = AgentMemoryService(
            store=store,
            agent_id=execution_config.agent_id,
            memory_limit=execution_config.agent_memory_limit,
        )
        self.arena_engine = ArenaDecisionEngine(execution_config, risk_config)
        self.performance = PerformanceTracker(
            store=store,
            execution_config=execution_config,
            broker=broker,
        )

    def run_cycle(
        self,
        *,
        symbols: list[str],
        analysis_date: str,
        mode: RunMode,
        execute: bool,
        cycle_bucket: str | None = None,
        cycle_context: dict | None = None,
        cycle_timestamp: datetime | None = None,
    ) -> TradingCycleResult:
        started_at = cycle_timestamp or datetime.now(timezone.utc)
        run_id = f"{mode.value}-{analysis_date}-{uuid4().hex[:8]}"
        logger, log_path = setup_logging(self.execution_config, run_id=run_id)
        audit_path = Path(self.execution_config.audit_dir) / f"{run_id}.jsonl"
        audit = AuditTrail(audit_path)
        results_dir = Path(self.execution_config.results_dir)
        results_dir.mkdir(parents=True, exist_ok=True)
        result_path = results_dir / f"{run_id}.json"

        self.store.record_run(
            run_id=run_id,
            mode=mode.value,
            trade_date=analysis_date,
            symbols=symbols,
            status="running",
            started_at=started_at,
            result_path=str(result_path),
            audit_path=str(audit_path),
        )

        execute_enabled = execute and mode in {RunMode.PAPER, RunMode.DAEMON}
        if execute_enabled and not self.execution_config.paper_trading_enabled:
            raise ValueError(
                "PAPER_TRADING_ENABLED=true is required before paper execution can submit orders."
            )

        logger.info("run_start", extra={"run_id": run_id, "mode": mode.value, "symbols": symbols})
        audit.write("run_start", run_id=run_id, mode=mode.value, symbols=symbols, analysis_date=analysis_date)

        account, positions, open_orders = self._load_broker_state(mode=mode)
        self.store.snapshot_equity(
            run_id=run_id,
            equity=account.equity,
            cash=account.cash,
            payload=account.model_dump(mode="json"),
        )
        self.store.snapshot_positions(
            run_id=run_id,
            payload=[position.model_dump(mode="json") for position in positions],
        )

        results: list[SymbolExecutionResult] = []
        positions_map = {position.symbol: position for position in positions}
        processed_symbols: set[str] = set()
        cycle_trade_count = 0

        for symbol in symbols:
            try:
                symbol = symbol.upper()
                if self.execution_config.one_order_per_symbol_per_cycle and symbol in processed_symbols:
                    results.append(
                        SymbolExecutionResult(
                            symbol=symbol,
                            execution_status="skipped",
                            error="Duplicate symbol skipped by one-order-per-symbol policy.",
                        )
                    )
                    continue

                processed_symbols.add(symbol)
                logger.info("symbol_start", extra={"run_id": run_id, "symbol": symbol})
                final_state, raw_decision_text = self.analysis_engine.generate(symbol, analysis_date)
                self.store.record_raw_decision(run_id=run_id, symbol=symbol, raw_text=raw_decision_text)
                audit.write("raw_decision", run_id=run_id, symbol=symbol, text=raw_decision_text)

                parsed = self.parser.parse(raw_decision_text, default_symbol=symbol)
                self.store.record_parsed_decision(
                    run_id=run_id,
                    symbol=symbol,
                    payload=parsed.model_dump(mode="json"),
                )
                logger.info(
                    "parsed_decision",
                    extra={"run_id": run_id, "symbol": symbol, "payload": parsed.model_dump(mode="json")},
                )
                audit.write("parsed_decision", run_id=run_id, symbol=symbol, payload=parsed.model_dump(mode="json"))

                base_intent = self._select_primary_intent(parsed, symbol=symbol)
                latest_price = self._get_latest_price(symbol)
                if base_intent is not None:
                    base_intent = self.policy.resolve(
                        base_intent,
                        account=account,
                        positions=positions_map,
                        latest_price=latest_price,
                    )
                memory_snapshot = self.memory.build_snapshot(symbol=symbol)
                arena_intent, reflection = self.arena_engine.decide(
                    symbol=symbol,
                    analysis_date=analysis_date,
                    raw_decision_text=raw_decision_text,
                    parsed_decision=parsed,
                    base_intent=base_intent,
                    cycle_inputs=self._build_cycle_inputs(
                        symbol=symbol,
                        cycle_timestamp=started_at,
                        cycle_context=cycle_context or {},
                        account=account,
                        positions=list(positions_map.values()),
                        latest_price=latest_price,
                    ),
                    memory_snapshot=memory_snapshot,
                )
                recent_symbol_actions = self._recent_symbol_actions(symbol=symbol)
                recent_trade_count = self._recent_trade_count(symbol=None, now=started_at)
                recent_symbol_trade_count = self._recent_trade_count(symbol=symbol, now=started_at)
                last_trade = self.store.get_last_broker_order(symbol=symbol)
                last_exit_at = self.store.get_last_exit_time(
                    agent_id=self.execution_config.agent_id,
                    symbol=symbol,
                )

                risk_decision = self.risk_engine.evaluate(
                    arena_intent,
                    account=account,
                    positions=list(positions_map.values()),
                    open_orders=open_orders,
                    latest_price=latest_price,
                    existing_daily_pnl=0.0,
                    new_positions_today=self.store.count_new_positions_for_date(
                        trade_date=analysis_date
                    ),
                    daily_trade_count=self.store.count_trades_for_date(
                        trade_date=analysis_date
                    ),
                    symbol_daily_trade_count=self.store.count_trades_for_date(
                        trade_date=analysis_date,
                        symbol=symbol,
                    ),
                    cycle_trade_count=cycle_trade_count,
                    recent_trade_count=recent_trade_count,
                    recent_symbol_trade_count=recent_symbol_trade_count,
                    last_order_at=self.store.get_last_order_time(symbol=symbol),
                    last_trade=last_trade,
                    last_exit_at=last_exit_at,
                    recent_symbol_actions=recent_symbol_actions,
                    now=started_at,
                )
                self.memory.record_decision(
                    run_id=run_id,
                    cycle_bucket=cycle_bucket,
                    intent=arena_intent,
                )
                self.store.record_risk_decision(
                    run_id=run_id,
                    symbol=symbol,
                    payload=risk_decision.model_dump(mode="json"),
                )
                audit.write("risk_decision", run_id=run_id, symbol=symbol, payload=risk_decision.model_dump(mode="json"))

                order = None
                status = "rejected"
                warnings = list(parsed.warnings)

                if risk_decision.approved:
                    if execute_enabled:
                        order = self._submit_order(
                            run_id=run_id,
                            intent=risk_decision.normalized_intent or arena_intent,
                            symbol=symbol,
                            is_new_position=symbol not in positions_map or positions_map[symbol].qty <= 0,
                        )
                        open_orders.append(order)
                        status = "submitted"
                    else:
                        order = self._build_simulated_order(
                            intent=risk_decision.normalized_intent or arena_intent,
                            latest_price=latest_price,
                        )
                        self.store.record_broker_order(
                            run_id=run_id,
                            symbol=symbol,
                            order=order,
                            is_new_position=symbol not in positions_map or positions_map[symbol].qty <= 0,
                        )
                        status = "dry-run-approved"
                    cycle_trade_count += 1
                else:
                    reflection = self._augment_reflection(
                        reflection=reflection,
                        risk_decision=risk_decision,
                    )

                learning_state = self.memory.record_reflection(
                    run_id=run_id,
                    cycle_bucket=cycle_bucket,
                    reflection=reflection,
                )
                results.append(
                    SymbolExecutionResult(
                        symbol=symbol,
                        raw_decision_text=raw_decision_text,
                        parsed_decision=parsed,
                        arena_decision=arena_intent,
                        risk_decision=risk_decision,
                        submitted_order=order,
                        execution_status=status,
                        latest_price=latest_price,
                        warnings=warnings,
                        reflection=reflection.model_dump(mode="json"),
                    )
                )
                logger.info(
                    "symbol_complete",
                    extra={"run_id": run_id, "symbol": symbol, "status": status},
                )
            except Exception as exc:
                logger.exception("symbol_failed", extra={"run_id": run_id, "symbol": symbol})
                normalized_error = self._format_execution_error(exc)
                audit.write("symbol_failed", run_id=run_id, symbol=symbol, error=normalized_error)
                results.append(
                    SymbolExecutionResult(
                        symbol=symbol,
                        execution_status="error",
                        error=normalized_error,
                    )
                )

        finished_at = datetime.now(timezone.utc)
        performance_snapshot = self._capture_performance_snapshot(
            run_id=run_id,
            cycle_bucket=cycle_bucket,
            analysis_date=analysis_date,
            starting_account=account,
            starting_positions=positions,
            mode=mode,
        )
        learning_state = self.memory.get_learning_state() or {}
        result = TradingCycleResult(
            run_id=run_id,
            mode=mode,
            trade_date=analysis_date,
            symbols=symbols,
            symbol_results=results,
            db_path=str(self.store.db_path),
            log_path=str(log_path),
            audit_path=str(audit_path),
            result_path=str(result_path),
            started_at=started_at,
            finished_at=finished_at,
            learning_summary=learning_state.get("learning_summary"),
            performance_snapshot=performance_snapshot,
        )

        result_path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        if self.broker is not None and mode in {RunMode.PAPER, RunMode.DAEMON}:
            self._record_post_cycle_snapshots(run_id=run_id)
        self.store.record_run(
            run_id=run_id,
            mode=mode.value,
            trade_date=analysis_date,
            symbols=symbols,
            status="completed",
            started_at=started_at,
            finished_at=finished_at,
            summary=result.model_dump(mode="json"),
            result_path=str(result_path),
            audit_path=str(audit_path),
        )
        logger.info("run_complete", extra={"run_id": run_id, "executed_count": result.executed_count})
        audit.write("run_complete", run_id=run_id, result=result.model_dump(mode="json"))
        return result

    def _capture_performance_snapshot(
        self,
        *,
        run_id: str,
        cycle_bucket: str | None,
        analysis_date: str,
        starting_account: BrokerAccountSnapshot,
        starting_positions: list[BrokerPosition],
        mode: RunMode,
    ) -> PerformanceSnapshot:
        if self.broker is not None and mode in {RunMode.PAPER, RunMode.DAEMON}:
            try:
                account = self.broker.get_account()
                positions = self.broker.list_positions()
            except Exception:
                account = starting_account
                positions = starting_positions
        else:
            account = starting_account
            positions = starting_positions
        return self.performance.capture_snapshot(
            run_id=run_id,
            cycle_bucket=cycle_bucket,
            trade_date=analysis_date,
            account=account,
            starting_positions=starting_positions,
            current_positions=positions,
        )

    def _record_post_cycle_snapshots(self, *, run_id: str) -> None:
        try:
            account = self.broker.get_account() if self.broker is not None else None
            positions = self.broker.list_positions() if self.broker is not None else []
        except Exception:
            return
        if account is not None:
            self.store.snapshot_equity(
                run_id=run_id,
                equity=account.equity,
                cash=account.cash,
                payload=account.model_dump(mode="json"),
            )
        self.store.snapshot_positions(
            run_id=run_id,
            payload=[position.model_dump(mode="json") for position in positions],
        )

    def _load_broker_state(
        self, *, mode: RunMode
    ) -> tuple[BrokerAccountSnapshot, list[BrokerPosition], list[BrokerOrder]]:
        if self.broker is not None and mode in {RunMode.PAPER, RunMode.DAEMON}:
            return (
                self.broker.get_account(),
                self.broker.list_positions(),
                self.broker.list_open_orders(),
            )

        simulated_account = BrokerAccountSnapshot(
            account_id="SIMULATED",
            status="ACTIVE",
            currency="USD",
            cash=self.execution_config.simulated_starting_equity,
            equity=self.execution_config.simulated_starting_equity,
            buying_power=self.execution_config.simulated_starting_equity,
            portfolio_value=self.execution_config.simulated_starting_equity,
            paper=True,
            raw={"mode": "simulated"},
        )
        return simulated_account, [], []

    def _select_primary_intent(self, parsed, *, symbol: str) -> OrderIntent | None:
        matching = [intent for intent in parsed.intents if intent.symbol == symbol]
        if matching:
            return matching[0]
        if len(parsed.intents) == 1:
            return parsed.intents[0]
        return None

    def _get_latest_price(self, symbol: str) -> float | None:
        if self.broker is not None:
            try:
                return self.broker.get_latest_price(symbol)
            except Exception:
                return None
        return None

    def _submit_order(
        self,
        *,
        run_id: str,
        intent: OrderIntent,
        symbol: str,
        is_new_position: bool,
    ) -> BrokerOrder:
        if self.broker is None:
            raise RuntimeError("Broker is required for paper execution.")
        client_order_id = f"ta-{run_id[:8]}-{symbol.lower()}-{intent.action.value.lower()}"
        order = self.broker.submit_order(intent, client_order_id=client_order_id)
        self.store.record_broker_order(
            run_id=run_id,
            symbol=symbol,
            order=order,
            is_new_position=is_new_position,
        )
        self.store.record_broker_event(
            run_id=run_id,
            symbol=symbol,
            event_type="order_submitted",
            payload=order.model_dump(mode="json"),
        )
        if order.filled_qty:
            self.store.record_fill(
                run_id=run_id,
                symbol=symbol,
                order_id=order.id,
                payload=order.model_dump(mode="json"),
            )
        return order

    def _build_simulated_order(self, *, intent: OrderIntent, latest_price: float | None) -> BrokerOrder:
        return BrokerOrder(
            id=f"sim-{uuid4().hex[:10]}",
            client_order_id=f"sim-{intent.symbol.lower()}-{intent.action.value.lower()}",
            symbol=intent.symbol,
            side=intent.action,
            order_type=intent.order_type,
            status="simulated",
            qty=intent.quantity,
            notional_usd=intent.notional_usd,
            limit_price=intent.limit_price,
            filled_qty=intent.quantity,
            filled_avg_price=latest_price,
            submitted_at=datetime.now(timezone.utc),
            raw={"simulated": True},
        )

    def _format_execution_error(self, exc: Exception) -> str:
        message = str(exc)
        lowered = message.lower()
        if "insufficient_quota" in lowered or "exceeded your current quota" in lowered:
            return (
                "LLM provider quota/billing error for the configured model. "
                "Add API billing or switch providers/models via "
                "TRADINGAGENTS_LLM_PROVIDER, TRADINGAGENTS_DEEP_THINK_LLM, and "
                "TRADINGAGENTS_QUICK_THINK_LLM."
            )
        return message

    def _build_cycle_inputs(
        self,
        *,
        symbol: str,
        cycle_timestamp: datetime,
        cycle_context: dict,
        account: BrokerAccountSnapshot,
        positions: list[BrokerPosition],
        latest_price: float | None,
    ) -> dict:
        trades_today = self.store.count_trades_for_date(
            trade_date=cycle_timestamp.astimezone(timezone.utc).date().isoformat()
        )
        recent_trade_count = self._recent_trade_count(symbol=None, now=cycle_timestamp)
        recent_symbol_trade_count = self._recent_trade_count(symbol=symbol, now=cycle_timestamp)
        open_position = self._open_position_context(symbol=symbol, positions=positions, now=cycle_timestamp)
        last_trade = self.store.get_last_broker_order(symbol=symbol)
        last_exit_at = self.store.get_last_exit_time(
            agent_id=self.execution_config.agent_id,
            symbol=symbol,
        )
        last_action_time = self.store.get_last_order_time(symbol=symbol)
        return {
            "timestamp": cycle_timestamp.isoformat(),
            "latest_price": latest_price,
            "trades_today": trades_today,
            "recent_trade_count": recent_trade_count,
            "recent_symbol_trade_count": recent_symbol_trade_count,
            "approaching_daily_trade_cap": trades_today >= max(0, self.risk_config.max_daily_trades - 1),
            "portfolio": {
                "cash": account.cash,
                "equity": account.equity,
                "buying_power": account.buying_power,
                "positions": [position.model_dump(mode="json") for position in positions],
                "gross_exposure": sum(abs(position.market_value or 0.0) for position in positions),
            },
            "recent_pnl": self.store.get_recent_pnl(limit=5),
            "news": (cycle_context.get("_global") or []) + (cycle_context.get(symbol) or []),
            "open_position": open_position,
            "last_trade": last_trade,
            "cooldowns": {
                "last_action_time": last_action_time.isoformat() if last_action_time else None,
                "position_reentry_cooldown_active": self._cooldown_active(
                    reference=last_exit_at,
                    now=cycle_timestamp,
                    hours=self.risk_config.position_reentry_cooldown_hours,
                ),
                "reversal_cooldown_active": self._cooldown_active(
                    reference=self._parse_trade_time(last_trade),
                    now=cycle_timestamp,
                    hours=self.risk_config.reversal_cooldown_hours,
                )
                if last_trade and str(last_trade.get("side", "")).upper() == "SELL"
                else False,
            },
        }

    def _recent_symbol_actions(self, *, symbol: str) -> list[str]:
        decisions = self.store.get_recent_agent_decisions(
            agent_id=self.execution_config.agent_id,
            symbol=symbol,
            limit=10,
        )
        return [str(item.get("action", "")).upper() for item in decisions if item.get("action")]

    def _augment_reflection(
        self,
        *,
        reflection: AgentReflection,
        risk_decision,
    ) -> AgentReflection:
        if risk_decision.approved:
            return reflection
        incorrect = list(reflection.incorrect_signals)
        incorrect.extend(risk_decision.reasons[:2])
        lesson = reflection.lesson or "Weak or stale theses should resolve to HOLD rather than a trade."
        if any("Flip-flop guard triggered" in reason for reason in risk_decision.reasons):
            lesson = "Avoid flip-flopping in the same symbol; wait for materially new information before re-entering."
        return reflection.model_copy(
            update={
                "incorrect_signals": incorrect[:5],
                "lesson": lesson,
            }
        )

    def _recent_trade_count(self, *, symbol: str | None, now: datetime) -> int:
        lookback_start = now - timedelta(minutes=self.risk_config.recent_trade_lookback_minutes)
        return self.store.count_recent_trades(since=lookback_start, symbol=symbol)

    def _open_position_context(
        self,
        *,
        symbol: str,
        positions: list[BrokerPosition],
        now: datetime,
    ) -> dict:
        position = next((item for item in positions if item.symbol == symbol and item.qty > 0), None)
        if position is None:
            return {
                "has_open_position": False,
            }
        last_buy = self.store.get_last_broker_order(symbol=symbol, side="BUY")
        entry_time = self._parse_trade_time(last_buy)
        return {
            "has_open_position": True,
            "qty": position.qty,
            "entry_price": position.avg_entry_price,
            "current_unrealized_pnl": position.unrealized_pl,
            "time_since_entry_minutes": (now - entry_time).total_seconds() / 60.0 if entry_time else None,
        }

    def _parse_trade_time(self, trade: dict | None) -> datetime | None:
        if not trade:
            return None
        submitted_at = trade.get("submitted_at")
        if not submitted_at:
            return None
        return datetime.fromisoformat(str(submitted_at))

    def _cooldown_active(
        self,
        *,
        reference: datetime | None,
        now: datetime,
        hours: int,
    ) -> bool:
        if reference is None or hours <= 0:
            return False
        return (now - reference).total_seconds() < hours * 3600
