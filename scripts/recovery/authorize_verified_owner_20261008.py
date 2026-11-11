"""Record completed Testnet acceptance and resolve only its historical incident.

Requires the actual successful paused rollout receipt. Does not call /start.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time

import deploy_paused_owner_20261008 as deploy

BINDINGS=('code_sha256','config_sha256','exchange_epoch_id','account_key_sha256')
FLAGS=('verified','controlled_lifecycle_verified','restart_verified','backup_restore_verified',
       'critical_suite_10x_verified','one_slot_soak_verified','four_slot_soak_verified')


def certificate_from_status(status,evidence,rollout):
    for key in BINDINGS:
        value=status.get(key)
        if not isinstance(value,str) or not value or (key.endswith('sha256') and not re.fullmatch('[0-9a-f]{64}',value)):
            raise RuntimeError('RUNTIME_BINDING_UNAVAILABLE')
    if set(status.get('blockers',[]))!={'release_evidence_unavailable','RECONCILIATION_BLOCKER_OPEN'}:
        raise RuntimeError('UNEXPECTED_RELEASE_BLOCKERS')
    now=time.time()
    return {**{k:status[k] for k in BINDINGS},**{k:True for k in FLAGS},
            'generated_at':now,'valid_until':now+7*86400,'image':deploy.IMAGE,
            'acceptance_receipt_sha256':evidence,'deployment_receipt':str(rollout),
            'scope':'Binance Spot Testnet existing policy only',
            'limits':['Strategy profitability is not certified.',
                      'Retained aggregates remain gated; executable dust consolidation is not implemented.',
                      'Backup/restore acceptance used flat databases; no active-position restore is claimed.']}


def verified_boot(receipt,expected_container=None):
    before=deploy.inspect()
    fresh=deploy.wait_owner(deploy.IMAGE,candidate=True)
    after=deploy.inspect()
    keys=lambda d:(d['Id'],d['Image'],d['State']['StartedAt'])
    if keys(before)!=keys(after) or (expected_container is not None and keys(after)!=keys(expected_container)):
        raise RuntimeError('OWNER_BOOT_CHANGED')
    expected=receipt['stages'][-1]['readiness']
    if any(fresh['readiness'].get(k)!=expected.get(k) for k in BINDINGS):
        raise RuntimeError('ACCEPTED_RUNTIME_BINDINGS_CHANGED')
    return fresh,after


def main():
    raise SystemExit(64)  # Retired AWS wrapper; retained helpers are regression inputs only.


if __name__=='__main__':
    os.umask(0o077)
    raise SystemExit(main())
