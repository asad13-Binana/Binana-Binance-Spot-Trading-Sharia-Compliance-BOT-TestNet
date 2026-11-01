"""Bounded regression for upstream pypdf GHSA-5849-834g-hvw5."""
import subprocess
import sys
import unittest


class PdfParserSecurityTests(unittest.TestCase):
    def test_missing_xref_trailer_is_rejected_without_hanging(self):
        # Upstream regression payload: py-pdf/pypdf pull request 4289.
        script = r"""from services.sharia_retriever.retriever import pdf_to_text, RetrievalBlocked
payload = b"%PDF-1.4\nstartxref\n28\n%%EOF\nxref\n1 0\naaa"
try:
    pdf_to_text(payload)
except RetrievalBlocked:
    pass
else:
    raise AssertionError("Malformed PDF was accepted as evidence")
"""
        try:
            result = subprocess.run([sys.executable, '-B', '-c', script],
                                    capture_output=True, text=True, timeout=5)
        except subprocess.TimeoutExpired:
            self.fail('Malformed xref trailer did not terminate within five seconds')
        self.assertEqual(result.returncode, 0, result.stderr)
