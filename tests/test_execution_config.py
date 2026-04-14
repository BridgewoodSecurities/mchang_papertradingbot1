import unittest
from unittest.mock import patch

from tradingagents.execution.config import build_analysis_config, load_execution_config, load_risk_config


class ExecutionConfigTests(unittest.TestCase):
    def test_load_execution_config_reads_core_env_vars(self):
        config = load_execution_config(
            env={
                "TRADINGAGENTS_RESULTS_DIR": "./custom-results",
                "EXECUTION_DB_PATH": "./runtime/custom.db",
                "PAPER_TRADING_ENABLED": "true",
                "LOG_LEVEL": "DEBUG",
                "DEFAULT_ORDER_NOTIONAL_USD": "2500",
                "TRADINGAGENTS_LLM_PROVIDER": "google",
                "TRADINGAGENTS_DEEP_THINK_LLM": "gemini-2.5-pro",
                "AGENT_MEMORY_LIMIT": "12",
                "ARENA_ENABLED": "true",
                "WATCHLIST": "SPY,QQQ",
            },
            project_dir="/tmp/project",
            execute=True,
        )

        self.assertEqual(config.project_dir, "/tmp/project")
        self.assertEqual(config.results_dir, "./custom-results")
        self.assertEqual(config.db_path, "./runtime/custom.db")
        self.assertTrue(config.paper_trading_enabled)
        self.assertTrue(config.execute)
        self.assertEqual(config.default_order_notional_usd, 2500.0)
        self.assertEqual(config.agent_memory_limit, 12)
        self.assertTrue(config.arena_enabled)
        self.assertEqual(config.max_trades_per_cycle, 0)
        self.assertEqual(config.llm_config_overrides["llm_provider"], "google")
        self.assertEqual(config.llm_config_overrides["deep_think_llm"], "gemini-2.5-pro")

    def test_load_risk_config_normalizes_allowed_symbols(self):
        config = load_risk_config(
            env={
                "ALLOWED_SYMBOLS": " nvda, aapl ",
                "MAX_ORDER_NOTIONAL_USD": "500",
            }
        )

        self.assertEqual(config.allowed_symbols, ["AAPL", "NVDA"])
        self.assertEqual(config.max_order_notional_usd, 500.0)
        self.assertEqual(config.min_confidence_threshold, 0.65)
        self.assertEqual(config.max_daily_trades, 0)
        self.assertEqual(config.max_daily_trades_per_symbol, 0)
        self.assertTrue(config.require_multiple_signals)

    def test_build_analysis_config_preserves_default_shape(self):
        with patch("tradingagents.execution.config.load_sp500_symbols", return_value=["AAPL", "MSFT"]):
            execution_config = load_execution_config(env={}, project_dir="/tmp/project", execute=False)
        self.assertFalse(execution_config.arena_enabled)
        config = build_analysis_config(execution_config, overrides={"llm_provider": "openai"})
        self.assertIn("results_dir", config)
        self.assertEqual(config["llm_provider"], "openai")
        self.assertEqual(config["data_vendors"]["core_stock_apis"], "alpaca")
        self.assertEqual(config["data_vendors"]["news_data"], "alpaca")

    def test_load_execution_config_defaults_watchlist_to_sp500(self):
        with patch("tradingagents.execution.config.load_sp500_symbols", return_value=["AAPL", "MSFT", "NVDA"]):
            config = load_execution_config(env={}, project_dir="/tmp/project", execute=False)
        self.assertEqual(config.watchlist, ["AAPL", "MSFT", "NVDA"])


if __name__ == "__main__":
    unittest.main()
