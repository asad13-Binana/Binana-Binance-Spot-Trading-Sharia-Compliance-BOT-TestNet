from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def test_four_bot_contract_static_validator() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "verify_four_bot_cohost.py")],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK: historical four-bot namespace contract verified" in result.stdout


def test_four_bot_contract_has_no_shared_host_identity() -> None:
    contract = json.loads(
        (ROOT / "deploy" / "four_bot_host_contract.json").read_text(encoding="utf-8")
    )
    instances = contract["instances"]
    assert len(instances) == 4
    for field in (
        "slug",
        "compose_project",
        "service_image",
        "app_root",
        "private_root",
        "persistent_root",
        "monitor_port",
        "bot_user",
        "monitor_user",
        "systemd_prefix",
        "deploy_wrapper",
    ):
        values = [str(instance[field]) for instance in instances]
        assert len(values) == len(set(values)), field


def test_four_bot_resource_envelope_reconciles() -> None:
    contract = json.loads(
        (ROOT / "deploy" / "four_bot_host_contract.json").read_text(encoding="utf-8")
    )
    instances = contract["instances"]
    host = contract["host_profile"]
    assert sum(float(item["container_cpu_limit"]) for item in instances) == 1.8
    assert sum(int(item["container_memory_mib"]) for item in instances) == 5140
    assert host["maximum_aggregate_container_cpus"] == 1.8
    assert host["maximum_aggregate_container_memory_mib"] == 5140
    assert sorted(item["monitor_port"] for item in instances) == [8090, 8091, 8092, 8093]

def _identity_value(name: str) -> str:
    prefix = f"readonly {name}="
    for raw in (ROOT / "deploy" / "instance_identity.sh").read_text(
        encoding="utf-8"
    ).splitlines():
        if raw.startswith(prefix):
            return raw.removeprefix(prefix)
    raise AssertionError(f"missing literal instance identity: {name}")
