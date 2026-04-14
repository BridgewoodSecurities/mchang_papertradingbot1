import tempfile
import unittest
from pathlib import Path

from tradingagents.persistence.sqlite_store import SQLitePersistence


class CounterfactualTrackingTests(unittest.TestCase):
    def _make_store(self):
        tmpdir = tempfile.TemporaryDirectory()
        store = SQLitePersistence(str(Path(tmpdir.name) / "runtime" / "test.db"))
        return tmpdir, store

    def test_record_and_retrieve_counterfactual(self):
        tmpdir, store = self._make_store()
        with tmpdir:
            store.record_counterfactual(
                run_id="run-1",
                symbol="NVDA",
                trade_date="2026-04-10",
                original_action="BUY",
                original_confidence=0.72,
                final_action="HOLD",
                price_at_decision=100.0,
                override_reason="Risk engine required more signals.",
            )

            items = store.get_recent_counterfactuals(symbol="NVDA", limit=5)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["original_action"], "BUY")
            self.assertEqual(items[0]["final_action"], "HOLD")

    def test_update_prices_computes_would_have_pnl(self):
        tmpdir, store = self._make_store()
        with tmpdir:
            store.record_counterfactual(
                run_id="run-1",
                symbol="NVDA",
                trade_date="2026-04-10",
                original_action="BUY",
                original_confidence=0.72,
                final_action="HOLD",
                price_at_decision=100.0,
                override_reason="Arena overrode to HOLD.",
            )

            store.update_counterfactual_prices(
                symbol="NVDA",
                trade_date="2026-04-10",
                price_after_1d=110.0,
                price_after_5d=95.0,
            )
            item = store.get_recent_counterfactuals(symbol="NVDA", limit=1)[0]
            self.assertEqual(item["would_have_pnl_1d"], 10.0)
            self.assertEqual(item["would_have_pnl_5d"], -5.0)

    def test_counterfactual_summary_aggregates(self):
        tmpdir, store = self._make_store()
        with tmpdir:
            store.record_counterfactual(
                run_id="run-1",
                symbol="NVDA",
                trade_date="2026-04-10",
                original_action="BUY",
                original_confidence=0.72,
                final_action="HOLD",
                price_at_decision=100.0,
                override_reason="Risk engine overrode to HOLD.",
            )
            store.record_counterfactual(
                run_id="run-2",
                symbol="AAPL",
                trade_date="2026-04-11",
                original_action="SELL",
                original_confidence=0.68,
                final_action="HOLD",
                price_at_decision=200.0,
                override_reason="Arena overrode to HOLD.",
            )

            store.update_counterfactual_prices(
                symbol="NVDA",
                trade_date="2026-04-10",
                price_after_1d=105.0,
                price_after_5d=110.0,
            )
            store.update_counterfactual_prices(
                symbol="AAPL",
                trade_date="2026-04-11",
                price_after_1d=210.0,
                price_after_5d=190.0,
            )

            summary = store.get_counterfactual_summary()
            self.assertEqual(summary["total_overrides"], 2)
            self.assertEqual(summary["profitable_overrides"], 1)
            self.assertEqual(summary["unprofitable_overrides"], 1)
            self.assertEqual(summary["avg_missed_pnl_1d"], -2.5)
            self.assertEqual(summary["avg_missed_pnl_5d"], 10.0)


if __name__ == "__main__":
    unittest.main()
