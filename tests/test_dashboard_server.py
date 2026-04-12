import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen

from tradingagents.dashboard.server import DashboardDataService, DashboardServer
from tradingagents.daemon.service import DaemonService
from tradingagents.execution.config import load_risk_config
from tradingagents.execution.models import (
    BrokerAccountSnapshot,
    BrokerOrder,
    BrokerPosition,
    DaemonHeartbeat,
    ExecutionConfig,
    NewsItem,
    TradeAction,
)
from tradingagents.persistence.sqlite_store import SQLitePersistence


class FakeRunner:
    def __init__(self):
        self.risk_config = load_risk_config(env={"MARKET_HOURS_ONLY": "false"})


class FakeBroker:
    def get_account(self):
        return BrokerAccountSnapshot(
            account_id="paper-123",
            status="ACTIVE",
            cash=9500.0,
            equity=10125.0,
            buying_power=10125.0,
            paper=True,
        )

    def list_positions(self):
        return [
            BrokerPosition(
                symbol="NVDA",
                qty=5.0,
                avg_entry_price=160.0,
                market_value=900.0,
                cost_basis=800.0,
                unrealized_pl=100.0,
            )
        ]


class DashboardServerTests(unittest.TestCase):
    def _make_config(self, root: str) -> ExecutionConfig:
        return ExecutionConfig(
            project_dir=root,
            results_dir=str(Path(root) / "results"),
            db_path=str(Path(root) / "runtime" / "dashboard.db"),
            log_dir=str(Path(root) / "runtime" / "logs"),
            audit_dir=str(Path(root) / "runtime" / "audit"),
            daemon_pid_path=str(Path(root) / "runtime" / "daemon.pid"),
            daemon_lock_path=str(Path(root) / "runtime" / "daemon.lock"),
            daemon_heartbeat_path=str(Path(root) / "runtime" / "daemon-heartbeat.json"),
            kill_switch_path=str(Path(root) / "runtime" / "KILL_SWITCH"),
            daily_summary_dir=str(Path(root) / "results" / "daily"),
            watchlist=["NVDA", "AAPL"],
            max_symbols_per_cycle=2,
            paper_trading_enabled=True,
        )

    def _build_data_service(self, root: str) -> DashboardDataService:
        config = self._make_config(root)
        store = SQLitePersistence(config.db_path)
        Path(config.log_dir).mkdir(parents=True, exist_ok=True)
        Path(config.results_dir).mkdir(parents=True, exist_ok=True)
        Path(config.audit_dir).mkdir(parents=True, exist_ok=True)
        Path(config.daemon_heartbeat_path).write_text(
            json.dumps(
                {
                    "pid": 4321,
                    "status": "idle",
                    "last_heartbeat_at": datetime.now(timezone.utc).isoformat(),
                    "last_cycle_bucket": "2026-04-13T14:00:00+00:00",
                    "symbols_processed": ["NVDA"],
                }
            ),
            encoding="utf-8",
        )
        (Path(config.log_dir) / "daemon.stdout.log").write_text(
            "daemon_starting\nsymbol_start NVDA\nparsed_decision BUY\n",
            encoding="utf-8",
        )
        (Path(config.log_dir) / "dry-run.log").write_text(
            "run_start\nrun_complete\n",
            encoding="utf-8",
        )

        store.record_daemon_start(pid=4321, hostname="localhost")
        store.update_daemon_state(
            DaemonHeartbeat(
                pid=4321,
                status="idle",
                last_heartbeat_at=datetime.now(timezone.utc),
                last_cycle_started_at=datetime(2026, 4, 13, 14, 0, tzinfo=timezone.utc),
                last_cycle_completed_at=datetime(2026, 4, 13, 14, 2, tzinfo=timezone.utc),
                last_cycle_bucket="2026-04-13T14:00:00+00:00",
                symbols_processed=["NVDA"],
            )
        )
        store.record_agent_decision(
            agent_id=config.agent_id,
            run_id="run-1",
            cycle_bucket="2026-04-13T14:00:00+00:00",
            symbol="NVDA",
            action="BUY",
            confidence=0.82,
            payload={
                "symbol": "NVDA",
                "action": "BUY",
                "confidence": 0.82,
                "reasoning": "Fresh news plus trend confirmation.",
                "supporting_signals": ["fresh news catalyst", "price/trend confirmation"],
            },
        )
        store.record_agent_reflection(
            agent_id=config.agent_id,
            run_id="run-1",
            cycle_bucket="2026-04-13T14:00:00+00:00",
            symbol="NVDA",
            payload={
                "symbol": "NVDA",
                "what_changed": "Momentum improved.",
                "lesson": "High-conviction entries work better when the catalyst is new.",
            },
        )
        store.upsert_learning_state(
            agent_id=config.agent_id,
            summary={
                "learning_summary": "Avoid weak trades; wait for fresh catalysts and confirmation.",
                "recurring_mistakes": ["Chasing weak setups."],
                "recurring_success_patterns": ["Fresh catalyst plus trend confirmation."],
                "recent_lessons": ["Patience improves entries."],
            },
        )
        store.record_daily_pnl_summary(
            run_id="run-1",
            trade_date="2026-04-13",
            equity=10125.0,
            cash=9500.0,
            realized_pnl=80.0,
            unrealized_pnl=100.0,
            gross_exposure=900.0,
            payload={},
        )
        store.record_broker_order(
            run_id="run-1",
            symbol="NVDA",
            order=BrokerOrder(
                id="order-1",
                symbol="NVDA",
                side=TradeAction.BUY,
                status="filled",
                qty=5.0,
                submitted_at=datetime.now(timezone.utc),
            ),
            is_new_position=True,
        )
        cycle_id = store.record_cycle_start(
            bucket_start="2026-04-13T14:00:00+00:00",
            started_at=datetime(2026, 4, 13, 14, 0, tzinfo=timezone.utc),
            symbols=["NVDA"],
        )
        store.record_cycle_end(
            cycle_id=cycle_id,
            finished_at=datetime(2026, 4, 13, 14, 2, tzinfo=timezone.utc),
            status="completed",
            summary={"symbol_results": [{"symbol": "NVDA", "execution_status": "submitted"}]},
        )
        store.record_run(
            run_id="run-1",
            mode="daemon",
            trade_date="2026-04-13",
            symbols=["NVDA"],
            started_at=datetime(2026, 4, 13, 14, 0, tzinfo=timezone.utc),
            finished_at=datetime(2026, 4, 13, 14, 2, tzinfo=timezone.utc),
            status="completed",
            summary={"executed_count": 1},
            result_path=str(Path(config.results_dir) / "run-1.json"),
            audit_path=str(Path(config.audit_dir) / "run-1.jsonl"),
        )
        store.record_daemon_error("temporary network error")
        store.upsert_news_items(
            [
                NewsItem(
                    symbol="NVDA",
                    title="NVIDIA wins another hyperscaler deal",
                    source="UnitTest Wire",
                    url="https://example.com/nvda",
                    published_at=datetime(2026, 4, 13, 13, 45, tzinfo=timezone.utc),
                    summary="A new AI infrastructure catalyst.",
                    content_hash="news-1",
                )
            ]
        )
        daemon_service = DaemonService(
            execution_config=config,
            store=store,
            runner=FakeRunner(),
            broker=FakeBroker(),
        )
        return DashboardDataService(
            execution_config=config,
            store=store,
            daemon_service=daemon_service,
            refresh_seconds=3,
        )

    def test_build_snapshot_contains_monitoring_sections(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            service = self._build_data_service(tmpdir)
            snapshot = service.build_snapshot()

            self.assertEqual(snapshot["refresh_seconds"], 3)
            self.assertIn("daemon_status", snapshot["overview"])
            self.assertEqual(snapshot["account"]["account_id"], "paper-123")
            self.assertEqual(snapshot["recent_orders"][0]["symbol"], "NVDA")
            self.assertIn("NVDA", snapshot["symbol_memory"])
            self.assertIn("daemon_stdout", snapshot["logs"])
            self.assertTrue(service.render_index().startswith("<!doctype html>"))

    def test_http_server_serves_index_and_overview(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_service = self._build_data_service(tmpdir)
            server = DashboardServer(
                data_service=data_service,
                host="127.0.0.1",
                port=0,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.httpd.server_address[1]
            try:
                with urlopen(f"http://127.0.0.1:{port}/") as response:
                    html = response.read().decode("utf-8")
                with urlopen(f"http://127.0.0.1:{port}/api/overview") as response:
                    payload = json.loads(response.read().decode("utf-8"))
            finally:
                server.shutdown()
                thread.join(timeout=2)

            self.assertIn("TradingBot Monitor", html)
            self.assertEqual(payload["account"]["account_id"], "paper-123")
            self.assertEqual(payload["recent_news"][0]["symbol"], "NVDA")


if __name__ == "__main__":
    unittest.main()
