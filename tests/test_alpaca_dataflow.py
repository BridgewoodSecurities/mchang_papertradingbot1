import unittest
from unittest.mock import Mock

import requests

from tradingagents.dataflows.alpaca import AlpacaDataClient, AlpacaDataError


class AlpacaDataflowTests(unittest.TestCase):
    def test_latest_trade_maps_response(self):
        response = Mock()
        response.status_code = 200
        response.text = '{"trades":{"SPY":{"p":601.25}}}'
        response.json.return_value = {"trades": {"SPY": {"p": 601.25}}}
        response.raise_for_status = Mock()

        session = Mock()
        session.request.return_value = response

        client = AlpacaDataClient(
            api_key="key",
            secret_key="secret",
            session=session,
        )
        trade = client.get_latest_trade("SPY")
        self.assertEqual(trade["p"], 601.25)

    def test_request_raises_data_error_after_retries(self):
        session = Mock()
        session.request.side_effect = requests.Timeout("boom")

        client = AlpacaDataClient(
            api_key="key",
            secret_key="secret",
            session=session,
            max_retries=1,
        )
        with self.assertRaises(AlpacaDataError):
            client.get_latest_quote("SPY")


if __name__ == "__main__":
    unittest.main()
