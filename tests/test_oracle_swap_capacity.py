"""Exercise the installer's real swap decision without privileged commands."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from tests._harness import posix_bash


ROOT = Path(__file__).resolve().parents[1]


class OracleSwapCapacityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bash = posix_bash()
        if cls.bash is None:
            raise unittest.SkipTest('a working POSIX bash is required')
        setup = (ROOT / 'deploy/oracle_setup.sh').read_text(encoding='utf-8')
        # Execute the production reader and condition, stopping before the
        # installer can allocate a file, enable swap, or modify /etc/fstab.
        cls.reader = setup[setup.index('physical_mib='):setup.index('if (( physical_mib')]
        cls.reader += ''.join(line + '\n' for line in setup.splitlines()
                              if line.startswith('page_size_bytes='))
        cls.condition = next(line for line in setup.splitlines()
                             if line.startswith('if (( swap_'))

    def decision(self, swap_kib, *, page_bytes=4096, target_gib=4):
        with tempfile.TemporaryDirectory() as directory:
            meminfo = Path(directory) / 'meminfo'
            meminfo.write_text(f'MemTotal: 12582912 kB\nSwapTotal: {swap_kib} kB\n',
                               encoding='ascii')
            script = ('set -euo pipefail\n'
                      'getconf(){ [[ "$1" == PAGESIZE ]] || return 1; '
                      'printf "%s\\n" "$TEST_PAGE_BYTES"; }\n'
                      + self.reader + self.condition
                      + '\n  printf "CREATE\\n"\nelse\n  printf "KEEP\\n"\nfi\n')
            result = subprocess.run([self.bash, '-c', script],
                env={**os.environ, 'MEMINFO_PATH': meminfo.as_posix(),
                     'TEST_PAGE_BYTES': str(page_bytes), 'SWAP_SIZE_GIB': str(target_gib)},
                capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout.strip()

    def test_four_gib_swap_with_one_header_page_is_kept(self):
        for page_bytes in (4096, 65536):
            with self.subTest(page_bytes=page_bytes):
                self.assertEqual(self.decision(4 * 1024 * 1024 - page_bytes // 1024,
                                               page_bytes=page_bytes), 'KEEP')

    def test_one_kib_below_target_usable_capacity_still_requires_swap(self):
        for page_bytes in (4096, 65536):
            with self.subTest(page_bytes=page_bytes):
                self.assertEqual(self.decision(4 * 1024 * 1024 - page_bytes // 1024 - 1,
                                               page_bytes=page_bytes), 'CREATE')

    def test_absent_or_materially_undersized_swap_still_requires_swap(self):
        for swap_kib in (0, 2 * 1024 * 1024 - 4):
            with self.subTest(swap_kib=swap_kib):
                self.assertEqual(self.decision(swap_kib), 'CREATE')

    def test_existing_larger_swap_is_kept(self):
        self.assertEqual(self.decision(8 * 1024 * 1024 - 4), 'KEEP')

    def test_requested_size_is_respected_at_both_supported_extremes(self):
        for target_gib in (1, 8):
            with self.subTest(target_gib=target_gib):
                usable_kib = target_gib * 1024 * 1024 - 4
                self.assertEqual(self.decision(usable_kib, target_gib=target_gib), 'KEEP')
                self.assertEqual(self.decision(usable_kib - 1, target_gib=target_gib), 'CREATE')


if __name__ == '__main__':
    unittest.main()
