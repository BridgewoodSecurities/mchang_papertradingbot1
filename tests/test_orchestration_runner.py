import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

from tradingagents.execution.models import (
    BrokerAccountSnapshot,
    BrokerOrder,
    ExecutionConfig,
    RunMode,
    TradeAction,
)
from tradingagents.orchestration.runner import TradingCycleRunner
from tradingagents.persistence.sqlite_store import SQLitePersistence
from tradingagents.execution.config import load_risk_config


class FakeAnalysisEngine:
    def __init__(self, decision_text: str):
        self.decision_text = decision_text

    def generate(self, symbol: str, analysis_date: str):
        return {"final_trade_decision": self.decision_text}, self.decision_text


class FakeBroker:
    def __init__(self, *, orders: list[BrokerOrder] | None = None):
        self._orders = orders or []

    def get_account(self) -> BrokerAccountSnapshot:
        return BrokerAccountSnapshot(
            account_id="paper",
            cash=100000.0,
            equity=100000.0,
            buying_power=100000.0,
            paper=True,
        )

    def list_positions(self):
        return []

    def list_open_orders(self):
        return []

    def list_orders(self, *, status: str = "all", limit: int = 50):
        return list(self._orders)[:limit]


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

    def test_reconciles_delayed_fill_and_reports_bridgewood(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._make_execution_config(tmpdir)
            config.bridgewood_api_base = "https://bridgewood.onrender.com/v1"
            config.bridgewood_agent_api_key = "bgw_test"
            store = SQLitePersistence(config.db_path)
            pending_order = BrokerOrder(
                id="order-1",
                client_order_id="cid-1",
                symbol="NVDA",
                side=TradeAction.BUY,
                status="pending_new",
                notional_usd=500.0,
                submitted_at=datetime(2026, 4, 14, 15, 45, tzinfo=timezone.utc),
            )
            store.record_broker_order(
                run_id="previous-run",
                symbol="NVDA",
                order=pending_order,
                is_new_position=True,
            )
            broker = FakeBroker(
                orders=[
                    BrokerOrder(
                        id="order-1",
                        client_order_id="cid-1",
                        symbol="NVDA",
                        side=TradeAction.BUY,
                        status="filled",
                        notional_usd=500.0,
                        filled_qty=4.0,
                        filled_avg_price=125.0,
                        submitted_at=datetime(2026, 4, 14, 15, 45, tzinfo=timezone.utc),
                        raw={"filled_at": "2026-04-14T15:45:05Z"},
                    )
                ]
            )
            runner = TradingCycleRunner(
                execution_config=config,
                risk_config=load_risk_config(
                    env={
                        "MARKET_HOURS_ONLY": "false",
                        "REQUIRE_MULTIPLE_SIGNALS": "false",
                    }
                ),
                store=store,
                analysis_engine=FakeAnalysisEngine("Rating: Hold\nExecutive Summary: Wait."),
                broker=broker,
            )
            runner.bridgewood = MagicMock()

            runner.run_cycle(
                symbols=["NVDA"],
                analysis_date="2026-04-14",
                mode=RunMode.DRY_RUN,
                execute=False,
            )

            self.assertTrue(store.has_fill(order_id="order-1"))
            self.assertEqual(store.get_last_broker_order(symbol="NVDA")["status"], "filled")
            runner.bridgewood.report_filled_order.assert_called_once()


if __name__ == "__main__":
    unittest.main()
