import tempfile
import unittest
from pathlib import Path

from tradingagents.execution.models import ExecutionConfig, RunMode
from tradingagents.orchestration.runner import TradingCycleRunner
from tradingagents.persistence.sqlite_store import SQLitePersistence
from tradingagents.risk.engine import RiskEngine
from tradingagents.execution.config import load_risk_config


class FakeAnalysisEngine:
    def __init__(self, decision_text: str):
        self.decision_text = decision_text

    def generate(self, symbol: str, analysis_date: str):
        return {"final_trade_decision": self.decision_text}, self.decision_text


class TradingCycleRunnerTests(unittest.TestCase):
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

    def test_happy_path_dry_run(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._make_execution_config(tmpdir)
            store = SQLitePersistence(config.db_path)
            runner = TradingCycleRunner(
                execution_config=config,
                risk_config=load_risk_config(
                    env={
                        "MARKET_HOURS_ONLY": "false",
                        "REQUIRE_MULTIPLE_SIGNALS": "false",
                    }
                ),
                store=store,
                analysis_engine=FakeAnalysisEngine(
                    "Rating: Buy\nExecutive Summary: Fresh demand data and trend confirmation support a disciplined starter position."
                ),
            )

            result = runner.run_cycle(
                symbols=["NVDA"],
                analysis_date="2026-04-11",
                mode=RunMode.DRY_RUN,
                execute=False,
            )

            self.assertEqual(len(result.symbol_results), 1)
            self.assertEqual(result.symbol_results[0].execution_status, "dry-run-approved")
            self.assertEqual(result.executed_count, 1)
            self.assertTrue(Path(result.result_path).exists())
            self.assertEqual(len(store.get_recent_orders(limit=5)), 1)

    def test_rejection_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._make_execution_config(tmpdir)
            store = SQLitePersistence(config.db_path)
            runner = TradingCycleRunner(
                execution_config=config,
                risk_config=load_risk_config(
                    env={"MARKET_HOURS_ONLY": "false", "ALLOWED_SYMBOLS": "AAPL"}
                ),
                store=store,
                analysis_engine=FakeAnalysisEngine("Rating: Buy\nExecutive Summary: Buy now."),
            )

            result = runner.run_cycle(
                symbols=["NVDA"],
                analysis_date="2026-04-11",
                mode=RunMode.DRY_RUN,
                execute=False,
            )

            self.assertEqual(result.symbol_results[0].execution_status, "rejected")
            self.assertEqual(result.executed_count, 0)
            self.assertEqual(store.get_recent_orders(limit=5), [])


if __name__ == "__main__":
    unittest.main()
