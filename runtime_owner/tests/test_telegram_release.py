import ast
from pathlib import Path
import time
import unittest


def load(name):
    path=Path(__file__).resolve().parents[1]/'runtime-fixes/telegram/runtime_stable_panel.py'
    tree=ast.parse(path.read_text(encoding='utf-8'))
    function=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name==name)
    namespace={'time':time}
    exec(compile(ast.Module(body=[function],type_ignores=[]),str(path),'exec'),namespace)
    return namespace[name]


class TelegramRelease(unittest.TestCase):
    def test_incomplete_lifecycle_always_blocks_resume(self):
        check=load('_resume_blockers_from_state')
        state={'cfg':{'state':'paused'},'ext':{'available':True,'incidents':[],'intents':[],'protection':[]},
               'market':{'status':'fresh','stream':{'connected':True,'probation_complete':True}},
               'sidecar':{'order_authority':False,'exchange_credentials_present':False},
               'telegram':{'ok':True},'code_marker':{'verified':True,'controlled_lifecycle_verified':False}}
        self.assertIn('controlled_lifecycle_unverified',check(**state))


if __name__=='__main__':
    unittest.main()
