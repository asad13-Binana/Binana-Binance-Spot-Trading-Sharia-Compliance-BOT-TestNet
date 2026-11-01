"""Exercise the actual adapter startup against the retained panel request path."""
import ast
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('oracle_panel_adapter', ROOT / 'runtime_owner/deployment/oracle_panel.py')
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


class PanelTransportTests(unittest.TestCase):
    def panel(self, post, guard):
        response = SimpleNamespace(status_code=200, json=lambda: {'ok': True},
                                   raise_for_status=lambda: None)
        base = SimpleNamespace(_telegram_post=post, _telegram_rate_guard=guard,
            requests=SimpleNamespace(request=Mock(return_value=response)), BASE='https://fixture.invalid',
            sharia_scan_request=lambda: None, sharia_bounded_scan_requests=lambda: None,
            sharia_owner_decision=lambda: None)
        source = ROOT / 'runtime_owner/runtime-fixes/telegram/runtime_stable_panel.py'
        node = next(node for node in ast.parse(source.read_bytes()).body
                    if isinstance(node, ast.FunctionDef) and node.name == '_tg_request')
        namespace = {'base': base, 'time': SimpleNamespace(sleep=Mock())}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), namespace)
        panel = SimpleNamespace(base=base, _tg_request=namespace['_tg_request'])
        panel.main = lambda: panel._tg_request('POST', '/setMyCommands', data={'commands': '[]'}, attempts=2)
        return panel

    def test_startup_installs_shared_cooldown_transport_before_panel_threads(self):
        post = Mock(side_effect=RuntimeError('fixture cooldown active'))
        panel = self.panel(post, Mock(side_effect=RuntimeError('fixture cooldown active')))
        with patch.dict(sys.modules, {'runtime_stable_panel': panel}):
            with self.assertRaisesRegex(RuntimeError, 'cooldown'):
                adapter.main()
        post.assert_not_called()
        panel.base.requests.request.assert_not_called()

    def test_transient_retry_still_uses_shared_transport_each_time(self):
        ok = SimpleNamespace(status_code=200, json=lambda: {'ok': True}, raise_for_status=lambda: None)
        post = Mock(side_effect=[ConnectionError('fixture disconnect'), ok])
        panel = self.panel(post, Mock())
        with patch.dict(sys.modules, {'runtime_stable_panel': panel}), patch.object(adapter.time, 'sleep'):
            adapter.main()
        self.assertEqual(post.call_count, 2)
        panel.base.requests.request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
