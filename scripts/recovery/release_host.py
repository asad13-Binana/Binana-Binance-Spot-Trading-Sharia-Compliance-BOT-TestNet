"""Validated host paths and Compose identity for the existing release issuer.

This adapter performs metadata validation only. Signing and the read-only owner
query remain in the existing recovery tools; it has no deployment entry point.
"""
import json
from pathlib import Path
import re
import stat


def checked_path(value, *, trusted=False, private=False):
    path = Path(value)
    if (not path.is_absolute() or ".." in path.parts
            or not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(path))):
        raise RuntimeError("UNEXPECTED_HOST_PATH")
    for part in (path, *path.parents):
        if part.is_symlink():
            raise RuntimeError("HOST_PATH_SYMLINK")
        info = part.stat()
        sticky_parent = part != path and stat.S_ISDIR(info.st_mode) and info.st_mode & stat.S_ISVTX
        if trusted and (info.st_uid != 0 or (info.st_mode & 0o022 and not sticky_parent)):
            raise RuntimeError("TRUSTED_HOST_PATH_REQUIRED")
    if private and path.stat().st_mode & 0o077:
        raise RuntimeError("PRIVATE_HOST_PATH_REQUIRED")
    return path


class HostContext:
    def __init__(self, profile, deployment):
        self.profile = checked_path(profile, trusted=True, private=True)
        self.data = json.loads(self.profile.read_text())
        expected = {"schema", "project", "service", "config", "shared", "release", "issuer", "evidence_root"}
        if set(self.data) != expected or self.data["schema"] != "binana-release-host-v1":
            raise RuntimeError("RELEASE_HOST_SCHEMA")
        for name in ("project", "service"):
            value = self.data[name]
            if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", value):
                raise RuntimeError("COMPOSE_IDENTITY_INVALID")
        self.project, self.service = self.data["project"], self.data["service"]
        self.paths = {name: checked_path(self.data[name], trusted=name != "shared",
                                        private=name in {"issuer", "evidence_root"})
                      for name in ("config", "shared", "release", "issuer", "evidence_root")}
        roots = (self.paths["config"].parent, self.paths["shared"], self.paths["release"],
                 self.paths["evidence_root"])
        if any(a.is_relative_to(b) or b.is_relative_to(a)
               for i, a in enumerate(roots) for b in roots[i + 1:]):
            raise RuntimeError("HOST_DIRECTORIES_OVERLAP")
        if any(self.paths["issuer"].is_relative_to(p) for p in roots[:3]):
            raise RuntimeError("ISSUER_RUNTIME_MOUNT")
        self.deployment = deployment

    def activate(self):
        target = self.deployment
        target.CONFIG, target.SHARED = self.paths["config"], self.paths["shared"]
        target.DB = checked_path(target.SHARED / "freqtrade")
        target.RELEASE, target.ISSUER = self.paths["release"], self.paths["issuer"]
        target.EVIDENCE_ROOT = self.paths["evidence_root"]
        target.HOST_CONTEXT = self
        return self

    def evidence_paths(self, evidence, rollout):
        evidence = checked_path(evidence, trusted=True, private=True)
        rollout = checked_path(rollout, trusted=True)
        if not evidence.is_dir() or not rollout.is_file() or not all(
                p.is_relative_to(self.paths["evidence_root"]) for p in (evidence, rollout)):
            raise RuntimeError("PRIVATE_EVIDENCE_BOUNDARY_REQUIRED")
        for name in ("lifecycle", "one_slot", "four_slot", "restore", "offline"):
            checked_path(evidence / (name + ".json"), trusted=True)
        return evidence, rollout

    def inspect(self):
        identifiers = self.deployment.run(["docker", "ps", "--filter",
            "label=com.docker.compose.project=" + self.project, "--filter",
            "label=com.docker.compose.service=" + self.service, "--format", "{{.ID}}"]).decode().split()
        if len(identifiers) != 1:
            raise RuntimeError("EXACTLY_ONE_OWNER_REQUIRED")
        rows = json.loads(self.deployment.run(["docker", "inspect", identifiers[0]]))
        if len(rows) != 1:
            raise RuntimeError("EXACTLY_ONE_OWNER_REQUIRED")
        container = rows[0]
        labels = container["Config"].get("Labels") or {}
        if (labels.get("com.docker.compose.project") != self.project
                or labels.get("com.docker.compose.service") != self.service
                or container["State"].get("Running") is not True):
            raise RuntimeError("OWNER_IDENTITY_MISMATCH")
        for mount in container["Mounts"]:
            source = checked_path(mount["Source"])
            if self.paths["issuer"] == source or self.paths["issuer"].is_relative_to(source):
                raise RuntimeError("ISSUER_RUNTIME_MOUNT")
        expected = {"/freqtrade/binana-config": (self.paths["config"].parent, False),
            "/freqtrade/binana-release": (self.paths["release"], False),
            "/freqtrade/shared": (self.paths["shared"], False),
            "/freqtrade/user_data": (self.paths["shared"] / "freqtrade", True)}
        for target, (source, writable) in expected.items():
            matches = [m for m in container["Mounts"] if m["Destination"] == target]
            if (len(matches) != 1 or matches[0]["Type"] != "bind"
                    or matches[0]["Source"] != str(source) or matches[0]["RW"] is not writable):
                raise RuntimeError("HOST_MOUNT_MISMATCH")
        self.deployment.OWNER = container["Id"]
        return container
