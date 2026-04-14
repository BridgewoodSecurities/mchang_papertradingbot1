import unittest
from datetime import datetime, timezone

from tradingagents.execution.models import (
    BrokerAccountSnapshot,
    BrokerPosition,
    OrderIntent,
    RiskConfig,
    TradeAction,
)
from tradingagents.risk.engine import RiskEngine


class RiskEngineTests(unittest.TestCase):
    def setUp(self):
        self.account = BrokerAccountSnapshot(
            account_id="paper",
            cash=10000.0,
            equity=10000.0,
            buying_power=10000.0,
            paper=True,
        )

    def _intent(self, **overrides):
        payload = {
            "symbol": "NVDA",
            "action": TradeAction.BUY,
            "confidence": 0.7,
            "notional_usd": 500.0,
            "rationale": "Fresh news, trend confirmation, and disciplined sizing all support a selective starter entry.",
            "expected_edge": "Positive revision momentum is still underpriced.",
            "supporting_signals": [
                "fresh news catalyst",
                "price/trend confirmation",
                "portfolio/risk alignment",
            ],
            "risks": ["Volatility", "Earnings risk"],
            "why_market_wrong": "Consensus is not fully pricing the improved demand outlook.",
            "is_new_information": True,
            "position_sizing_rationale": "Risk is capped with a small starter position.",
            "fits_success_patterns": True,
            "source_raw_text": "Rating: Buy",
        }
        payload.update(overrides)
        return OrderIntent(**payload)

    def test_approves_safe_buy(self):
        engine = RiskEngine(RiskConfig(market_hours_only=False))
        intent = self._intent()

        decision = engine.evaluate(
            intent,
            account=self.account,
            positions=[],
            open_orders=[],
            latest_price=100.0,
        )
        self.assertTrue(decision.approved)

    def test_rejects_non_whitelisted_symbol(self):
        engine = RiskEngine(RiskConfig(allowed_symbols=["AAPL"], market_hours_only=False))
        intent = self._intent(notional_usd=200.0)

        decision = engine.evaluate(
            intent,
            account=self.account,
            positions=[],
            open_orders=[],
            latest_price=100.0,
        )
        self.assertFalse(decision.approved)
        self.assertIn("allowed whitelist", " ".join(decision.reasons))

    def test_rejects_short_selling(self):
        engine = RiskEngine(RiskConfig(market_hours_only=False))
        intent = self._intent(
            action=TradeAction.SELL,
            quantity=1.0,
            source_raw_text="Rating: Sell",
        )

        decision = engine.evaluate(
            intent,
            account=self.account,
            positions=[],
            open_orders=[],
            latest_price=100.0,
        )
        self.assertFalse(decision.approved)
        self.assertIn("short selling", " ".join(decision.reasons))

    def test_rejects_outside_market_hours(self):
        engine = RiskEngine(RiskConfig(market_hours_only=True))
        intent = self._intent(notional_usd=200.0)

        decision = engine.evaluate(
            intent,
            account=self.account,
            positions=[],
            open_orders=[],
            latest_price=100.0,
            now=datetime(2026, 4, 11, 3, 0, tzinfo=timezone.utc),
        )
        self.assertFalse(decision.approved)
        self.assertIn("outside regular US market hours", " ".join(decision.reasons))

    def test_rejects_order_that_exceeds_position_cap(self):
        engine = RiskEngine(RiskConfig(max_position_notional_pct_per_symbol=0.10, market_hours_only=False))
        positions = [
            BrokerPosition(
                symbol="NVDA",
                qty=5.0,
                avg_entry_price=100.0,
                market_value=500.0,
                cost_basis=500.0,
            )
        ]
        intent = OrderIntent(
            symbol="NVDA",
            action=TradeAction.BUY,
            confidence=0.9,
            notional_usd=700.0,
            rationale="Test buy",
            expected_edge="Trend and earnings momentum remain underpriced.",
            why_market_wrong="Investors are extrapolating too much slowdown risk.",
            is_new_information=True,
            position_sizing_rationale="Cap the order at a starter allocation.",
            fits_success_patterns=True,
            source_raw_text="Rating: Buy",
        )
        decision = engine.evaluate(
            intent,
            account=self.account,
            positions=positions,
            open_orders=[],
            latest_price=100.0,
        )
        self.assertFalse(decision.approved)

    def test_rejects_missing_expected_edge(self):
        engine = RiskEngine(RiskConfig(market_hours_only=False))
        intent = self._intent(expected_edge="")
        decision = engine.evaluate(
            intent,
            account=self.account,
            positions=[],
            open_orders=[],
            latest_price=100.0,
        )
        self.assertFalse(decision.approved)
        self.assertIn("Expected edge", " ".join(decision.reasons))

    def test_rejects_insufficient_supporting_signals(self):
        engine = RiskEngine(RiskConfig(market_hours_only=False))
        intent = self._intent(supporting_signals=[])
        decision = engine.evaluate(
            intent,
            account=self.account,
            positions=[],
            open_orders=[],
            latest_price=100.0,
        )
        self.assertFalse(decision.approved)
        self.assertIn("supporting signal", " ".join(decision.reasons))

    def test_three_supporting_signals_can_pass_signal_gate(self):
        engine = RiskEngine(RiskConfig(market_hours_only=False))
        intent = self._intent(
            supporting_signals=[
                "fresh news catalyst",
                "price/trend confirmation",
                "portfolio/risk alignment",
            ]
        )
        decision = engine.evaluate(
            intent,
            account=self.account,
            positions=[],
            open_orders=[],
            latest_price=100.0,
        )
        self.assertTrue(decision.approved)

    def test_two_signals_sufficient(self):
        engine = RiskEngine(RiskConfig(market_hours_only=False))
        intent = self._intent(
            supporting_signals=[
                "fresh news catalyst",
                "price/trend confirmation",
            ]
        )
        decision = engine.evaluate(
            intent,
            account=self.account,
            positions=[],
            open_orders=[],
            latest_price=100.0,
        )
        self.assertTrue(decision.approved)

    def test_first_entry_gets_lower_threshold(self):
        engine = RiskEngine(RiskConfig(market_hours_only=False))
        intent = self._intent(
            confidence=0.62,
            supporting_signals=["fresh news catalyst"],
        )
        decision = engine.evaluate(
            intent,
            account=self.account,
            positions=[],
            open_orders=[],
            latest_price=100.0,
        )
        self.assertTrue(decision.approved)
        self.assertTrue(decision.checks["first_entry_discount"])

    def test_rejects_generic_reasoning(self):
        engine = RiskEngine(RiskConfig(market_hours_only=False))
        intent = self._intent(rationale="Buy now with strong conviction.")
        decision = engine.evaluate(
            intent,
            account=self.account,
            positions=[],
            open_orders=[],
            latest_price=100.0,
        )
        self.assertFalse(decision.approved)
        self.assertIn("generic", " ".join(decision.reasons).lower())

    def test_third_trade_in_day_is_rejected(self):
        engine = RiskEngine(RiskConfig(market_hours_only=False, max_daily_trades=2))
        intent = self._intent()
        decision = engine.evaluate(
            intent,
            account=self.account,
            positions=[],
            open_orders=[],
            latest_price=100.0,
            daily_trade_count=2,
        )
        self.assertFalse(decision.approved)
        self.assertIn("Daily trade count", " ".join(decision.reasons))

    def test_second_trade_same_symbol_is_rejected(self):
        engine = RiskEngine(RiskConfig(market_hours_only=False, max_daily_trades_per_symbol=1))
        intent = self._intent()
        decision = engine.evaluate(
            intent,
            account=self.account,
            positions=[],
            open_orders=[],
            latest_price=100.0,
            symbol_daily_trade_count=1,
        )
        self.assertFalse(decision.approved)
        self.assertIn("Daily trade count for NVDA", " ".join(decision.reasons))

    def test_recent_trade_penalty_raises_threshold(self):
        engine = RiskEngine(
            RiskConfig(
                market_hours_only=False,
                min_confidence_threshold=0.80,
                extra_confidence_threshold_after_recent_trade=0.1,
            )
        )
        intent = self._intent(confidence=0.84)
        decision = engine.evaluate(
            intent,
            account=self.account,
            positions=[],
            open_orders=[],
            latest_price=100.0,
            recent_trade_count=1,
        )
        self.assertFalse(decision.approved)
        self.assertIn("0.85", " ".join(decision.reasons))

    def test_cycle_trade_cap_disabled_when_zero(self):
        engine = RiskEngine(RiskConfig(market_hours_only=False, max_trades_per_cycle=0))
        intent = self._intent()
        decision = engine.evaluate(
            intent,
            account=self.account,
            positions=[],
            open_orders=[],
            latest_price=100.0,
            cycle_trade_count=99,
        )
        self.assertTrue(decision.approved)

    def test_open_position_blocks_reentry(self):
        engine = RiskEngine(RiskConfig(market_hours_only=False, block_reentry_while_position_open=True))
        positions = [
            BrokerPosition(
                symbol="NVDA",
                qty=5.0,
                avg_entry_price=100.0,
                market_value=550.0,
                cost_basis=500.0,
            )
        ]
        intent = self._intent()
        decision = engine.evaluate(
            intent,
            account=self.account,
            positions=positions,
            open_orders=[],
            latest_price=110.0,
        )
        self.assertFalse(decision.approved)
        self.assertIn("blocks re-entry", " ".join(decision.reasons))

    def test_recent_exit_blocks_reentry(self):
        engine = RiskEngine(RiskConfig(market_hours_only=False, position_reentry_cooldown_hours=4))
        now = datetime(2026, 4, 13, 15, 0, tzinfo=timezone.utc)
        intent = self._intent()
        decision = engine.evaluate(
            intent,
            account=self.account,
            positions=[],
            open_orders=[],
            latest_price=100.0,
            last_exit_at=datetime(2026, 4, 13, 12, 0, tzinfo=timezone.utc),
            now=now,
        )
        self.assertFalse(decision.approved)
        self.assertIn("Re-entry cooldown", " ".join(decision.reasons))

    def test_reversal_blocked_within_cooldown(self):
        engine = RiskEngine(RiskConfig(market_hours_only=False, allow_reversal=False, reversal_cooldown_hours=8))
        now = datetime(2026, 4, 13, 15, 0, tzinfo=timezone.utc)
        intent = self._intent()
        decision = engine.evaluate(
            intent,
            account=self.account,
            positions=[],
            open_orders=[],
            latest_price=100.0,
            last_trade={"side": "SELL", "submitted_at": "2026-04-13T10:00:00+00:00"},
            now=now,
        )
        self.assertFalse(decision.approved)
        self.assertIn("Reversal cooldown", " ".join(decision.reasons))

    def test_flip_flop_sequence_detected(self):
        engine = RiskEngine(RiskConfig(market_hours_only=False, max_flip_flops_per_symbol_per_day=1))
        intent = self._intent()
        decision = engine.evaluate(
            intent,
            account=self.account,
            positions=[],
            open_orders=[],
            latest_price=100.0,
            recent_symbol_actions=["BUY", "SELL"],
        )
        self.assertFalse(decision.approved)
        self.assertIn("Flip-flop guard", " ".join(decision.reasons))


if __name__ == "__main__":
    unittest.main()
