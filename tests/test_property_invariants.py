from __future__ import annotations

import os
from unittest import mock

import pytest
from hypothesis import given, settings, strategies as st

from monitoring.api.log_redaction import redact
from services.common.config_bounds import ConfigError, env_int
from services.common.envelope import canonical_bytes


JSON_SCALAR = st.none() | st.booleans() | st.integers(min_value=-(2**53), max_value=2**53) | st.text(max_size=80)
SAFE_KEYS = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)), min_size=1, max_size=24
)


@settings(max_examples=100, derandomize=True, deadline=None)
@given(st.dictionaries(SAFE_KEYS, JSON_SCALAR, max_size=20))
def test_signed_envelope_canonical_bytes_ignore_mapping_insertion_order(payload):
    reversed_payload = dict(reversed(list(payload.items())))
    assert canonical_bytes(payload) == canonical_bytes(reversed_payload)


@settings(max_examples=80, derandomize=True, deadline=None)
@given(st.integers().filter(lambda value: value < 10 or value > 20))
def test_integer_environment_bounds_always_fail_closed(value: int):
    with mock.patch.dict(os.environ, {"PROPERTY_BOUND": str(value)}, clear=False):
        with pytest.raises(ConfigError):
            env_int("PROPERTY_BOUND", 15, 10, 20)


@settings(max_examples=80, derandomize=True, deadline=None)
@given(st.text(alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd")), min_size=20, max_size=60))
def test_monitor_redaction_never_returns_token_value(secret: str):
    output = redact(f"authorization=Bearer {secret}")
    assert secret not in output
    assert "[REDACTED]" in output
