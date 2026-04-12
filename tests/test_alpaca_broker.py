import unittest
from unittest.mock import Mock

from tradingagents.brokers.alpaca import AlpacaPaperBroker, BrokerConfigurationError
from tradingagents.execution.models import OrderIntent, OrderType, TradeAction


class AlpacaPaperBrokerTests(unittest.TestCase):
    def test_rejects_non_paper_endpoint(self):
        with self.assertRaises(BrokerConfigurationError):
            AlpacaPaperBroker(
                api_key="key",
                secret_key="secret",
                base_url="https://api.alpaca.markets",
            )

    def test_get_account_maps_response(self):
        response = Mock()
        response.status_code = 200
        response.text = '{"id":"acct","status":"ACTIVE","currency":"USD","cash":"1000","equity":"1200","buying_power":"2000","portfolio_value":"1200","daytrade_count":1,"account_blocked":false}'
        response.json.return_value = {
            "id": "acct",
            "status": "ACTIVE",
            "currency": "USD",
            "cash": "1000",
            "equity": "1200",
            "buying_power": "2000",
            "portfolio_value": "1200",
            "daytrade_count": 1,
            "account_blocked": False,
        }
        response.raise_for_status = Mock()

        session = Mock()
        session.request.return_value = response

        broker = AlpacaPaperBroker(
            api_key="key",
            secret_key="secret",
            session=session,
        )
        account = broker.get_account()
        self.assertEqual(account.account_id, "acct")
        self.assertEqual(account.cash, 1000.0)
        self.assertTrue(account.paper)

    def test_submit_market_buy_uses_notional(self):
        response = Mock()
        response.status_code = 200
        response.text = '{"id":"order-1","client_order_id":"cid","symbol":"NVDA","side":"buy","type":"market","status":"accepted","notional":"500"}'
        response.json.return_value = {
            "id": "order-1",
            "client_order_id": "cid",
            "symbol": "NVDA",
            "side": "buy",
            "type": "market",
            "status": "accepted",
            "notional": "500",
        }
        response.raise_for_status = Mock()

        session = Mock()
        session.request.return_value = response

        broker = AlpacaPaperBroker(
            api_key="key",
            secret_key="secret",
            session=session,
        )
        intent = OrderIntent(
            symbol="NVDA",
            action=TradeAction.BUY,
            confidence=0.9,
            notional_usd=500.0,
            order_type=OrderType.MARKET,
            rationale="Test",
            source_raw_text="Rating: Buy",
        )

        order = broker.submit_order(intent, client_order_id="cid")
        kwargs = session.request.call_args.kwargs
        self.assertEqual(kwargs["json"]["notional"], "500.00")
        self.assertEqual(order.symbol, "NVDA")
        self.assertEqual(order.status, "accepted")


if __name__ == "__main__":
    unittest.main()
