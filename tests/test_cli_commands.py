import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from unittest.mock import MagicMock

from typer.testing import CliRunner

from cli.main import app
from tradingagents.execution.models import RunMode, TradingCycleResult


class FakeRunner:
    def __init__(self, result):
        self.result = result

    def run_cycle(self, **kwargs):
        return self.result


class FakeReplayRunner:
    def __init__(self, result):
        self.result = result

    def run(self, **kwargs):
        return self.result


class CliCommandTests(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()
        self.result_payload = TradingCycleResult(
            run_id="run-1",
            mode=RunMode.DRY_RUN,
            trade_date="2026-04-11",
            symbols=["NVDA"],
            symbol_results=[],
            db_path="./runtime/tradingagents.db",
            log_path="./runtime/logs/run-1.log",
            audit_path="./runtime/audit/run-1.jsonl",
            result_path="./results/run-1.json",
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
        )

    @patch("cli.main._build_trading_cycle_runner_with_overrides")
    def test_dry_run_command(self, build_runner):
        build_runner.return_value = FakeRunner(self.result_payload)
        result = self.runner.invoke(
            app,
            ["dry-run", "--symbols", "NVDA,AAPL", "--date", "2026-04-11"],
        )
        self.assertEqual(result.exit_code, 0)
        self.assertIn("DB:", result.output)

    @patch("cli.main._build_trading_cycle_runner_with_overrides")
    def test_dry_run_command_passes_provider_overrides(self, build_runner):
        build_runner.return_value = FakeRunner(self.result_payload)
        result = self.runner.invoke(
            app,
            [
                "dry-run",
                "--symbols",
                "NVDA",
                "--date",
                "2026-04-11",
                "--llm-provider",
                "google",
                "--deep-model",
                "gemini-2.5-pro",
                "--quick-model",
                "gemini-2.5-flash",
            ],
        )
        self.assertEqual(result.exit_code, 0)
        kwargs = build_runner.call_args.kwargs
        self.assertEqual(kwargs["llm_overrides"]["llm_provider"], "google")
        self.assertEqual(kwargs["llm_overrides"]["deep_think_llm"], "gemini-2.5-pro")

    @patch("cli.main._build_replay_runner_with_overrides")
    def test_replay_command(self, build_runner):
        build_runner.return_value = (FakeReplayRunner(self.result_payload), None)
        result = self.runner.invoke(
            app,
            ["replay", "--symbols", "NVDA", "--from", "2026-03-01", "--to", "2026-03-31"],
        )
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Results:", result.output)

    @patch("cli.main.DashboardServer")
    @patch("cli.main._build_dashboard_data_service")
    def test_dashboard_run_command(self, build_data_service, dashboard_server):
        build_data_service.return_value = MagicMock()
        dashboard_server.return_value = MagicMock()
        result = self.runner.invoke(
            app,
            ["dashboard", "run", "--host", "127.0.0.1", "--port", "8123", "--refresh-seconds", "7"],
        )
        self.assertEqual(result.exit_code, 0)
        build_data_service.assert_called_once_with(refresh_seconds=7)
        dashboard_server.assert_called_once()
        dashboard_server.return_value.serve_forever.assert_called_once()
        self.assertIn("Dashboard listening on http://127.0.0.1:8123", result.output)


if __name__ == "__main__":
    unittest.main()
