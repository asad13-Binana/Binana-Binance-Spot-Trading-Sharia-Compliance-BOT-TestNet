"""Fail release validation when direct pins and hash-locked closures diverge.

Runtime sources in this repository intentionally use exact name==version pins.
Unsupported requirement syntax fails closed rather than being silently skipped.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

PAIRS = (
    ("requirements.renewal.txt", "requirements.renewal.lock"),
    ("requirements.services.txt", "requirements.services.lock"),
    ("monitoring/requirements-monitoring.txt", "monitoring/requirements-monitoring.lock"),
)
PIN = re.compile(r"([A-Za-z0-9][A-Za-z0-9_.-]*)(?:\[[A-Za-z0-9_,.-]+\])?==([A-Za-z0-9.!+_-]+)")
HASH = re.compile(r"--hash=sha256:[0-9a-f]{64}")
# These are the simple conditional transitive pins in the shipped closures.
# Do not evaluate them on the checking host: that could hide an ARM/Linux pin.
# More complex future marker syntax must receive explicit parser/test support.
MARKER = re.compile(r'''(?:python_version|sys_platform)\s*(?:==|!=|<=|>=|<|>)\s*(["'])[A-Za-z0-9._-]+\1''')


def read_pins(path: Path, *, require_hashes: bool) -> dict[str, str]:
    pins: dict[tuple[str, str], str] = {}
    hashed: set[tuple[str, str]] = set()
    current = None
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].strip().removesuffix("\\").strip()
        if not line:
            continue
        if require_hashes and HASH.fullmatch(line):
            if current is None:
                raise ValueError(f"{path.name}:{number}: hash without a dependency")
            hashed.add(current)
            continue
        requirement, separator, marker = line.partition(";")
        if separator and (not require_hashes or not MARKER.fullmatch(marker.strip())):
            raise ValueError(f"{path.name}:{number}: unsupported conditional pin marker")
        match = PIN.fullmatch(requirement.strip())
        if match is None:
            raise ValueError(f"{path.name}:{number}: expected an exact name==version pin"
                             + (" or a SHA256 hash" if require_hashes else ""))
        name, version = match.groups()
        name = re.sub(r"[-_.]+", "-", name).lower()
        key = (name, marker.strip())
        if key in pins:
            raise ValueError(f"{path.name}:{number}: duplicate dependency {name}")
        pins[key] = version
        current = key
    if not pins:
        raise ValueError(f"{path.name}: no dependency pins")
    if require_hashes and set(pins) != hashed:
        missing = ', '.join(name for name, marker in sorted(set(pins) - hashed))
        raise ValueError(f"{path.name}: missing hashes for {missing}")
    # A conditional transitive lock is checked for pin/hash validity, but cannot
    # satisfy an unconditional direct requirement on every supported platform.
    return {name: version for (name, marker), version in pins.items() if not marker}


def verify(root: Path) -> list[str]:
    problems: list[str] = []
    for source, lock in PAIRS:
        try:
            direct = read_pins(root / source, require_hashes=False)
            locked = read_pins(root / lock, require_hashes=True)
        except (OSError, UnicodeError, ValueError) as exc:
            problems.append(f"{source}: {exc}")
            continue
        for name, version in direct.items():
            installed = locked.get(name)
            if installed != version:
                problems.append(f"{source}: {name} source={version}, lock={installed or 'MISSING'}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    problems = verify(parser.parse_args().root)
    if problems:
        print("FAIL: dependency source/lock consistency")
        print("\n".join(problems))
        return 1
    print("PASS: direct runtime pins match both hash-locked dependency closures")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
