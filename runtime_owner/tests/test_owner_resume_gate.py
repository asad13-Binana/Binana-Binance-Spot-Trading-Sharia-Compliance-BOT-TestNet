import ast
from pathlib import Path
from types import SimpleNamespace
import unittest


class OwnerResumeGate(unittest.TestCase):
    def test_rpc_start_cannot_bypass_owner_admission_gate(self):
        source = Path(__file__).resolve().parents[1] / 'owner/freqtrade/rpc/rpc.py'
        if not source.exists():
            source = Path('/freqtrade/freqtrade/rpc/rpc.py')
        tree = ast.parse(source.read_text())
        rpc = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'RPC')
        method = next(node for node in rpc.body if isinstance(node, ast.FunctionDef) and node.name == '_rpc_start')
        class RPCException(Exception):
            pass
        class BlockedOwner:
            def assert_release_ready(self):
                raise RuntimeError('CONTROLLED_LIFECYCLE_UNVERIFIED')
        states = SimpleNamespace(RUNNING='running', PAUSED='paused')
        namespace = {'State': states, 'RPCException': RPCException}
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), 'exec'), namespace)
        bot = SimpleNamespace(state='paused', binana=BlockedOwner())
        with self.assertRaisesRegex(RPCException, 'CONTROLLED_LIFECYCLE_UNVERIFIED'):
            namespace['_rpc_start'](SimpleNamespace(_freqtrade=bot))
        self.assertEqual(bot.state, 'paused')


if __name__ == '__main__':
    unittest.main()
