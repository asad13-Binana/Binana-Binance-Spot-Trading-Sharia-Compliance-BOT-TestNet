"""Corrupt local state and malformed public responses must not invent listing age."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import tests._harness  # noqa: F401
from services.universe_service.scanner import ListingAgeCache


class ListingAgeValidation(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'age.json'
        self.now = 1_800_000_000_000

    def test_boolean_cache_is_not_an_ancient_listing_timestamp(self):
        self.path.write_text(json.dumps({'ADAUSDT': True}), encoding='utf-8')
        api = Mock()
        api.get.side_effect = OSError('offline fixture')
        self.assertIsNone(ListingAgeCache(self.path, api).age_days('ADAUSDT', self.now))

    def test_non_object_cache_can_recover_from_valid_exchange_evidence(self):
        for malformed in ([123], 'corrupt', 1, True):
            with self.subTest(cache=malformed):
                self.path.write_text(json.dumps(malformed), encoding='utf-8')
                api = Mock()
                api.get.return_value = [[self.now - 31 * 86_400_000]]
                self.assertEqual(ListingAgeCache(self.path, api).age_days('ADAUSDT', self.now), 31)

    def test_non_integer_response_cannot_be_coerced_into_old_listing(self):
        for malformed in (True, False, 1.5, '1', None, {}, []):
            with self.subTest(timestamp=malformed):
                self.path.write_text('{}', encoding='utf-8')
                api = Mock()
                api.get.return_value = [[malformed]]
                self.assertIsNone(ListingAgeCache(self.path, api).age_days('ADAUSDT', self.now))
                self.assertEqual(json.loads(self.path.read_text()), {})

    def test_valid_cached_integer_needs_no_network(self):
        self.path.write_text(json.dumps({'ADAUSDT': self.now - 30 * 86_400_000}), encoding='utf-8')
        api = Mock()
        self.assertEqual(ListingAgeCache(self.path, api).age_days('ADAUSDT', self.now), 30)
        api.get.assert_not_called()

    def test_future_timestamp_cannot_pass_minimum_age(self):
        self.path.write_text(json.dumps({'ADAUSDT': self.now + 86_400_000}), encoding='utf-8')
        self.assertLess(ListingAgeCache(self.path, Mock()).age_days('ADAUSDT', self.now), 30)


if __name__ == '__main__':
    unittest.main()
