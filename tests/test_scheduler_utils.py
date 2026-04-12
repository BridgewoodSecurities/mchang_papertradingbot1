import unittest
from datetime import datetime, timezone

from tradingagents.scheduler.market import MarketSession, is_market_open, is_trading_day
from tradingagents.scheduler.timing import align_to_bucket_start, next_bucket_start


class SchedulerUtilsTests(unittest.TestCase):
    def setUp(self):
        self.session = MarketSession()

    def test_align_to_bucket_start(self):
        now = datetime(2026, 4, 13, 14, 7, tzinfo=timezone.utc)  # 10:07 ET
        aligned = align_to_bucket_start(now, interval_minutes=15, session=self.session)
        self.assertEqual(aligned.astimezone(self.session.tzinfo).strftime("%H:%M"), "10:00")

    def test_next_bucket_start(self):
        now = datetime(2026, 4, 13, 14, 7, tzinfo=timezone.utc)
        nxt = next_bucket_start(now, interval_minutes=15, session=self.session)
        self.assertEqual(nxt.astimezone(self.session.tzinfo).strftime("%H:%M"), "10:15")

    def test_market_open_and_trading_day(self):
        open_time = datetime(2026, 4, 13, 14, 0, tzinfo=timezone.utc)  # 10:00 ET Monday
        closed_time = datetime(2026, 4, 11, 14, 0, tzinfo=timezone.utc)  # Saturday
        self.assertTrue(is_trading_day(open_time, self.session))
        self.assertTrue(is_market_open(open_time, self.session))
        self.assertFalse(is_trading_day(closed_time, self.session))
        self.assertFalse(is_market_open(closed_time, self.session))


if __name__ == "__main__":
    unittest.main()
