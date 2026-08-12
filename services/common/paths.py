from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(os.getenv('SHARED_ROOT', '/app/shared'))
SIGNAL_INBOX = Path(os.getenv('SIGNAL_INBOX', ROOT / 'signals/inbox'))
SIGNAL_PROCESSED = Path(os.getenv('SIGNAL_PROCESSED', ROOT / 'signals/processed'))
SIGNAL_REJECTED = Path(os.getenv('SIGNAL_REJECTED', ROOT / 'signals/rejected'))
UNIVERSE_CURRENT = Path(os.getenv('UNIVERSE_FILE', ROOT / 'universe/current_pairlist.json'))
SHARIA_FILE = Path(os.getenv('SHARIA_FILE', ROOT / 'sharia/sharia_status.json'))
LEGACY_HALAL_FILE = Path(os.getenv('LEGACY_HALAL_FILE', ROOT / 'sharia/halal_coins.json'))
RUNTIME = Path(os.getenv('RUNTIME_DIR', ROOT / 'runtime'))
COMMAND_INBOX = Path(os.getenv('COMMAND_INBOX', ROOT / 'commands/inbox'))
AUDIT_DIR = Path(os.getenv('AUDIT_DIR', ROOT / 'audit'))
TELEGRAM_ALERT_OUTBOX = Path(os.getenv(
    'TELEGRAM_ALERT_OUTBOX', ROOT / 'telegram_alerts/outbox'))

# V19.1 Sharia screening buses. The canonical Sharia directory is writable by
# the sharia-screener service ONLY; every other container mounts it read-only.
SHARIA_DIR = SHARIA_FILE.parent
SHARIA_CONTROLLER_FILE = Path(os.getenv(
    'SHARIA_CONTROLLER_FILE', SHARIA_DIR / 'HALAL_CRYPTO_SPOT_SCREENING_V19_1_PRODUCTION.json'))
SHARIA_QUEUE_INBOX = Path(os.getenv('SHARIA_QUEUE_INBOX', ROOT / 'sharia_queue/inbox'))
SHARIA_QUEUE_PROCESSED = Path(os.getenv('SHARIA_QUEUE_PROCESSED', ROOT / 'sharia_queue/processed'))
SHARIA_DECISION_INBOX = Path(os.getenv(
    'SHARIA_DECISION_INBOX', ROOT / 'sharia_decisions/inbox'))
SHARIA_DECISION_PROCESSED = Path(os.getenv(
    'SHARIA_DECISION_PROCESSED', ROOT / 'sharia_decisions/processed'))
SHARIA_RESULTS_DIR = Path(os.getenv('SHARIA_RESULTS_DIR', SHARIA_DIR / 'results'))
SHARIA_REPORTS_DIR = Path(os.getenv('SHARIA_REPORTS_DIR', SHARIA_DIR / 'reports'))
SHARIA_EVIDENCE_DIR = Path(os.getenv(
    'SHARIA_EVIDENCE_DIR', SHARIA_DIR / 'evidence'))
SHARIA_DISCOVERY_DIR = Path(os.getenv(
    'SHARIA_DISCOVERY_DIR', SHARIA_DIR / 'discovery'))
SHARIA_DISCOVERY_CURRENT_DIR = Path(os.getenv(
    'SHARIA_DISCOVERY_CURRENT_DIR', SHARIA_DISCOVERY_DIR / 'current'))
SHARIA_DISCOVERY_ARCHIVE_DIR = Path(os.getenv(
    'SHARIA_DISCOVERY_ARCHIVE_DIR', SHARIA_DISCOVERY_DIR / 'archive'))
SHARIA_SOURCE_REGISTRY = Path(os.getenv(
    'SHARIA_SOURCE_REGISTRY', SHARIA_DIR / 'source_registry.json'))
SHARIA_RUNTIME_DIR = Path(os.getenv('SHARIA_RUNTIME_DIR', RUNTIME / 'sharia_screener'))

for p in [SIGNAL_INBOX, SIGNAL_PROCESSED, SIGNAL_REJECTED, UNIVERSE_CURRENT.parent,
          SHARIA_FILE.parent, LEGACY_HALAL_FILE.parent, RUNTIME, COMMAND_INBOX, AUDIT_DIR,
          SHARIA_QUEUE_INBOX, SHARIA_QUEUE_PROCESSED, SHARIA_RESULTS_DIR,
          SHARIA_DECISION_INBOX, SHARIA_DECISION_PROCESSED,
          SHARIA_REPORTS_DIR, SHARIA_EVIDENCE_DIR, SHARIA_DISCOVERY_CURRENT_DIR,
          SHARIA_DISCOVERY_ARCHIVE_DIR, SHARIA_RUNTIME_DIR,
          SHARIA_SOURCE_REGISTRY.parent, TELEGRAM_ALERT_OUTBOX]:
    # Best effort: services with read-only mounts cannot (and must not)
    # create directories owned by other services; the owning service and the
    # installer guarantee their own paths.
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
