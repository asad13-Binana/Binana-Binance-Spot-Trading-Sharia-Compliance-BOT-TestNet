#!/usr/bin/env python3
"""Fail CI when a workflow uses an unverified or mutable external action."""

from __future__ import annotations

import re
import sys
from pathlib import Path

# These commit IDs were resolved from the named tags in the official action
# repositories on 2026-08-25. Updating an action requires independently
# resolving its official release tag and changing this allowlist in review.
VERIFIED_ACTION_PINS = {
    "actions/checkout": "11bd71901bbe5b1630ceea73d27597364c9af683",  # v4.2.2
    "actions/setup-python": "0b93645e9fea7318ecaed2b359559ac225c90a2b",  # v5.3.0
    "actions/upload-artifact": "6f51ac03b9356f520e9adb1b1b7802705f340c2b",  # v4.5.0
    "actions/download-artifact": "fa0a91b85d4f404e444e00e005971372dc801d16",  # v4.1.8
}

USES_LINE = re.compile(r"^\s*(?:-\s*)?uses:\s*[\"']?([^\"'\s#]+)")
COMMIT_SHA = re.compile(r"[0-9a-f]{40}")
DOCKER_DIGEST = re.compile(r"docker://.+@sha256:[0-9a-f]{64}")


def workflow_pin_errors(text: str, source: str = "<workflow>") -> tuple[list[str], int]:
    """Return validation errors and the number of external references found."""

    errors: list[str] = []
    references = 0
    for line_number, line in enumerate(text.splitlines(), start=1):
        match = USES_LINE.match(line)
        if match is None:
            continue

        target = match.group(1)
        if target.startswith("./"):
            continue

        references += 1
        location = f"{source}:{line_number}"

        if target.startswith("docker://"):
            if DOCKER_DIGEST.fullmatch(target) is None:
                errors.append(f"{location}: Docker action is not pinned by sha256 digest: {target}")
            continue

        if "@" not in target:
            errors.append(f"{location}: external action has no immutable commit pin: {target}")
            continue

        action, revision = target.rsplit("@", 1)
        action_key = action.lower()
        expected = VERIFIED_ACTION_PINS.get(action_key)
        if expected is None:
            errors.append(
                f"{location}: external action is not in the verified allowlist: {action}"
            )
            continue
        if COMMIT_SHA.fullmatch(revision) is None:
            errors.append(
                f"{location}: {action} must use a lowercase 40-character commit SHA, got {revision}"
            )
            continue
        if revision != expected:
            errors.append(
                f"{location}: {action} uses unverified SHA {revision}; expected {expected}"
            )

    return errors, references


def verify_repository(repo_root: Path) -> tuple[list[str], int, int]:
    """Validate every YAML workflow/composite action under ``.github``."""

    github_root = repo_root / ".github"
    workflow_files = sorted(
        path
        for pattern in ("*.yml", "*.yaml")
        for path in github_root.rglob(pattern)
        if path.is_file()
    )
    if not workflow_files:
        return [f"{github_root}: no GitHub workflow YAML files found"], 0, 0

    errors: list[str] = []
    references = 0
    for path in workflow_files:
        relative = path.relative_to(repo_root).as_posix()
        file_errors, file_references = workflow_pin_errors(
            path.read_text(encoding="utf-8"), relative
        )
        errors.extend(file_errors)
        references += file_references
    return errors, len(workflow_files), references


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    repo_root = Path(args[0]).resolve() if args else Path.cwd().resolve()
    errors, files, references = verify_repository(repo_root)
    if errors:
        print("GitHub Action pin verification FAILED:")
        for error in errors:
            print(f"- {error}")
        return 1

    print(
        "GitHub Action pin verification passed: "
        f"{references} external references across {files} YAML files"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
