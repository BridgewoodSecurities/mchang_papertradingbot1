import tempfile
import unittest
from pathlib import Path

from tradingagents.execution.config import load_risk_config
from tradingagents.execution.models import ExecutionConfig, RunMode, TradeAction
from tradingagents.orchestration.runner import TradingCycleRunner
from tradingagents.persistence.sqlite_store import SQLitePersistence


class FakeAnalysisEngine:
    def generate(self, symbol: str, analysis_date: str):
        decision = """Rating: Sell
        Confidence: 82%
        Executive Summary: Sell NVDA because the setup has deteriorated.
        """
        return {"final_trade_decision": decision}, decision


class TrackingRiskEngine:
    def __init__(self):
        self.calls = 0

    def evaluate(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("Risk engine should not be called for SELL with no position.")


class TrackingArenaEngine:
    def __init__(self):
        self.calls = 0

    def decide(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("Arena should not be called for SELL with no position.")


class SellNoPositionShortCircuitTests(unittest.TestCase):
    def _make_execution_config(self, root: str) -> ExecutionConfig:
        return ExecutionConfig(
            project_dir=root,
            results_dir=str(Path(root) / "results"),
            db_path=str(Path(root) / "runtime" / "test.db"),
            log_dir=str(Path(root) / "runtime" / "logs"),
            audit_dir=str(Path(root) / "runtime" / "audit"),
            paper_trading_enabled=False,
            arena_enabled=False,
        )

    def test_sell_with_no_position_skips_risk_engine(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._make_execution_config(tmpdir)
            store = SQLitePersistence(config.db_path)
            risk_engine = TrackingRiskEngine()
            runner = TradingCycleRunner(
                execution_config=config,
                risk_config=load_risk_config(env={"MARKET_HOURS_ONLY": "false"}),
                store=store,
                analysis_engine=FakeAnalysisEngine(),
                risk_engine=risk_engine,
            )
            arena_engine = TrackingArenaEngine()
            runner.arena_engine = arena_engine

            result = runner.run_cycle(
                symbols=["NVDA"],
                analysis_date="2026-04-11",
                mode=RunMode.DRY_RUN,
                execute=False,
            )

            symbol_result = result.symbol_results[0]
            self.assertEqual(symbol_result.execution_status, "skipped")
            self.assertEqual(symbol_result.arena_decision.action, TradeAction.HOLD)
            self.assertIn(
                "No position to sell; converted SELL",
                " ".join(symbol_result.arena_decision.execution_notes),
            )
            self.assertEqual(risk_engine.calls, 0)
            self.assertEqual(arena_engine.calls, 0)

    def test_reflection_records_non_actionable_sell_lesson(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._make_execution_config(tmpdir)
            store = SQLitePersistence(config.db_path)
            runner = TradingCycleRunner(
                execution_config=config,
                risk_config=load_risk_config(env={"MARKET_HOURS_ONLY": "false"}),
                store=store,
                analysis_engine=FakeAnalysisEngine(),
                risk_engine=TrackingRiskEngine(),
            )
            runner.arena_engine = TrackingArenaEngine()

            result = runner.run_cycle(
                symbols=["NVDA"],
                analysis_date="2026-04-11",
                mode=RunMode.DRY_RUN,
                execute=False,
            )

            reflection = result.symbol_results[0].reflection or {}
            self.assertEqual(
                reflection.get("lesson"),
                "SELL signal with no position is not actionable.",
            )


if __name__ == "__main__":
    unittest.main()
