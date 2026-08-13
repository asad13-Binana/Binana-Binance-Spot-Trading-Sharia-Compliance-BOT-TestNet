#!/usr/bin/env python3
"""Generate detached, machine-readable CI evidence for one release artifact.

The evidence file is intentionally outside the release archive so it can bind
the archive's final SHA-256 without creating a circular self-hash.
"""
from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _protected_mapping(root: Path) -> dict[str, str]:
    source = (root / "tests/test_oracle_hardening_v2.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "PROTECTED"
                for target in node.targets):
            value = ast.literal_eval(node.value)
            if isinstance(value, dict) and value:
                return {str(key): str(expected) for key, expected in value.items()}
    raise RuntimeError("protected hash inventory was not found")


def _verify_protected(root: Path) -> dict[str, str]:
    verified: dict[str, str] = {}
    for relative, expected in sorted(_protected_mapping(root).items()):
        actual = _sha256(root / relative)
        if actual != expected:
            raise RuntimeError(f"protected hash mismatch: {relative}")
        verified[relative] = actual
    return verified


def _data_rows(path: Path) -> int:
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    return max(0, len(rows) - 1)


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix="." + path.name + ".", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def build_evidence(args: argparse.Namespace) -> dict:
    root = args.root.resolve()
    artifact = args.artifact.resolve()
    if args.package_mode not in {"testnet", "live"}:
        raise ValueError("package mode must be testnet or live")
    if len(args.commit_sha) != 40 or any(ch not in "0123456789abcdef" for ch in args.commit_sha):
        raise ValueError("commit SHA must be 40 lowercase hexadecimal characters")
    protected = _verify_protected(root)
    return {
        "schema_version": 1,
        "evidence_kind": "detached_ci_release_evidence",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repository": args.repository,
        "commit_sha": args.commit_sha,
        "package_mode": args.package_mode,
        "workflow": {
            "run_id": args.workflow_run_id,
            "run_url": args.workflow_run_url,
            "artifact_job": "PASSED_TO_EVIDENCE_GENERATION_STEP",
            "scope_note": (
                "This statement is generated after release-artifact verification. "
                "It does not claim Oracle deployment or authenticated exchange execution."
            ),
        },
        "artifact": {
            "name": artifact.name,
            "sha256": _sha256(artifact),
            "size_bytes": artifact.stat().st_size,
        },
        "protected_hashes": protected,
        "inventory": {
            "file_records": _data_rows(root / "docs/audit/FILE_REVIEW_LEDGER.csv"),
            "function_records": _data_rows(root / "docs/audit/FUNCTION_CALLBACK_LEDGER.csv"),
        },
        "remaining_external_gates": [
            "authenticated Binance Spot Testnet lifecycle certification",
            "real Oracle host deployment and fault/soak validation",
            "owner performance acceptance",
            "separate signed LIVE promotion approval",
        ],
        "production_live_certified": False,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--package-mode", required=True)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--workflow-run-id", required=True)
    parser.add_argument("--workflow-run-url", required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    _atomic_json(args.output, build_evidence(args))


if __name__ == "__main__":
    main()
