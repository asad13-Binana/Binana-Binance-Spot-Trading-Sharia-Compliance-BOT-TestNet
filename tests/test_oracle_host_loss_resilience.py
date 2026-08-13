import hashlib
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROTECTED = {
    "freqtrade/user_data/strategies/IctSmcStrategy.py":
        "9f6bafc78c8cd0d9b9cbde615ddce89e304ab09738584b88d05bfdf92ff4e830",
    "legacy_core/binance_bot_V4.9.16_ALL_IN_ONE.py":
        "70b1d67cc0092b5b8db4a68b343cf893641bde1aae580e9ef51e2adec1062459",
    "services/common/sharia_v19.py":
        "5eb9fd5338d80fcaf0d39bb3f4935a75b57dd91136c72a83a7551b659b04d865",
}


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_protected_core_hashes_are_unchanged():
    for relative, expected in PROTECTED.items():
        assert hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() == expected


def test_offhost_backup_is_encrypted_immutable_and_instance_principal_only():
    script = read("deploy/offhost_backup.sh")
    assert "age --encrypt --recipient" in script
    assert "OCI_CLI_AUTH=instance_principal" in script
    assert "--auth instance_principal" in script
    assert "--no-overwrite" in script
    assert "--no-multipart" in script
    assert "--verify-checksum" in script
    assert "--opc-checksum-algorithm SHA256" in script
    assert "--opc-content-sha256" in script
    assert "os.chown(temporary, 0, path.parent.stat().st_gid)" in script
    assert "os.chmod(temporary, 0o640)" in script
    assert "os.chmod(temporary, 0o600)" not in script
    assert "os object get" in script
    assert "verified-download.age" in script
    assert "--pull never" in script and "--read-only" in script
    assert "--cap-drop ALL" in script and "no-new-privileges" in script
    assert "api_key" not in script.lower()
    assert not re.search(r"os\s+object\s+(delete|rename|restore)", script)


def test_oci_cli_container_is_digest_pinned_for_both_supported_architectures():
    script = read("deploy/offhost_backup.sh")
    assert "linux/arm64" not in script  # architecture selected by dpkg, not an unverified tag
    digests = re.findall(r"ghcr\.io/oracle/oci-cli:[^'\"]+@sha256:[0-9a-f]{64}", script)
    assert len(set(digests)) == 2


def test_private_recovery_identity_is_not_configured_on_the_vm():
    template = read("deploy/offhost-backup.env.example")
    assert "AGE_RECIPIENT=" in template
    assert "AGE_IDENTITY" not in template
    assert "PRIVATE_KEY" not in template
    assert "OFFHOST_BACKUP_ENABLED=false" in template


def test_restore_is_staging_only_and_rejects_unsafe_archives():
    restore = read("deploy/stage_offhost_restore.sh")
    assert "live state was not modified" in restore
    assert "path.is_absolute()" in restore
    assert '".." in path.parts' in restore
    assert "member.isfile() or member.isdir()" in restore
    assert "--keep-old-files" in restore
    assert "--no-same-owner" in restore
    assert "restore_validate.sh" in restore
    assert "/var/lib/binana-freqtrade-v101/shared" not in restore


def test_systemd_orders_local_then_offhost_backup_and_does_not_auto_enable_it():
    local_timer = read("monitoring/systemd/binana-state-backup.timer")
    offhost_service = read("monitoring/systemd/binana-offhost-backup.service")
    offhost_timer = read("monitoring/systemd/binana-offhost-backup.timer")
    installer = read("deploy/install_monitoring.sh")
    assert "binana-state-backup.service" in local_timer
    assert "After=docker.service binana-state-backup.service network-online.target" in offhost_service
    assert "Persistent=true" in offhost_timer
    assert "binana-offhost-backup.service" in offhost_timer
    assert '"$UNIT_DIR/binana-offhost-backup.service"' in installer
    assert "enable --now binana-offhost-backup.timer" not in installer


def test_external_alarm_uses_oracles_grouped_absence_query_and_no_instance_auth():
    alarm = read("deploy/create_oci_host_loss_alarm.sh")
    assert ".groupBy(resourceId).absent(15m)" in alarm
    assert "--namespace oci_computeagent" in alarm
    assert "--severity CRITICAL" in alarm
    assert "--pending-duration PT5M" in alarm
    assert "--repeat-notification-duration PT6H" in alarm
    assert "OCI_CLI_AUTH:-} != instance_principal" in alarm


def test_no_artificial_activity_or_false_permanence_claim():
    combined = "\n".join(
        read(path) for path in (
            "deploy/offhost_backup.sh",
            "deploy/create_oci_host_loss_alarm.sh",
            "docs/ORACLE_HOST_LOSS_RESILIENCE.md",
        )
    ).lower()
    for forbidden in ("stress-ng", "cpuburn", "keepalive traffic"):
        assert forbidden not in combined
    assert "make an always free vm permanent" in combined
    assert "never generate artificial cpu or network traffic" in combined
