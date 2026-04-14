"""
Integration test: verify that a well-reasoned BUY decision from the research
engine survives the full execution pipeline and gets approved.
"""

import unittest

from tradingagents.execution.config import load_risk_config
from tradingagents.execution.models import (
    BrokerAccountSnapshot,
    ExecutionConfig,
)
from tradingagents.execution.parser import DecisionParser
from tradingagents.execution.policy import ExecutionPolicy
from tradingagents.risk.engine import RiskEngine


WELL_FORMED_BUY_DECISION = """
1. Rating: Buy

2. Confidence: 78%

3. Executive Summary:
Initiate a starter long position in NVDA at current levels near $130.
The stock has reclaimed the 50-day SMA and 200-day SMA, RSI is at 58
(constructive, not overbought), and MACD histogram has turned positive.
A fresh catalyst from the latest hyperscaler capex guidance supports
near-term demand. Size the position at approximately $2,000 notional
with a stop-loss at $118 (below the 200-day SMA).

4. Investment Thesis:
NVDA is underpriced relative to the improving demand trajectory.
The market is still anchored to slowdown fears from Q1, but Q2 capex
guidance from three major hyperscalers has revised upward by 15-20%.
This is new information that the consensus has not fully absorbed.

Key risks: broader market selloff from macro/geopolitical headlines,
sector rotation away from semis, and potential earnings disappointment
if the capex revisions don't flow through to NVDA specifically.

Position sizing is conservative at ~1.5% of equity as a starter,
with room to add on confirmed breakout above $140.
"""


class FullTradePathTest(unittest.TestCase):
    def test_well_formed_buy_gets_approved(self):
        parser = DecisionParser()
        parsed = parser.parse(WELL_FORMED_BUY_DECISION, default_symbol="NVDA")
        self.assertFalse(parsed.rejected)

        intent = parsed.intents[0]
        self.assertEqual(intent.action.value, "BUY")

        self.assertTrue(intent.expected_edge, "Parser should extract expected_edge")
        self.assertGreaterEqual(
            len(intent.supporting_signals),
            2,
            "Parser should detect at least 2 signals",
        )
        self.assertGreaterEqual(
            len(intent.risks),
            1,
            "Parser should extract at least 1 risk",
        )
        self.assertTrue(
            intent.position_sizing_rationale,
            "Parser should extract sizing rationale",
        )

        config = ExecutionConfig(
            arena_enabled=False,
            allow_fractional_shares=False,
            default_position_size_pct=0.03,
            max_order_notional_usd=2000.0,
            min_order_notional_usd=100.0,
        )
        account = BrokerAccountSnapshot(
            account_id="paper",
            cash=100000.0,
            equity=100000.0,
            buying_power=100000.0,
            paper=True,
        )
        policy = ExecutionPolicy(config)
        resolved = policy.resolve(
            intent,
            account=account,
            positions={},
            latest_price=130.0,
        )

        risk_config = load_risk_config(
            env={
                "MARKET_HOURS_ONLY": "false",
                "MIN_CONFIDENCE_THRESHOLD": "0.65",
                "MIN_SIGNALS_REQUIRED": "2",
                "MAX_ORDER_NOTIONAL_USD": "2000",
            }
        )
        risk_engine = RiskEngine(risk_config)
        decision = risk_engine.evaluate(
            resolved,
            account=account,
            positions=[],
            open_orders=[],
            latest_price=130.0,
        )

        self.assertTrue(
            decision.approved,
            f"Trade should be approved but was rejected: {decision.reasons}",
        )


if __name__ == "__main__":
    unittest.main()
