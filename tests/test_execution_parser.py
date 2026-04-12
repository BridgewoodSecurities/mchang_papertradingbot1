import unittest

from tradingagents.execution.models import TradeAction
from tradingagents.execution.parser import DecisionParser


class DecisionParserTests(unittest.TestCase):
    def setUp(self):
        self.parser = DecisionParser()

    def test_explicit_buy(self):
        result = self.parser.parse(
            """Rating: Buy
            Executive Summary: Buy NVDA with a $1,500 position size and a 2 week horizon.
            Confidence: 80%
            Stop loss at $100
            Target at $140
            """,
            default_symbol="NVDA",
        )

        self.assertFalse(result.rejected)
        intent = result.intents[0]
        self.assertEqual(intent.action, TradeAction.BUY)
        self.assertEqual(intent.notional_usd, 1500.0)
        self.assertEqual(intent.stop_loss, 100.0)
        self.assertEqual(intent.take_profit, 140.0)
        self.assertAlmostEqual(intent.confidence, 0.8)

    def test_explicit_sell(self):
        result = self.parser.parse(
            """Rating: Sell
            Executive Summary: Exit the position and sell 25 shares immediately.
            """,
            default_symbol="AAPL",
        )
        self.assertEqual(result.intents[0].action, TradeAction.SELL)
        self.assertEqual(result.intents[0].quantity, 25.0)

    def test_hold(self):
        result = self.parser.parse(
            """Rating: Hold
            Executive Summary: Maintain current position and take no action.
            """,
            default_symbol="MSFT",
        )
        self.assertEqual(result.intents[0].action, TradeAction.HOLD)

    def test_small_long_phrasing(self):
        result = self.parser.parse(
            "A small long makes sense here with gradual accumulation over the next month.",
            default_symbol="AMD",
        )
        self.assertEqual(result.intents[0].action, TradeAction.BUY)

    def test_reduce_exposure_phrasing(self):
        result = self.parser.parse(
            "Reduce exposure in TSLA and trim the position into strength.",
            default_symbol="TSLA",
        )
        self.assertEqual(result.intents[0].action, TradeAction.SELL)

    def test_multiple_symbols_in_one_response(self):
        result = self.parser.parse(
            """AAPL: Rating: Buy
            Executive Summary: allocate $500 here.

            MSFT: Rating: Sell
            Executive Summary: sell 10 shares to reduce exposure.
            """
        )
        symbols = [intent.symbol for intent in result.intents]
        actions = [intent.action for intent in result.intents]
        self.assertEqual(symbols, ["AAPL", "MSFT"])
        self.assertEqual(actions, [TradeAction.BUY, TradeAction.SELL])

    def test_malformed_or_ambiguous_output(self):
        result = self.parser.parse(
            "We might buy, but maybe hold for now while we watch the next print.",
            default_symbol="NFLX",
        )
        self.assertEqual(result.intents[0].action, TradeAction.HOLD)
        self.assertTrue(result.warnings)


if __name__ == "__main__":
    unittest.main()
