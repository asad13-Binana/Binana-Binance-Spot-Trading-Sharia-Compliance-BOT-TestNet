"""Read retired source bytes from local Git objects, without restoring or running them."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys


def resolve(root: Path, relative: str, expected_sha256: str) -> bytes:
    path = PurePosixPath(relative)
    if not relative or path.is_absolute() or ".." in path.parts or "\\" in relative or path.as_posix() != relative:
        raise ValueError("Invalid historical source path")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("Invalid expected SHA256")
    record = json.loads((root / "docs/retirement/retired_sources.json").read_text(encoding="utf-8"))
    item = record["files"].get(relative)
    if not item or item["sha256"] != expected_sha256:
        raise ValueError("Unregistered source path or SHA256 mismatch")
    commit = record["baseline_commit"]
    blob = item["git_blob"]
    if not re.fullmatch(r"[0-9a-f]{40}", commit) or not re.fullmatch(r"[0-9a-f]{40}", blob):
        raise ValueError("Invalid immutable Git identity")
    def read(*args):
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=20)
        if result.returncode:
            raise ValueError("Historical Git object unavailable; obtain the recorded baseline history explicitly. No fetch was attempted.")
        return result.stdout
    actual_blob = read("rev-parse", "--verify", commit + ":" + relative).decode("ascii").strip()
    if actual_blob != blob:
        raise ValueError("Historical Git tree/blob mismatch")
    raw = read("cat-file", "blob", blob)
    if hashlib.sha256(raw).hexdigest() != expected_sha256 or len(raw) != item["bytes"]:
        raise ValueError("Historical source bytes failed verification")
    return raw


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--path", required=True)
    parser.add_argument("--expected-sha256", required=True)
    args = parser.parse_args()
    try:
        raw = resolve(args.root.resolve(), args.path, args.expected_sha256)
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        return 1
    sys.stdout.buffer.write(raw)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
