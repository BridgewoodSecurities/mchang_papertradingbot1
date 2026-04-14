import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from unittest.mock import MagicMock

from tradingagents.daemon.service import DaemonService
from tradingagents.execution.models import (
    BrokerAccountSnapshot,
    BrokerOrder,
    BrokerPosition,
    DaemonStatus,
    ExecutionConfig,
    RiskDecision,
    RunMode,
    SymbolExecutionResult,
    TradeAction,
    TradingCycleResult,
)
from tradingagents.persistence.sqlite_store import SQLitePersistence
from tradingagents.execution.config import load_risk_config


class FakeRunner:
    def __init__(self, result_factory):
        self.calls = []
        self._result_factory = result_factory
        self.risk_config = load_risk_config(env={"MARKET_HOURS_ONLY": "false"})

    def run_cycle(self, **kwargs):
        self.calls.append(kwargs)
        return self._result_factory(kwargs)


class FakeBroker:
    def __init__(self):
        self.account = BrokerAccountSnapshot(
            account_id="paper",
            cash=10000.0,
            equity=10000.0,
            buying_power=10000.0,
            paper=True,
            status="ACTIVE",
        )
        self.positions = [
            BrokerPosition(
                symbol="AAPL",
                qty=5.0,
                avg_entry_price=100.0,
                market_value=550.0,
                cost_basis=500.0,
                unrealized_pl=50.0,
            )
        ]

    def get_account(self):
        return self.account

    def list_positions(self):
        return self.positions


