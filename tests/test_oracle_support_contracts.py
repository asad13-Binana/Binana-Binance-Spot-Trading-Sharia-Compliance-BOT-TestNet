"""Public support packaging preserves mode and real panel input requirements."""
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT/'runtime_owner/deployment'


def load(name):
    spec = importlib.util.spec_from_file_location(name, DEPLOY/(name+'.py'))
    module = importlib.util.module_from_spec(spec)
    previous = sys.path[:]
    try:
        sys.path.insert(0, str(DEPLOY))
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = previous
    return module


class OracleSupportContractTests(unittest.TestCase):
    def test_panel_assignment_is_not_a_required_broker_input(self):
        validator = load('oracle_validate_support')
        self.assertTrue(callable(getattr(validator, 'required_panel_attributes', None)))
        required = validator.required_panel_attributes('base.reader()\nbase.created = lambda: None\n')
        self.assertEqual(required, {'reader'})

    def test_augmented_assignment_and_deletion_still_require_existing_attributes(self):
        validator = load('oracle_validate_support')
        self.assertTrue(callable(getattr(validator, 'required_panel_attributes', None)))
        required = validator.required_panel_attributes('base.count += 1\ndel base.old\nbase.created = 2\n')
        self.assertEqual(required, {'count', 'old'})

    def test_live_generated_monitor_never_labels_itself_testnet(self):
        builder = load('materialize_oracle_bundle')
        with patch.object(builder, 'package_environment', return_value='live'):
            generated = builder.support_sources(ROOT)['owner_monitor.py']
        namespace = {'__name__': 'offline_monitor_fixture'}
        exec(compile(generated, 'owner_monitor.py', 'exec'), namespace)
        state = namespace['state_payload'](100)
        self.assertEqual(state['mode'], 'live')
        self.assertFalse(state['order_authority'])
        self.assertFalse(state['trading_ready'])
        self.assertFalse(state['entries_enabled'])


if __name__ == '__main__':
    unittest.main()
