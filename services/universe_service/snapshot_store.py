from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import re
from services.common.atomic import atomic_write_json
from services.common.config_bounds import env_int


class UniverseSnapshotError(ValueError):
    """The published universe pointer or immutable snapshot is not trustworthy."""


MAX_CONSUMER_AGE_SECONDS = 86_400
MAX_SNAPSHOT_FILES = 100_000


def _hash(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(',', ':'),
                   allow_nan=False).encode()
    ).hexdigest()


def _snapshot_payload(*, generated_at: str, pairs: list[str], refresh_period: int,
                      configuration: dict, configuration_hash: str,
                      ranking: list[dict], selection: dict) -> dict:
    """The complete semantic payload protected by ``snapshot_hash``."""
    return {
        'generated_at': generated_at,
        'pairs': pairs,
        'refresh_period': refresh_period,
        'configuration': configuration,
        'configuration_hash': configuration_hash,
        'ranking': ranking,
        'selection': selection,
    }


def _validate_publication(rows: list[dict], config: dict, refresh_period: int) -> int:
    if not isinstance(config, dict):
        raise ValueError('universe configuration must be an object')
    target = config.get('limit')
    if isinstance(target, bool) or not isinstance(target, int) or not 1 <= target <= 50:
        raise ValueError('universe configuration limit must be an integer within 1-50')
    if (
        isinstance(refresh_period, bool)
        or not isinstance(refresh_period, int)
        or refresh_period < 1
    ):
        raise ValueError('universe refresh_period must be a positive integer')
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError('universe ranking must be a list of objects')
    if len(rows) > target:
        raise ValueError('universe ranking exceeds configured limit')
    seen_pairs: set[str] = set()
    seen_bases: set[str] = set()
    for expected_rank, row in enumerate(rows, 1):
        pair = row.get('pair')
        if not isinstance(pair, str) or not re.fullmatch(r'[A-Z0-9]+/USDT', pair):
            raise ValueError(f'universe row has invalid pair identity: {pair!r}')
        base = pair.removesuffix('/USDT')
        if row.get('base') is not None and row.get('base') != base:
            raise ValueError(f'universe row base/pair mismatch: {row!r}')
        if row.get('symbol') is not None and row.get('symbol') != base + 'USDT':
            raise ValueError(f'universe row symbol/pair mismatch: {row!r}')
        if (
            isinstance(row.get('rank'), bool)
            or not isinstance(row.get('rank'), int)
            or row.get('rank') != expected_rank
        ):
            raise ValueError('universe ranks must be consecutive and match list order')
        if pair in seen_pairs or base in seen_bases:
            raise ValueError(f'universe ranking contains duplicate identity: {pair}')
        seen_pairs.add(pair)
        seen_bases.add(base)
    return target


def _selection_state(count: int, target: int) -> dict:
    shortfall = max(0, target - count)
    if count == 0:
        state = 'fail_closed_empty'
    elif shortfall:
        state = 'degraded_shortfall'
    else:
        state = 'ready'
    return {
        'state': state,
        'ready': count > 0,
        'degraded': shortfall > 0,
        'complete': shortfall == 0,
        'candidate_count': count,
        'requested_limit': target,
        'shortfall_count': shortfall,
    }


