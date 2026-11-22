#!/usr/bin/env python3
"""Check the historical four-instance namespace registry; it is not a current capacity approval.

The default check is safe on any checkout and never reads credentials.
The old host/topology verifier is retired. Current standalone Oracle bundles
require their own resource, private-input, image and host acceptance checks.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "deploy" / "four_bot_host_contract.json"
IDENTITY_PATH = ROOT / "deploy" / "instance_identity.sh"
COMPOSE_PATH = ROOT / "docker-compose.yml"

IDENTITY_FIELDS = {
    "slug": "INSTANCE_SLUG",
    "release_mode": "INSTANCE_MODE",
    "compose_project": "COMPOSE_PROJECT_NAME",
    "service_image": "SERVICE_IMAGE",
    "app_root": "APP_ROOT",
    "private_root": "PRIVATE_ROOT",
    "persistent_root": "PERSIST_PARENT",
    "bot_user": "BOT_USER",
    "monitor_user": "MONITOR_USER",
    "systemd_prefix": "SYSTEMD_PREFIX",
    "monitor_port": "EXPECTED_MONITOR_PORT",
}

UNIQUE_FIELDS = (
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
)


class ContractError(RuntimeError):
    pass


def load_contract() -> dict[str, Any]:
    data = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1:
        raise ContractError("unsupported four-bot contract schema")
    instances = data.get("instances")
    if not isinstance(instances, list) or len(instances) != 4:
        raise ContractError("contract must define exactly four instances")
    return data


def parse_identity() -> dict[str, str]:
    values: dict[str, str] = {}
    pattern = re.compile(r"^readonly ([A-Z][A-Z0-9_]*)=(.*)$")
    for raw in IDENTITY_PATH.read_text(encoding="utf-8").splitlines():
        match = pattern.fullmatch(raw.strip())
        if not match:
            continue
        key, value = match.groups()
        if "$" in value:
            continue
        values[key] = value.strip().strip("'\\\"")
    return values


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    pattern = re.compile(r"([A-Z][A-Z0-9_]*)=(.*)")
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = pattern.fullmatch(line)
        if not match:
            raise ContractError(f"{path}: invalid KEY=VALUE line {number}")
        key, value = match.groups()
        if key in values:
            raise ContractError(f"{path}: duplicate key {key}")
        values[key] = value.strip().strip("'\\\"")
    return values


def local_instance(contract: dict[str, Any], identity: dict[str, str]) -> dict[str, Any]:
    slug = identity.get("INSTANCE_SLUG")
    matches = [item for item in contract["instances"] if item["slug"] == slug]
    if len(matches) != 1:
        raise ContractError("local INSTANCE_SLUG is absent or ambiguous in the contract")
    return matches[0]


def check_uniqueness(contract: dict[str, Any]) -> None:
    instances = contract["instances"]
    for field in UNIQUE_FIELDS:
        values = [str(item[field]) for item in instances]
        if len(values) != len(set(values)):
            raise ContractError(f"contract field is not unique: {field}")
    ports = sorted(int(item["monitor_port"]) for item in instances)
    if ports != [8090, 8091, 8092, 8093]:
        raise ContractError("monitor ports must be the reserved 8090-8093 sequence")
    host = contract["host_profile"]
    cpu = round(sum(float(item["container_cpu_limit"]) for item in instances), 6)
    memory = sum(int(item["container_memory_mib"]) for item in instances)
    if cpu != float(host["maximum_aggregate_container_cpus"]):
        raise ContractError("aggregate CPU limit does not reconcile")
    if memory != int(host["maximum_aggregate_container_memory_mib"]):
        raise ContractError("aggregate memory limit does not reconcile")


def memory_to_mib(value: str, unit: str) -> int:
    amount = int(value)
    return amount if unit.lower() == "m" else amount * 1024


def check_local_static(contract: dict[str, Any]) -> str:
    identity = parse_identity()
    item = local_instance(contract, identity)
    for contract_key, identity_key in IDENTITY_FIELDS.items():
        actual = identity.get(identity_key)
        expected = str(item[contract_key])
        if actual != expected:
            raise ContractError(
                f"identity mismatch for {identity_key}: expected {expected}, found {actual}"
            )

    env = parse_env(ROOT / ".env.example")
    expected_shared = f"{item['persistent_root']}/shared"
    if env.get("SHARED_HOST_PATH") != expected_shared:
        raise ContractError(".env.example does not bind the dedicated persistent root")

    if item["family"] == "binana":
        relevant_modes = (item["release_mode"],)
    elif item["release_mode"] == "testnet":
        relevant_modes = ("simulation", "testnet")
    else:
        relevant_modes = ("simulation", "live")
    for mode in relevant_modes:
        template = ROOT / "monitoring" / f".env.monitor.{mode}.example"
        values = parse_env(template)
        if values.get("MONITOR_PORT") != str(item["monitor_port"]):
            raise ContractError(f"{template.name} has a colliding monitor port")
        for key in ("BOT_DIRECTORY", "MONITOR_SHARED_ROOT", "MONITOR_AUDIT_LOG"):
            value = values.get(key, "")
            if item["slug"] not in value:
                raise ContractError(f"{template.name} is not namespaced: {key}")

    return item["slug"]


def read_meminfo() -> tuple[int, int]:
    values: dict[str, int] = {}
    for raw in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        key, value = raw.split(":", 1)
        values[key] = int(value.strip().split()[0]) // 1024
    return values.get("MemTotal", 0), values.get("SwapTotal", 0)


def canonical_directory(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink() and path.resolve() == path


def check_host(contract: dict[str, Any]) -> None:
    raise ContractError('LEGACY_HOST_TOPOLOGY_RETIRED: qualify each standalone Oracle bundle; historical resource totals do not approve co-hosting')


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--host",
        action="store_true",
        help="retired host qualification; rejects until each standalone bundle is independently qualified",
    )
    args = parser.parse_args()
    try:
        contract = load_contract()
        check_uniqueness(contract)
        slug = check_local_static(contract)
        if args.host:
            check_host(contract)
    except (ContractError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    print(f"OK: historical four-bot namespace contract verified for {slug}")
    if args.host:
        print("OK: Oracle host resources, identities, paths and credential separation verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
