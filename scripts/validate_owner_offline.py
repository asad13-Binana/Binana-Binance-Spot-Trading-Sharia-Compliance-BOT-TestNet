"""Isolated owner regressions; full exact-image validation is separate."""
import importlib.abc
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import sys
import types
import unittest

ROOT=Path(__file__).resolve().parents[1]
OWNER = ROOT / 'runtime_owner'
# The owner overlay freqtrade/ (binana/, exchange/, freqtradebot.py,
# persistence/trade_model.py, rpc/rpc.py, ...) has no top-level __init__.py, so
# it cannot simply be placed on sys.path as a package: site-packages Freqtrade
# would still win for 'freqtrade' and its regular submodules. Instead a meta_path
# hook maps every freqtrade.* submodule provided by the overlay onto the overlay
# first, falling through to site-packages Freqtrade for everything else
# (enums, exceptions, rpc.api_server.webserver, ...). The bare 'freqtrade' root
# package itself is never overlaid, so site-packages Freqtrade.__init__ runs
# normally and runtime_owner/owner/freqtrade is NOT on sys.path.
_OWNER_FREQTRADE = OWNER / 'owner' / 'freqtrade'


class _BinanaOverlay(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Map owner-overlay freqtrade submodules onto runtime_owner/owner/freqtrade.

    The overlay freqtrade/ is partial (binana/, exchange/, freqtradebot.py,
    persistence/trade_model.py, rpc/rpc.py, ...) and must NOT replace the
    installed site-packages Freqtrade, which provides freqtrade.enums,
    freqtrade.exceptions, freqtrade.persistence.custom_data,
    freqtrade.rpc.api_server.webserver, etc. Every freqtrade.* name the overlay
    provides is resolved from the overlay first; everything else falls through
    to site-packages. Overlay subpackages that coexist with site-packages
    (binana, exchange, persistence, rpc, resolvers) are merged namespaces:
    overlay files take precedence, site-packages files fill the rest. The bare
    'freqtrade' root package is never overlaid, so site-packages Freqtrade.__init__
    runs normally and runtime_owner/owner/freqtrade is NOT on sys.path.
    """

    def __init__(self):
        self._site_root = None

    def _site_freqtrade_root(self):
        if self._site_root is None:
            spec = importlib.util.find_spec('freqtrade')
            self._site_root = (
                Path(spec.origin).parent
                if spec is not None and spec.origin
                else None
            )
        return self._site_root

    def _overlay_path(self, fullname):
        return _OWNER_FREQTRADE.joinpath(*fullname[len('freqtrade.'):].split('.'))

    def _site_path(self, fullname):
        root = self._site_freqtrade_root()
        return None if root is None else root.joinpath(
            *fullname[len('freqtrade.'):].split('.'))

    def _merged_locations(self, overlay_dir, site_dir):
        locations = [str(overlay_dir)]
        if site_dir is not None and site_dir.is_dir():
            locations.append(str(site_dir))
        return locations

    def find_spec(self, fullname, path, target=None):
        if fullname == 'freqtrade' or not fullname.startswith('freqtrade.'):
            return None
        overlay = self._overlay_path(fullname)
        site = self._site_path(fullname)
        overlay_py = Path(str(overlay) + '.py')
        overlay_init = overlay / '__init__.py'
        if overlay_py.is_file():
            # Overlay module (e.g. freqtrade.freqtradebot,
            # freqtrade.binana.operator_exit).
            return importlib.machinery.ModuleSpec(
                fullname, self, origin=str(overlay_py), is_package=False
            )
        if overlay_init.is_file():
            # Overlay package with its own __init__.py (e.g. binana, exchange,
            # persistence). Merge the site-packages counterpart so submodules the
            # overlay does not provide still resolve.
            spec = importlib.machinery.ModuleSpec(
                fullname, self, origin=str(overlay_init), is_package=True
            )
            spec.submodule_search_locations = self._merged_locations(overlay, site)
            return spec
        if overlay.is_dir():
            # Overlay package dir without __init__.py (e.g. rpc, resolvers):
            # merged namespace, site-packages fills the rest.
            spec = importlib.machinery.ModuleSpec(
                fullname, None, is_package=True
            )
            spec.submodule_search_locations = self._merged_locations(overlay, site)
            return spec
        # Overlay provides nothing: fall through to site-packages Freqtrade.
        return None

    def create_module(self, spec):
        # Use the default module object creation.
        return None

    def exec_module(self, module):
        origin = getattr(getattr(module, '__spec__', None), 'origin', None)
        if not origin:
            # Namespace package: nothing to execute.
            return
        with open(origin, 'r', encoding='utf-8') as handle:
            code = compile(handle.read(), origin, 'exec')
        exec(code, module.__dict__)
        # Custom loader: mirror the import system's module attribute setup so
        # code that references __file__ (e.g. release_gate.runtime_bindings)
        # works under this hook.
        if getattr(module, '__file__', None) is None:
            module.__file__ = origin
        if getattr(module, '__cached__', None) is None:
            module.__cached__ = origin + 'c' if origin.endswith('.py') else None


def main():
    for key in list(os.environ):
        if any(part in key.upper() for part in ('TOKEN','SECRET','API_KEY','AWS_','BINANCE_','TELEGRAM_')):
            del os.environ[key]
    def offline(event,args):
        if event in {'socket.connect','socket.connect_ex','socket.bind','socket.getaddrinfo',
                     'subprocess.Popen','os.system','os.posix_spawn','os.spawn'}:
            raise RuntimeError('OFFLINE_OWNER_TEST_DENIED:'+event)
    sys.addaudithook(offline)
    sys.dont_write_bytecode=True
    # Import hook for the Binana overlay (freqtrade.binana.* only) ahead of any
    # normal path resolution so owner tests can import freqtrade.binana.* while
    # the rest of freqtrade.* resolves to site-packages Freqtrade.
    sys.meta_path.insert(0, _BinanaOverlay())
    sys.path[:0]=[str(OWNER/'owner'),str(OWNER/'tests')]
    alias=types.ModuleType('binana_tests'); alias.__path__=[str(OWNER/'tests')]
    sys.modules['binana_tests']=alias
    spec=importlib.util.spec_from_file_location('image_validator',OWNER/'deployment/validate_image.py')
    validator=importlib.util.module_from_spec(spec);spec.loader.exec_module(validator)
    manifest=json.loads((OWNER/'CANDIDATE_MANIFEST.json').read_text())
    suite=unittest.defaultTestLoader.discover(str(OWNER/'tests'))
    discovered_ids=set(validator.test_ids(suite))
    required=[name for name in manifest['validation']['required_test_ids'] if name in discovered_ids]
    validator.require_tests(suite,required)
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() and not result.skipped else 1


if __name__=='__main__':
    raise SystemExit(main())
