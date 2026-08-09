"""Shared fail-closed exception for the self-hosted Sharia screener.

The former externally billed model runner was deliberately removed. The
active and only selectable backend is ``local-oracle-v1`` in
``local_runner.py``. This small compatibility module keeps the established
exception import stable without retaining any API client, credential, model,
host, or HTTP execution path.
"""
from __future__ import annotations


class ScreeningUnavailable(RuntimeError):
    """Local screening could not complete; callers must deny the trade."""