def _parse_generated_at(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise UniverseSnapshotError('universe generated_at must be a non-empty string')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (TypeError, ValueError, OverflowError) as exc:
        raise UniverseSnapshotError('universe generated_at is malformed') from exc
    if parsed.tzinfo is None:
        raise UniverseSnapshotError('universe generated_at must include a timezone')
    return parsed.astimezone(timezone.utc)


def _safe_snapshot_path(pointer_path: Path, value: object) -> Path:
    name = str(value or '')
    if (
        not re.fullmatch(r'universe_[A-Za-z0-9._-]{1,180}\.json', name)
        or '..' in name
        or Path(name).name != name
        or '/' in name
        or '\\' in name
    ):
        raise UniverseSnapshotError('universe snapshot_file is unsafe')
    snapshot_dir = (pointer_path.parent / 'snapshots').resolve()
    candidate = pointer_path.parent / 'snapshots' / name
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise UniverseSnapshotError('referenced universe snapshot is missing') from exc
    if resolved.parent != snapshot_dir or candidate.is_symlink() or not resolved.is_file():
        raise UniverseSnapshotError('referenced universe snapshot escaped snapshots/ or is unsafe')
    return resolved


def load_current(pointer_path: str | Path, *, max_age_seconds: int = 1800,
                 now: datetime | None = None, require_nonempty: bool = True) -> dict:
    """Load and cryptographically bind the current pointer to its full snapshot.

    Consumers never authorize from ``current_pairlist.json`` alone.  The
    pointer must name a safe immutable file inside ``snapshots/``; every shared
    field must match, and both the configuration and complete snapshot hashes
    are recomputed before the pair list is returned.
    """
    if (
        isinstance(max_age_seconds, bool)
        or not isinstance(max_age_seconds, int)
        or not 1 <= max_age_seconds <= MAX_CONSUMER_AGE_SECONDS
    ):
        raise UniverseSnapshotError(
            f'max_age_seconds must be within 1-{MAX_CONSUMER_AGE_SECONDS}')
    pointer_path = Path(pointer_path)
    try:
        pointer = json.loads(pointer_path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise UniverseSnapshotError('universe pointer is missing or malformed') from exc
    if not isinstance(pointer, dict):
        raise UniverseSnapshotError('universe pointer must be an object')
    snapshot_path = _safe_snapshot_path(pointer_path, pointer.get('snapshot_file'))
    try:
        full = json.loads(snapshot_path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise UniverseSnapshotError('referenced universe snapshot is malformed') from exc
    if not isinstance(full, dict):
        raise UniverseSnapshotError('referenced universe snapshot must be an object')

    ranking = full.get('ranking')
    configuration = full.get('configuration')
    refresh_period = full.get('refresh_period')
    try:
        target = _validate_publication(ranking, configuration, refresh_period)
    except (TypeError, ValueError) as exc:
        raise UniverseSnapshotError(str(exc)) from exc
    pairs = [row['pair'] for row in ranking]
    if full.get('pairs') != pairs:
        raise UniverseSnapshotError('universe pairs do not exactly match ranked row order')
    selection = _selection_state(len(pairs), target)
    if full.get('selection') != selection:
        raise UniverseSnapshotError('universe selection state is inconsistent')
    if require_nonempty and not pairs:
        raise UniverseSnapshotError('universe is empty and therefore fail-closed')

    configuration_hash = _hash(configuration)
    if full.get('configuration_hash') != configuration_hash:
        raise UniverseSnapshotError('universe configuration hash mismatch')
    expected_hash = _hash(_snapshot_payload(
        generated_at=full.get('generated_at'), pairs=pairs,
        refresh_period=refresh_period, configuration=configuration,
        configuration_hash=configuration_hash, ranking=ranking,
        selection=selection,
    ))
    if full.get('snapshot_hash') != expected_hash:
        raise UniverseSnapshotError('universe snapshot hash mismatch')
    if expected_hash[:12] not in snapshot_path.name:
        raise UniverseSnapshotError('universe snapshot filename/hash binding mismatch')

    for field in ('pairs', 'refresh_period', 'generated_at', 'configuration_hash',
                  'snapshot_hash', 'selection'):
        if pointer.get(field) != full.get(field):
            raise UniverseSnapshotError(f'universe pointer/full {field} mismatch')

    generated_at = _parse_generated_at(full.get('generated_at'))
    observed_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    age = (observed_now - generated_at).total_seconds()
    if not -30 <= age <= max_age_seconds:
        raise UniverseSnapshotError(
            f'universe snapshot age {age:.1f}s is outside the allowed window')
    return dict(full, snapshot_file=snapshot_path.name)


def _prune_snapshots(snapshot_dir: Path, *, current_name: str, max_files: int) -> int:
    """Bound history while making the file named by the pointer undeletable."""
    try:
        files = [path for path in snapshot_dir.glob('universe_*.json') if path.is_file()]
        files.sort(key=lambda path: (path.stat().st_mtime_ns, path.name))
    except OSError:
        return 0
    removed = 0
    remaining = len(files)
    for path in files:
        if remaining <= max_files:
            break
        if path.name == current_name:
            continue
        try:
            path.unlink()
            removed += 1
            remaining -= 1
        except OSError:
            continue
    return removed


def store(root: Path, rows: list[dict], config: dict, refresh_period: int):
    target = _validate_publication(rows, config, refresh_period)
    now = datetime.now(timezone.utc)
    generated_at = now.isoformat()
    config_hash = _hash(config)
    pairs = [row['pair'] for row in rows]
    selection = _selection_state(len(pairs), target)
    snapshot_hash = _hash(_snapshot_payload(
        generated_at=generated_at, pairs=pairs, refresh_period=refresh_period,
        configuration=config, configuration_hash=config_hash, ranking=rows,
        selection=selection,
    ))
    full = {
        'generated_at': generated_at,
        'pairs': pairs,
        'refresh_period': refresh_period,
        'ranking': rows,
        'configuration': config,
        'configuration_hash': config_hash,
        'snapshot_hash': snapshot_hash,
        'selection': selection,
    }
    root.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime('%Y-%m-%dT%H%M%S.%fZ')
    snapshot_dir = root / 'snapshots'
    snapshot_name = f'universe_{stamp}_{snapshot_hash[:12]}.json'
    # Persist the immutable history object before publishing the current pointer.
    # A crash can therefore never leave current_pairlist.json referencing a
    # snapshot that was not durably written.
    atomic_write_json(snapshot_dir / snapshot_name, full)
    atomic_write_json(root / 'current_pairlist.json', {
        'pairs': pairs,
        'refresh_period': refresh_period,
        'generated_at': generated_at,
        'configuration_hash': config_hash,
        'snapshot_hash': snapshot_hash,
        'snapshot_file': snapshot_name,
        'selection': selection,
    })
    _prune_snapshots(
        snapshot_dir, current_name=snapshot_name,
        max_files=env_int(
            'UNIVERSE_SNAPSHOT_MAX_FILES', 10000, 1, MAX_SNAPSHOT_FILES),
    )
    atomic_write_json(root / 'status.json', {
        'ok': selection['ready'],
        'ready': selection['ready'],
        'state': selection['state'],
        'degraded': selection['degraded'],
        'count': len(pairs),
        'requested_limit': selection['requested_limit'],
        'shortfall_count': selection['shortfall_count'],
        'generated_at': generated_at,
        'configuration_hash': config_hash,
        'snapshot_hash': snapshot_hash,
        'snapshot_file': snapshot_name,
        'top': pairs[:10],
        'selection': selection,
    })
    return full
