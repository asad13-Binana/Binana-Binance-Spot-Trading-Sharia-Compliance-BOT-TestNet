import unittest
from datetime import datetime, timezone, timedelta

import pandas as pd

from freqtrade.binana.confluence import score_confluence


class ConfluencePolicy(unittest.TestCase):
    def test_configured_minimum_votes_are_enforced(self):
        now = datetime(2026, 9, 20, 0, 0, 1, tzinfo=timezone.utc)
        rows = []
        for index in range(100):
            price = 100 + 0.1 * index * index
            rows.append({'date': now - timedelta(seconds=1, minutes=5 * (100 - index)),
                         'open': price, 'high': price + 1, 'low': price - 1,
                         'close': price, 'volume': 100})
        frame = pd.DataFrame(rows)
        baseline = score_confluence(frame, now=now)
        self.assertEqual((baseline['status'], baseline['volume_votes'], baseline['momentum_votes']),
                         ('QUALIFIED', 2, 2))
        for policy in [{'minimum_volume_votes': 3}, {'minimum_momentum_votes': 3}]:
            with self.subTest(policy=policy):
                self.assertEqual(score_confluence(frame, now=now, policy=policy)['status'], 'BLOCKED')


if __name__ == '__main__':
    unittest.main()