class DaemonServiceTests(unittest.TestCase):
    def _make_config(self, root: str) -> ExecutionConfig:
        return ExecutionConfig(
            project_dir=root,
            results_dir=str(Path(root) / "results"),
            db_path=str(Path(root) / "runtime" / "daemon.db"),
            log_dir=str(Path(root) / "runtime" / "logs"),
            audit_dir=str(Path(root) / "runtime" / "audit"),
            daemon_pid_path=str(Path(root) / "runtime" / "daemon.pid"),
            daemon_lock_path=str(Path(root) / "runtime" / "daemon.lock"),
            daemon_heartbeat_path=str(Path(root) / "runtime" / "heartbeat.json"),
            kill_switch_path=str(Path(root) / "runtime" / "KILL_SWITCH"),
            daily_summary_dir=str(Path(root) / "results" / "daily"),
            watchlist=["NVDA", "AAPL"],
            max_symbols_per_cycle=2,
            paper_trading_enabled=True,
            market_open_sleep_seconds=15,
            market_closed_sleep_seconds=300,
        )

    def _result_factory(self, kwargs):
        symbols = kwargs["symbols"]
        return TradingCycleResult(
            run_id="run-1",
            mode=RunMode.PAPER,
            trade_date=kwargs["analysis_date"],
            symbols=symbols,
            symbol_results=[
                SymbolExecutionResult(
                    symbol=symbol,
                    execution_status="submitted" if kwargs["execute"] else "dry-run-approved",
                    risk_decision=RiskDecision(approved=True, reasons=["Approved."]),
                    submitted_order=BrokerOrder(
                        id=f"order-{symbol}",
                        symbol=symbol,
                        side=TradeAction.BUY,
                        status="accepted",
                    ),
                )
                for symbol in symbols
            ],
            db_path="./runtime/daemon.db",
            log_path="./runtime/log.log",
            audit_path="./runtime/audit.jsonl",
            result_path="./results/result.json",
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
        )

    @patch("tradingagents.daemon.service.ContextCacheService.fetch_cycle_context")
    def test_duplicate_cycle_prevention_and_restart_recovery(self, fetch_context):
        fetch_context.return_value = {"_global": [], "NVDA": [], "AAPL": []}
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._make_config(tmpdir)
            store = SQLitePersistence(config.db_path)
            runner = FakeRunner(self._result_factory)
            broker = FakeBroker()

            service = DaemonService(
                execution_config=config,
                store=store,
                runner=runner,
                broker=broker,
            )
            now = datetime(2026, 4, 13, 14, 5, tzinfo=timezone.utc)
            first = service.run_once(now=now)
            second = service.run_once(now=now)

            restarted = DaemonService(
                execution_config=config,
                store=store,
                runner=runner,
                broker=broker,
            )
            third = restarted.run_once(now=now)

            self.assertEqual(first["status"], "completed")
            self.assertEqual(second["status"], "already-processed")
            self.assertEqual(third["status"], "already-processed")
            self.assertEqual(len(runner.calls), 1)

    @patch("tradingagents.daemon.service.ContextCacheService.fetch_cycle_context")
    def test_market_hours_gating(self, fetch_context):
        fetch_context.return_value = {"_global": [], "NVDA": []}
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._make_config(tmpdir)
            config.watchlist = ["NVDA"]
            store = SQLitePersistence(config.db_path)
            runner = FakeRunner(self._result_factory)
            service = DaemonService(execution_config=config, store=store, runner=runner, broker=FakeBroker())
            result = service.run_once(now=datetime(2026, 4, 11, 15, 0, tzinfo=timezone.utc))
            self.assertEqual(result["status"], "market-closed")
            self.assertEqual(runner.calls, [])

    @patch("tradingagents.daemon.service.ContextCacheService.fetch_cycle_context")
    def test_kill_switch_disables_execution(self, fetch_context):
        fetch_context.return_value = {"_global": [], "NVDA": []}
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._make_config(tmpdir)
            config.watchlist = ["NVDA"]
            Path(config.kill_switch_path).parent.mkdir(parents=True, exist_ok=True)
            Path(config.kill_switch_path).write_text("halt", encoding="utf-8")
            store = SQLitePersistence(config.db_path)
            runner = FakeRunner(self._result_factory)
            service = DaemonService(execution_config=config, store=store, runner=runner, broker=FakeBroker())
            service.run_once(now=datetime(2026, 4, 13, 14, 0, tzinfo=timezone.utc))
            self.assertFalse(runner.calls[0]["execute"])

    @patch("tradingagents.daemon.service.ContextCacheService.fetch_cycle_context")
    def test_heartbeat_written(self, fetch_context):
        fetch_context.return_value = {"_global": [], "NVDA": []}
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._make_config(tmpdir)
            config.watchlist = ["NVDA"]
            store = SQLitePersistence(config.db_path)
            runner = FakeRunner(self._result_factory)
            service = DaemonService(execution_config=config, store=store, runner=runner, broker=FakeBroker())
            service.run_once(now=datetime(2026, 4, 13, 14, 0, tzinfo=timezone.utc))
            heartbeat_path = Path(config.daemon_heartbeat_path)
            self.assertTrue(heartbeat_path.exists())

    def test_pause_resume_flags(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._make_config(tmpdir)
            store = SQLitePersistence(config.db_path)
            store.set_paused(True)
            self.assertEqual(store.get_daemon_state_value("paused"), 1)
            store.set_paused(False)
            self.assertEqual(store.get_daemon_state_value("paused"), 0)

    def test_is_market_open_helper(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._make_config(tmpdir)
            service = DaemonService(
                execution_config=config,
                store=SQLitePersistence(config.db_path),
                runner=FakeRunner(self._result_factory),
                broker=FakeBroker(),
            )
            self.assertTrue(service._is_market_open(datetime(2026, 4, 13, 14, 0, tzinfo=timezone.utc)))
            self.assertFalse(service._is_market_open(datetime(2026, 4, 13, 1, 0, tzinfo=timezone.utc)))

    def test_market_closed_log_is_throttled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._make_config(tmpdir)
            service = DaemonService(
                execution_config=config,
                store=SQLitePersistence(config.db_path),
                runner=FakeRunner(self._result_factory),
                broker=FakeBroker(),
            )
            logger = MagicMock()
            now = datetime(2026, 4, 13, 1, 0, tzinfo=timezone.utc)
            service._maybe_log_market_closed(logger=logger, now=now)
            service._maybe_log_market_closed(
                logger=logger,
                now=datetime(2026, 4, 13, 1, 1, tzinfo=timezone.utc),
            )
            self.assertEqual(logger.info.call_count, 1)

    def test_daily_summary_generation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._make_config(tmpdir)
            store = SQLitePersistence(config.db_path)
            cycle_id = store.record_cycle_start(
                bucket_start="2026-04-13T14:00:00+00:00",
                started_at=datetime(2026, 4, 13, 14, 0, tzinfo=timezone.utc),
                symbols=["NVDA"],
            )
            store.record_cycle_end(
                cycle_id=cycle_id,
                finished_at=datetime(2026, 4, 13, 14, 2, tzinfo=timezone.utc),
                status="completed",
                summary={
                    "symbol_results": [
                        {
                            "symbol": "NVDA",
                            "execution_status": "submitted",
                            "submitted_order": {"side": "BUY"},
                            "risk_decision": {"reasons": ["Approved."]},
                        }
                    ]
                },
            )
            store.record_daily_pnl_summary(
                run_id="run-1",
                trade_date="2026-04-13",
                equity=10100.0,
                cash=9500.0,
                realized_pnl=50.0,
                unrealized_pnl=25.0,
                gross_exposure=600.0,
                payload={},
            )
            store.record_broker_order(
                run_id="run-1",
                symbol="NVDA",
                order=BrokerOrder(id="1", symbol="NVDA", side=TradeAction.BUY, status="filled"),
                is_new_position=True,
            )
            store.record_fill(run_id="run-1", symbol="NVDA", order_id="1", payload={"symbol": "NVDA"})
            store.upsert_news_items([])
            service = DaemonService(execution_config=config, store=store, runner=FakeRunner(self._result_factory))
            service._maybe_generate_daily_summary(now=datetime(2026, 4, 13, 21, 30, tzinfo=timezone.utc))
            summary_path = Path(config.daily_summary_dir) / "2026-04-13.md"
            self.assertTrue(summary_path.exists())

    def test_status_reports_trade_caps(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._make_config(tmpdir)
            store = SQLitePersistence(config.db_path)
            runner = FakeRunner(self._result_factory)
            service = DaemonService(execution_config=config, store=store, runner=runner, broker=FakeBroker())
            market_now = datetime.now(service.session.tzinfo)
            store.record_broker_order(
                run_id="run-1",
                symbol="NVDA",
                order=BrokerOrder(
                    id="1",
                    symbol="NVDA",
                    side=TradeAction.BUY,
                    status="filled",
                    submitted_at=market_now,
                ),
                is_new_position=True,
            )
            store.record_broker_order(
                run_id="run-2",
                symbol="AAPL",
                order=BrokerOrder(
                    id="2",
                    symbol="AAPL",
                    side=TradeAction.BUY,
                    status="filled",
                    submitted_at=market_now,
                ),
                is_new_position=True,
            )
            status = service.get_status()
            self.assertEqual(status.trades_today, 2)
            self.assertFalse(status.daily_trade_cap_enabled)
            self.assertFalse(status.daily_trade_cap_reached)
            self.assertEqual(status.trades_per_symbol_today["NVDA"], 1)

    def test_select_cycle_watchlist_moves_past_recent_symbols(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._make_config(tmpdir)
            config.watchlist = ["AAA", "BBB", "CCC", "DDD"]
            config.max_symbols_per_cycle = 2
            store = SQLitePersistence(config.db_path)
            for symbol in ["AAA", "BBB"]:
                store.record_symbol_bucket(
                    bucket_key="2026-04-13T14:00:00+00:00",
                    symbol=symbol,
                    cycle_id="cycle-1",
                    run_id="run-1",
                    status="completed",
                    error=None,
                )
            service = DaemonService(
                execution_config=config,
                store=store,
                runner=FakeRunner(self._result_factory),
                broker=FakeBroker(),
            )
            service.runner.risk_config.cooldown_minutes_per_symbol = 60

            selected = service._select_cycle_watchlist(
                now=datetime(2026, 4, 13, 14, 15, tzinfo=timezone.utc)
            )

            self.assertEqual(selected, ["CCC", "DDD"])

    def test_select_cycle_watchlist_uses_time_based_symbol_cooldown(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._make_config(tmpdir)
            config.watchlist = ["AAA", "BBB", "CCC", "DDD", "EEE"]
            config.max_symbols_per_cycle = 3
            store = SQLitePersistence(config.db_path)
            for symbol in ["AAA", "BBB", "CCC"]:
                store.record_symbol_bucket(
                    bucket_key="2026-04-13T14:00:00+00:00",
                    symbol=symbol,
                    cycle_id="cycle-1",
                    run_id="run-1",
                    status="completed",
                    error=None,
                )
            service = DaemonService(
                execution_config=config,
                store=store,
                runner=FakeRunner(self._result_factory),
                broker=FakeBroker(),
            )
            service.runner.risk_config.cooldown_minutes_per_symbol = 120

            selected = service._select_cycle_watchlist(
                now=datetime(2026, 4, 13, 14, 30, tzinfo=timezone.utc)
            )

            self.assertEqual(selected, ["DDD", "EEE"])


if __name__ == "__main__":
    unittest.main()
