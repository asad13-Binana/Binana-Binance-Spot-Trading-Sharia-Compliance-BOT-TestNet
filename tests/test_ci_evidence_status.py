from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pytest

from scripts.generate_evidence_status import _atomic_json, build_evidence


ROOT = Path(__file__).resolve().parents[1]


def _args(tmp_path: Path, artifact: Path, **changes) -> argparse.Namespace:
    values = {
        "root": ROOT,
        "artifact": artifact,
        "output": tmp_path / "EVIDENCE_STATUS.json",
        "package_mode": "testnet",
        "commit_sha": "a" * 40,
        "repository": "owner/repository",
        "workflow_run_id": "123",
        "workflow_run_url": "https://github.com/owner/repository/actions/runs/123",
    }
    values.update(changes)
    return argparse.Namespace(**values)


def test_detached_evidence_binds_artifact_and_current_protected_hashes(tmp_path):
    artifact = tmp_path / "release.tar.gz"
    artifact.write_bytes(b"immutable release bytes")
    evidence = build_evidence(_args(tmp_path, artifact))
    assert evidence["artifact"]["sha256"] == hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert evidence["inventory"]["file_records"] > 0
    assert evidence["inventory"]["function_records"] > 0
    assert evidence["protected_hashes"]
    assert evidence["production_live_certified"] is False
    assert any("Oracle" in gate for gate in evidence["remaining_external_gates"])


def test_evidence_rejects_invalid_mode_or_commit(tmp_path):
    artifact = tmp_path / "release.tar.gz"
    artifact.write_bytes(b"release")
    with pytest.raises(ValueError, match="package mode"):
        build_evidence(_args(tmp_path, artifact, package_mode="production"))
    with pytest.raises(ValueError, match="commit SHA"):
        build_evidence(_args(tmp_path, artifact, commit_sha="not-a-sha"))


def test_evidence_output_is_valid_json(tmp_path):
    output = tmp_path / "nested/EVIDENCE_STATUS.json"
    _atomic_json(output, {"ok": True})
    assert json.loads(output.read_text(encoding="utf-8")) == {"ok": True}
