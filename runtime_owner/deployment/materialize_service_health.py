"""Prepare read-only AWS health-writer overlays without changing trading logic."""
import argparse
import hashlib
import json
from pathlib import Path


BASE = 'runtime_owner/deployed_services/overrides/telegram-broker/services/telegram_broker/bot.py'
BASE_SHA256 = '67daf2644b15909195004f0d718fdabbaeb7405b847f76acdf7eb9a915125ba4'
OLD = b'    atomic_write_json(ALERT_OUTBOX_HEALTH, payload)'
NEW = b'    atomic_write_json(ALERT_OUTBOX_HEALTH, payload, group_readable=True)'
MARKET_BASE = 'runtime_owner/market_context_repair/service.py'
MARKET_SHA256 = 'c32fc22d9423ba6df2b4d905812003be1e7c11f207dae5f8007c026f977d1a60'
SHARIA_SOURCE = 'services/sharia_screener/manual_registry_service.py'
SHARIA_SHA256 = '39af9f36d60e2eb95b159388783a59df5fe6a950aa4e1f846987f37c327da4e9'
SHARIA_PATCH = (b"            'automatic_research_enabled': False,\n            'ts': time.time(),\n        })",
                b"            'automatic_research_enabled': False,\n            'ts': time.time(),\n        }, group_readable=True)")
BROKER_PATCHES = (
    (OLD, NEW),
    (b"                'ok': True, 'ts': time.time(), 'offset': offset,\n            })",
     b"                'ok': True, 'ts': time.time(), 'offset': offset,\n            }, group_readable=True)"),
    (b"                'error_type': type(exc).__name__,\n            })",
     b"                'error_type': type(exc).__name__,\n            }, group_readable=True)"),
)
MARKET_PATCHES = (
    (b'atomic_write_json(self.snapshot_path, snapshot)',
     b'atomic_write_json(self.snapshot_path, snapshot, group_readable=True)'),
    (b'                "statistics": snapshot["statistics"],\n            },\n        )',
     b'                "statistics": snapshot["statistics"],\n            },\n            group_readable=True,\n        )'),
)


def patch(raw: bytes, replacements: tuple) -> bytes:
    for old, new in replacements:
        if raw.count(old) != 1:
            raise ValueError('Reviewed health call does not match exactly once')
        raw = raw.replace(old, new, 1)
    return raw


def materialize(repo: Path, destination: Path) -> dict:
    broker = (repo / BASE).read_bytes()
    if hashlib.sha256(broker).hexdigest() != BASE_SHA256 or broker.count(OLD) != 1:
        raise ValueError('Captured broker source does not match the reviewed base')
    market = (repo / MARKET_BASE).read_bytes()
    if hashlib.sha256(market).hexdigest() != MARKET_SHA256:
        raise ValueError('Market source does not match the reviewed base')
    sharia = (repo / SHARIA_SOURCE).read_bytes()
    old, new = SHARIA_PATCH
    if sharia.count(new) != 1 or hashlib.sha256(sharia.replace(new, old, 1)).hexdigest() != SHARIA_SHA256:
        raise ValueError('Sharia source differs from the reviewed health-only change')
    atomic = (repo / 'services/common/atomic.py').read_bytes()
    if b'group_readable: bool = False' not in atomic:
        raise ValueError('Compatible atomic writer is required')
    outputs = {'broker_base.py': patch(broker, BROKER_PATCHES),
               'market_service.py': patch(market, MARKET_PATCHES),
               'manual_registry_service.py': sharia,
               'common_atomic.py': atomic}
    for name, raw in outputs.items():
        compile(raw, name, 'exec')
    destination.mkdir(exist_ok=False)
    for name, raw in outputs.items():
        (destination / name).write_bytes(raw)
    receipt = {
        'base_broker_sha256': BASE_SHA256,
        'base_market_sha256': MARKET_SHA256,
        'base_sharia_sha256': SHARIA_SHA256,
        'scope': 'Group-readable derived service health and public market data; private state stays private',
        'files': {name: hashlib.sha256(raw).hexdigest() for name, raw in outputs.items()},
    }
    (destination / 'source.json').write_text(json.dumps(receipt, indent=2) + '\n')
    return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    print(json.dumps(materialize(Path(__file__).resolve().parents[2], args.destination)))
