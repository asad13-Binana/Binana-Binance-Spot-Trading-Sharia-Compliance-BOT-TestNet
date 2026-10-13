"""The shared Deployment button must not report an old rollout as live state."""
import ast
import json
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

SOURCE = Path(__file__).resolve().parents[1] / 'services/telegram_broker/bot.py'


def display(*, stale=False, unavailable=False, health_changes=None):
    sent = []
    paths = {
        'deployment_status.json': {'status': 'OLD_ROLLOUT', 'github_updated': False},
        'sidecar_health.json': {'ts': 1 if stale else time.time(), 'execution_mode': 'simulation',
                               'entries_enabled': False, 'trading_ready': False, 'readiness_blockers': ['ENTRIES_DISARMED']},
    }
    paths['sidecar_health.json'].update(health_changes or {})
    ns = {
        'json': json, 'time': time, 'RUNTIME': Path('/runtime'), 'BOT_ENVIRONMENT': 'SIMULATION',
        'read_json': lambda p, default: paths.get(p.name, default),
        'ft_call': lambda *args: {'ok': False} if unavailable else {
            'ok': True, 'data': {'state': 'stopped', 'strategy': 'IctSmcStrategy'}},
        'envelope': SimpleNamespace(installed_release_hash=lambda: 'a' * 64),
        '_release_label': lambda: 'test-release',
        'send': lambda text, chat: sent.append(text),
    }
    tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
    nodes = [x for x in tree.body if isinstance(x, ast.FunctionDef)
             and x.name in {'route', '_current_deployment'}]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), 'exec'), ns)
    ns['route']('deploy', 'owner')
    assert len(sent[-1]) <= 3800, 'Deployment response exceeds the Telegram budget'
    return json.loads(sent[-1])


class DeploymentView(unittest.TestCase):
    def test_button_uses_current_authenticated_state(self):
        result = display()
        self.assertEqual(result['freqtrade_state'], 'STOPPED')
        self.assertEqual(result['strategy'], 'IctSmcStrategy')
        self.assertIs(result['entries_enabled'], False)
        self.assertNotIn('OLD_ROLLOUT', json.dumps(result))

    def test_stale_health_and_unavailable_api_remain_unknown(self):
        result = display(stale=True, unavailable=True)
        self.assertEqual(result['freqtrade_state'], 'UNKNOWN')
        self.assertIsNone(result['trading_ready'])
        self.assertIsNone(result['entries_enabled'])
        self.assertEqual(result['execution_mode'], 'UNKNOWN')

    def test_safety_halts_and_faults_are_visible_even_without_named_blockers(self):
        result = display(health_changes={'readiness_blockers': [],
            'safety_halts': {'reconcile': 'pending'},
            'runtime_safety_faults': {'database': 'unavailable'}})
        self.assertEqual(result.get('safety_halts'), {'reconcile': 'pending'})
        self.assertEqual(result.get('runtime_safety_faults'), {'database': 'unavailable'})

    def test_verbose_faults_keep_valid_json_and_all_status_fields(self):
        result = display(health_changes={
            'safety_halts': {str(i): 'reconciliation ' * 100 for i in range(20)},
            'runtime_safety_faults': {'database': '\u26a0' * 5000},
            'readiness_blockers': ['NEEDS_RECONCILIATION'],
        })
        self.assertEqual(result['readiness_blockers'], ['NEEDS_RECONCILIATION'])
        self.assertIn('observed_at_utc', result)
        self.assertEqual(result['freqtrade_state'], 'STOPPED')
        self.assertTrue(result['safety_halts']['truncated'])
        self.assertTrue(result['runtime_safety_faults']['truncated'])


if __name__ == '__main__':
    unittest.main()
