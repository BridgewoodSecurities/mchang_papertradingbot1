import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tradingagents.arena.decider import ArenaDecisionEngine
from tradingagents.arena.memory import AgentMemoryService
from tradingagents.arena.performance import PerformanceTracker
from tradingagents.execution.models import (
    AgentMemorySnapshot,
    AgentReflection,
    BrokerAccountSnapshot,
    BrokerPosition,
    ExecutionConfig,
    OrderIntent,
    ParsedDecisionResult,
    RiskConfig,
    TradeAction,
)
from tradingagents.persistence.sqlite_store import SQLitePersistence


class FakeBroker:
    def get_latest_price(self, symbol: str) -> float:
        return 110.0


class ArenaServiceTests(unittest.TestCase):
    def _make_config(self, root: str) -> ExecutionConfig:
        return ExecutionConfig(
            project_dir=root,
            results_dir=str(Path(root) / "results"),
            db_path=str(Path(root) / "runtime" / "arena.db"),
            log_dir=str(Path(root) / "runtime" / "logs"),
            audit_dir=str(Path(root) / "runtime" / "audit"),
            agent_id="arena-test",
            arena_enabled=False,
            market_open_time="09:30",
            market_close_time="16:00",
        )

    def test_memory_service_builds_learning_summary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._make_config(tmpdir)
            store = SQLitePersistence(config.db_path)
            memory = AgentMemoryService(
                store=store,
                agent_id=config.agent_id,
                memory_limit=10,
            )
            intent = OrderIntent(
                symbol="NVDA",
                action=TradeAction.BUY,
                confidence=0.8,
                rationale="Structured thesis.",
                expected_edge="Demand revisions are underpriced.",
                why_market_wrong="Consensus is behind the latest demand data.",
                is_new_information=True,
                fits_success_patterns=True,
                position_sizing_rationale="Starter size only.",
                source_raw_text="Rating: Buy",
            )
            memory.record_decision(run_id="run-1", cycle_bucket="bucket-1", intent=intent)
            summary = memory.record_reflection(
                run_id="run-1",
                cycle_bucket="bucket-1",
                reflection=AgentReflection(
                    symbol="NVDA",
                    what_changed="Earnings revisions improved.",
                    correct_signals=["Revisions acceleration"],
                    incorrect_signals=[],
                    lesson="High-conviction revisions signals deserve more weight.",
                    current_reasoning="Structured thesis.",
                    action_taken=TradeAction.BUY,
                    confidence=0.8,
                ),
            )

            snapshot = memory.build_snapshot(symbol="NVDA")
            self.assertEqual(len(snapshot.recent_decisions), 1)
            self.assertIn("High-conviction revisions", snapshot.learning_summary)
            self.assertIn("High-conviction revisions", summary["learning_summary"])

    def test_performance_tracker_detects_closed_trade(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._make_config(tmpdir)
            store = SQLitePersistence(config.db_path)
            tracker = PerformanceTracker(
                store=store,
                execution_config=config,
                broker=FakeBroker(),
            )
            account = BrokerAccountSnapshot(
                account_id="paper",
                cash=10000.0,
                equity=10000.0,
                buying_power=10000.0,
                paper=True,
            )
            starting_positions = [
                BrokerPosition(
                    symbol="NVDA",
                    qty=5.0,
                    avg_entry_price=100.0,
                    market_value=500.0,
                    cost_basis=500.0,
                    unrealized_pl=0.0,
                )
            ]
            snapshot = tracker.capture_snapshot(
                run_id="run-1",
                cycle_bucket="2026-04-13T14:00:00+00:00",
                trade_date="2026-04-13",
                account=account,
                starting_positions=starting_positions,
                current_positions=[],
            )
            closed_trades = store.get_recent_closed_trades(agent_id=config.agent_id, limit=5)
            self.assertEqual(len(closed_trades), 1)
            self.assertGreater(closed_trades[0]["realized_pnl"], 0.0)
            self.assertEqual(snapshot.open_positions, 0)

    def test_arena_decider_fallback_defaults_to_structured_fields(self):
        config = ExecutionConfig(arena_enabled=False)
        decider = ArenaDecisionEngine(config, RiskConfig(market_hours_only=False))
        base_intent = OrderIntent(
            symbol="NVDA",
            action=TradeAction.BUY,
            confidence=0.8,
            rationale="Buy only if fresh earnings revision data and trend confirmation both remain intact.",
            expected_edge="Fresh estimate revisions and trend confirmation suggest the market is still underpricing demand durability.",
            why_market_wrong="The market is still anchored to stale slowdown fears despite stronger recent evidence.",
            risks=["Headline risk", "Volatility risk"],
            supporting_signals=["fresh news catalyst", "price/trend confirmation"],
            is_new_information=True,
            position_sizing_rationale="Use a small starter position until the breakout holds.",
            source_raw_text="Rating: Buy",
        )
        memory_snapshot = AgentMemorySnapshot(
            symbol="NVDA",
            recent_losing_trades=[{"symbol": "NVDA", "realized_pnl": -10.0}],
        )

        intent, reflection = decider.decide(
            symbol="NVDA",
            analysis_date="2026-04-13",
            raw_decision_text="Rating: Buy",
            parsed_decision=ParsedDecisionResult(raw_text="Rating: Buy"),
            base_intent=base_intent,
            cycle_inputs={
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "news": [{"seen_before": False}],
                "portfolio": {"gross_exposure": 1000.0, "equity": 10000.0},
            },
            memory_snapshot=memory_snapshot,
        )

        self.assertTrue(intent.expected_edge)
        self.assertTrue(intent.position_sizing_rationale)
        self.assertTrue(intent.contradicts_recent_failures)
        self.assertGreaterEqual(len(intent.supporting_signals), 2)
        self.assertEqual(reflection.action_taken, intent.action)

    def test_prompt_includes_hold_bias_and_trade_context(self):
        config = self._make_config("/tmp")
        decider = ArenaDecisionEngine(config, RiskConfig(market_hours_only=False))
        prompt = decider._build_prompt(
            symbol="NVDA",
            analysis_date="2026-04-13",
            raw_decision_text="Rating: Buy",
            parsed_decision=ParsedDecisionResult(raw_text="Rating: Buy"),
            base_intent=None,
            cycle_inputs={
                "timestamp": "2026-04-13T14:00:00+00:00",
                "latest_price": 100.0,
                "trades_today": 1,
                "recent_trade_count": 1,
                "recent_symbol_trade_count": 1,
                "approaching_daily_trade_cap": True,
                "open_position": {"has_open_position": True, "entry_price": 95.0},
                "last_trade": {"side": "BUY"},
                "cooldowns": {"position_reentry_cooldown_active": True},
                "portfolio": {},
                "recent_pnl": [],
                "news": [],
            },
            memory_snapshot=AgentMemorySnapshot(symbol="NVDA"),
        )
        self.assertIn("HOLD is preferred when uncertain", prompt)
        self.assertIn("Do not trade just because a cycle occurred", prompt)
        self.assertIn("Trades today", prompt)
        self.assertIn("Open position context", prompt)


if __name__ == "__main__":
    unittest.main()
