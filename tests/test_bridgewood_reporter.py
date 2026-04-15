import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock

from tradingagents.execution.models import (
    BrokerOrder,
    ExecutionConfig,
    OrderType,
    TradeAction,
)
from tradingagents.reporting.bridgewood import (
    BridgewoodReporter,
    BridgewoodReporterError,
)


class BridgewoodReporterTests(unittest.TestCase):
    def test_from_execution_config_normalizes_root_api_url(self):
        config = ExecutionConfig(
            bridgewood_api_base="https://bridgewood.onrender.com",
            bridgewood_agent_api_key="bgw_test",
        )

        reporter = BridgewoodReporter.from_execution_config(config)

        self.assertEqual(reporter.api_base, "https://bridgewood.onrender.com/v1")

    def test_report_filled_order_posts_expected_payload(self):
        response = MagicMock()
        response.json.return_value = {"results": [{"status": "recorded"}]}
        response.raise_for_status.return_value = None
        session = MagicMock()
        session.post.return_value = response
        reporter = BridgewoodReporter(
            api_base="https://bridgewood.onrender.com/v1",
            agent_api_key="bgw_test",
            session=session,
        )
        order = BrokerOrder(
            id="alpaca-order-123",
            client_order_id="client-123",
            symbol="AAPL",
            side=TradeAction.BUY,
            order_type=OrderType.MARKET,
            status="filled",
            filled_qty=2.5,
            filled_avg_price=187.52,
            submitted_at=datetime(2026, 4, 14, 16, 0, tzinfo=timezone.utc),
            raw={"filled_at": "2026-04-14T16:00:05Z"},
        )

        reporter.report_filled_order(order)

        session.post.assert_called_once_with(
            "https://bridgewood.onrender.com/v1/executions",
            headers={
                "Authorization": "Bearer bgw_test",
                "Content-Type": "application/json",
            },
            json={
                "executions": [
                    {
                        "external_order_id": "alpaca-order-123",
                        "symbol": "AAPL",
                        "side": "buy",
                        "quantity": 2.5,
                        "price": 187.52,
                        "fees": 0,
                        "executed_at": "2026-04-14T16:00:05Z",
                    }
                ]
            },
            timeout=10.0,
        )

    def test_report_filled_order_rejects_unfilled_orders(self):
        reporter = BridgewoodReporter(
            api_base="https://bridgewood.onrender.com/v1",
            agent_api_key="bgw_test",
            session=MagicMock(),
        )
        order = BrokerOrder(
            id="alpaca-order-123",
            symbol="AAPL",
            side=TradeAction.BUY,
            order_type=OrderType.MARKET,
            status="accepted",
            submitted_at=datetime(2026, 4, 14, 16, 0, tzinfo=timezone.utc),
        )

        with self.assertRaises(BridgewoodReporterError):
            reporter.report_filled_order(order)


if __name__ == "__main__":
    unittest.main()
