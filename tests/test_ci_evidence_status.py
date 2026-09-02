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


def test_strategy_probe_and_offline_compose_use_runtime_image_digest():
    digest = (
        "freqtradeorg/freqtrade:2026.6@sha256:"
        "d451af021d5e08b70580c0eea5848534e9846b57391b34821c0a5814416397e6"
    )
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    offline_compose = (ROOT / "freqtrade/docker-compose.yml").read_text(encoding="utf-8")
    runtime_compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    runtime_wrapper = (ROOT / "Dockerfile.freqtrade").read_text(encoding="utf-8")
    assert digest in workflow
    assert digest in offline_compose
    assert "dockerfile: Dockerfile.freqtrade" in runtime_compose
    assert digest in runtime_wrapper
    assert "Generate detached machine-readable release evidence" in workflow
