import unittest
from datetime import datetime, timezone

from tradingagents.universe.selection import select_symbols_for_cycle


class FakeAlpacaDataClient:
    def __init__(self, payload):
        self.payload = payload

    def get_stock_bars_batch(self, symbols, *, start, end, timeframe="1Day", limit=10):
        return {symbol: self.payload.get(symbol, []) for symbol in symbols}

    def get_news(self, *, symbols=None, start=None, end=None, limit=20):
        items = []
        for symbol in symbols or []:
            for count in range(self.payload.get(f"{symbol}_news_count", 0)):
                items.append({"symbols": [symbol], "headline": f"{symbol} news {count}"})
        return items[:limit]


class UniverseSelectionTests(unittest.TestCase):
    def test_select_symbols_for_cycle_prefers_liquid_movers(self):
        client = FakeAlpacaDataClient(
            {
                "AAPL": [
                    {"c": 100.0, "v": 1_000_000},
                    {"c": 106.0, "v": 2_000_000},
                ],
                "MSFT": [
                    {"c": 200.0, "v": 200_000},
                    {"c": 202.0, "v": 220_000},
                ],
                "NVDA": [
                    {"c": 150.0, "v": 3_000_000},
                    {"c": 162.0, "v": 4_000_000},
                ],
            }
        )

        selected = select_symbols_for_cycle(
            symbols=["AAPL", "MSFT", "NVDA"],
            limit=2,
            as_of=datetime(2026, 4, 13, 13, 30, tzinfo=timezone.utc),
            data_client=client,
        )

        self.assertEqual(selected, ["NVDA", "AAPL"])

    def test_select_symbols_for_cycle_prefers_sector_diversity(self):
        client = FakeAlpacaDataClient(
            {
                "AAPL": [
                    {"c": 100.0, "v": 2_000_000},
                    {"c": 106.0, "v": 2_500_000},
                ],
                "MSFT": [
                    {"c": 200.0, "v": 2_100_000},
                    {"c": 212.0, "v": 2_600_000},
                ],
                "JPM": [
                    {"c": 150.0, "v": 1_500_000},
                    {"c": 160.0, "v": 1_900_000},
                ],
            }
        )

        selected = select_symbols_for_cycle(
            symbols=["AAPL", "MSFT", "JPM"],
            limit=2,
            as_of=datetime(2026, 4, 13, 13, 30, tzinfo=timezone.utc),
            data_client=client,
            sector_by_symbol={
                "AAPL": "Information Technology",
                "MSFT": "Information Technology",
                "JPM": "Financials",
            },
        )

        self.assertEqual(set(selected), {"MSFT", "JPM"})

    def test_select_symbols_for_cycle_prioritizes_existing_positions(self):
        client = FakeAlpacaDataClient(
            {
                "AAPL": [
                    {"c": 100.0, "v": 500_000},
                    {"c": 101.0, "v": 550_000},
                ],
                "XOM": [
                    {"c": 100.0, "v": 500_000},
                    {"c": 103.0, "v": 550_000},
                ],
                "AAPL_news_count": 2,
            }
        )

        selected = select_symbols_for_cycle(
            symbols=["AAPL", "XOM"],
            limit=1,
            as_of=datetime(2026, 4, 13, 13, 30, tzinfo=timezone.utc),
            data_client=client,
            held_symbols={"AAPL"},
            sector_by_symbol={
                "AAPL": "Information Technology",
                "XOM": "Energy",
            },
        )

        self.assertEqual(selected, ["AAPL"])


if __name__ == "__main__":
    unittest.main()
