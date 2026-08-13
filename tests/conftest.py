from __future__ import annotations

import os
import tempfile
from pathlib import Path


# The release tree is immutable and its audit ledger requires exact file parity.
# Hypothesis therefore stores its examples/constants in the host temp area.
os.environ.setdefault(
    "HYPOTHESIS_STORAGE_DIRECTORY",
    str(Path(tempfile.gettempdir()) / "binana-testnet-hypothesis"),
)
