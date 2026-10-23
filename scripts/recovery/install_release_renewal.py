"""Install host-specific renewal tooling; roll back files and timer state on failure."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import deploy_candidate_owner as deploy
from release_host import HostContext, checked_path

DESTINATION = Path("/usr/local/lib/binana-release-maintenance")
UNIT_DIR = Path("/etc/systemd/system")
FILES = ("scripts/recovery/issue_candidate_release.py", "scripts/recovery/deploy_candidate_owner.py",
         "scripts/recovery/release_host.py", "runtime_owner/deployment/release_authority.py")


def unit_state(unit, operation):
    result = subprocess.run(["systemctl", operation, unit], capture_output=True, timeout=30)
    value = result.stdout.decode().strip()
    allowed = ({"enabled", "disabled", "not-found"} if operation == "is-enabled"
               else {"active", "inactive", "failed", "unknown"})
    if value not in allowed or result.returncode not in {0, 1, 3, 4}:
        raise RuntimeError("UNREADABLE_RENEWAL_UNIT_STATE")
    return value


def stop_unit(unit):
    if unit_state(unit, "is-active") != "unknown":
        deploy.run(["systemctl", "stop", unit])
    if unit_state(unit, "is-active") == "active":
        raise RuntimeError("RENEWAL_UNIT_STILL_ACTIVE")


def prepare_parent(path):
    parent = path.parent
    existing = parent
    while not existing.exists():
        existing = existing.parent
    checked_path(existing, trusted=True)
    parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    checked_path(parent, trusted=True)
    if path.is_symlink():
        raise RuntimeError("ISSUER_DESTINATION_SYMLINK")


def install(context, evidence, rollout):
    destination = DESTINATION
    # Each project has its own tooling, unit names and profile. Tests supply an
    # already isolated destination; the production prefix gets a project child.
    if destination == Path("/usr/local/lib/binana-release-maintenance"):
        destination = destination / context.project
    prefix = context.project + "-release-renewal"
    service_name, timer_name = prefix + ".service", prefix + ".timer"
    timer_enabled = unit_state(timer_name, "is-enabled")
    timer_active = unit_state(timer_name, "is-active")
    if unit_state(service_name, "is-active") == "active":
        raise RuntimeError("RENEWAL_ALREADY_RUNNING")
    profile = destination / "host.json"
    command = (str(destination / "scripts/recovery/issue_candidate_release.py") +
               " --host " + str(profile) + " --evidence " + str(evidence) +
               " --rollout " + str(rollout) + " --renew")
    service = """[Unit]
Description=Renew unchanged qualified BINANA Spot Testnet release
After=docker.service network-online.target
Requires=docker.service

[Service]
Type=oneshot
User=root
Group=root
UMask=0077
TimeoutStartSec=2400
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=""" + str(deploy.RELEASE) + " " + str(context.paths["evidence_root"]) + """
ExecStart=/usr/bin/python3 """ + command + "\n"
    timer = """[Unit]
Description=Check BINANA release renewal daily before expiry

[Timer]
OnCalendar=*-*-* 03:45:00 UTC
RandomizedDelaySec=10m
Persistent=true
Unit=""" + service_name + """

[Install]
WantedBy=timers.target
"""
    updates = {destination / relative: ((deploy.REPO / relative).read_bytes(), 0o644) for relative in FILES}
    updates.update({profile: ((json.dumps(context.data, sort_keys=True) + "\n").encode(), 0o600),
        UNIT_DIR / service_name: (service.encode(), 0o644), UNIT_DIR / timer_name: (timer.encode(), 0o644)})
    snapshots = {}
    # A successful oneshot followed by timer failure must not leave a changed
    # certificate attached to a failed installation.
    certificate = deploy.RELEASE / "certificate.json"
    for path in (*updates, certificate):
        prepare_parent(path)
        if path.exists():
            checked_path(path, trusted=True)
            snapshots[path] = (path.read_bytes(), path.stat().st_mode & 0o777)
        else:
            snapshots[path] = None
    try:
        stop_unit(timer_name)
        for path, (raw, mode) in updates.items():
            deploy.authority.atomic(path, raw, mode=mode)
        deploy.run(["systemctl", "daemon-reload"])
        deploy.run(["systemctl", "start", service_name], timeout=2410)
        deploy.run(["systemctl", "enable", "--now", timer_name])
        if (unit_state(timer_name, "is-enabled") != "enabled"
                or unit_state(timer_name, "is-active") != "active"):
            raise RuntimeError("RENEWAL_TIMER_NOT_ACTIVE")
        report = {"passed": True, "source_sha256": {
            relative: hashlib.sha256(updates[destination / relative][0]).hexdigest() for relative in FILES},
            "host_profile_sha256": hashlib.sha256(updates[profile][0]).hexdigest(),
            "evidence": str(evidence), "rollout": str(rollout), "timer": timer_name,
            "trading_resumed": False}
        deploy.authority.atomic(evidence / "renewal-installation.json",
            (json.dumps(report, indent=2) + "\n").encode(), mode=0o600)
    except BaseException:
        try:
            for unit in (timer_name, service_name):
                stop_unit(unit)
            if unit_state(timer_name, "is-enabled") != "not-found":
                deploy.run(["systemctl", "disable", timer_name])
            for path, snapshot in snapshots.items():
                if snapshot is None:
                    if path.is_symlink():
                        raise RuntimeError("ROLLBACK_DESTINATION_SYMLINK")
                    path.unlink(missing_ok=True)
                else:
                    deploy.authority.atomic(path, snapshot[0], mode=snapshot[1])
            deploy.run(["systemctl", "daemon-reload"])
            if timer_enabled == "enabled":
                deploy.run(["systemctl", "enable", timer_name])
            if timer_active == "active":
                deploy.run(["systemctl", "start", timer_name])
            if (unit_state(timer_name, "is-active") == "active") != (timer_active == "active"):
                raise RuntimeError("ROLLBACK_TIMER_STATE")
            if (unit_state(timer_name, "is-enabled") == "enabled") != (timer_enabled == "enabled"):
                raise RuntimeError("ROLLBACK_TIMER_STATE")
        except BaseException as rollback_error:
            raise RuntimeError("RENEWAL_INSTALL_ROLLBACK_FAILED") from rollback_error
        raise
    return report


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--rollout", type=Path, required=True)
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        raise RuntimeError("ROOT_REQUIRED")
    os.umask(0o077)
    context = HostContext(args.host, deploy).activate()
    evidence, rollout = context.evidence_paths(args.evidence, args.rollout)
    if json.loads(rollout.read_text()).get("passed") is not True:
        raise RuntimeError("SUCCESSFUL_ROLLOUT_REQUIRED")
    print(json.dumps(install(context, evidence, rollout)))


if __name__ == "__main__":
    main()
