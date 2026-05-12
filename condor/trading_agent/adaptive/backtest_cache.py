"""Backtest cache for the adaptive agent.

Per ``BACKTEST_LOOP_SPEC.md`` §2.5.5: the same (controller, config,
window) call produces the same backtest result, so we cache by
sha256 of the canonicalized inputs. Hit rates in practice:

- Baseline: ~80–100% (same period subóptimo persists multiple ticks).
- Candidates: ~0–20% (new values per tick).
- Average: ~25–40% reduction in cycle wall time.

Storage is one JSON file per cache entry under
``trading_agents/<agent>/state/backtest_cache/<key>.json``. A tiny
index file (``index.json``) at the same path keeps the ``ts_cached``
of each entry for the daily cleanup pass.

The cache is intentionally per-agent — Phase F1 may move to a shared
cache once the directory structure stabilizes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# Config fields that DON'T affect the backtest result. Excluding them
# from the hash prevents trivial differences (e.g. a regenerated
# ``id``) from busting the cache.
_EXCLUDED_CONFIG_FIELDS: frozenset[str] = frozenset({
    "id", "_config_name", "created_at", "updated_at",
})


# ---------------------------------------------------------------------------
# Key computation (spec §Cache key)
# ---------------------------------------------------------------------------


def canonicalize_config(config: dict) -> str:
    """Deterministic string for hashing. Sorted keys + no whitespace +
    only the fields that affect outcomes."""
    cleaned = {
        k: v for k, v in sorted(config.items())
        if k not in _EXCLUDED_CONFIG_FIELDS
    }
    return json.dumps(cleaned, sort_keys=True, separators=(",", ":"), default=str)


def cache_key(
    *,
    controller_id: str,
    config_snapshot: dict,
    start_ts: datetime,
    end_ts: datetime,
    resolution: str,
) -> str:
    """16-char sha256 prefix. Stable, sortable in printouts."""
    payload = {
        "ctrl": controller_id,
        "cfg": canonicalize_config(config_snapshot),
        "start": int(start_ts.timestamp()),
        "end":   int(end_ts.timestamp()),
        "res":   resolution,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def cache_dir(agent_dir: Path) -> Path:
    return agent_dir / "state" / "backtest_cache"


def _entry_path(agent_dir: Path, key: str) -> Path:
    return cache_dir(agent_dir) / f"{key}.json"


def _index_path(agent_dir: Path) -> Path:
    return cache_dir(agent_dir) / "index.json"


def get(agent_dir: Path, key: str) -> dict | None:
    """Read a cached entry. Returns the stored ``{"result": ..., "meta": ...}``
    dict, or ``None`` on miss / read error.
    """
    path = _entry_path(agent_dir, key)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("backtest_cache: failed to read %s: %s", path, e)
        return None


def put(
    agent_dir: Path,
    key: str,
    result: dict,
    *,
    meta: dict | None = None,
    now: datetime | None = None,
) -> None:
    """Atomically persist a backtest result + tiny index entry.

    ``meta`` typically carries ``{controller_id, window_start,
    window_end}`` so the cleanup pass and any future debugging can
    reconstruct the context without rehashing.
    """
    now = now or datetime.now(timezone.utc)
    cache_dir(agent_dir).mkdir(parents=True, exist_ok=True)
    payload = {
        "result": result,
        "meta": {
            "ts_cached": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            **(meta or {}),
        },
    }
    _atomic_write_json(_entry_path(agent_dir, key), payload)
    _update_index(agent_dir, key, payload["meta"]["ts_cached"])


def _update_index(agent_dir: Path, key: str, ts_cached: str) -> None:
    idx_path = _index_path(agent_dir)
    try:
        index = json.loads(idx_path.read_text()) if idx_path.exists() else {}
    except (OSError, json.JSONDecodeError):
        index = {}
    index[key] = ts_cached
    _atomic_write_json(idx_path, index)


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(data, sort_keys=True, separators=(",", ":"), default=str))
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Daily cleanup
# ---------------------------------------------------------------------------


def purge_older_than(
    agent_dir: Path, cutoff: datetime
) -> int:
    """Remove entries cached before ``cutoff``. Returns the count removed.

    Walks the index to find candidates so we never read all entries
    just to delete them.
    """
    idx_path = _index_path(agent_dir)
    if not idx_path.exists():
        return 0
    try:
        index = json.loads(idx_path.read_text())
    except (OSError, json.JSONDecodeError):
        return 0
    cutoff_iso = cutoff.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    to_drop = [k for k, ts in index.items() if ts < cutoff_iso]
    for k in to_drop:
        try:
            _entry_path(agent_dir, k).unlink(missing_ok=True)
        except OSError as e:
            logger.warning("backtest_cache: failed to delete %s: %s", k, e)
        index.pop(k, None)
    if to_drop:
        _atomic_write_json(idx_path, index)
    return len(to_drop)


def cleanup_marker_path(agent_dir: Path) -> Path:
    return agent_dir / "state" / ".last_cache_cleanup"


def should_run_daily_cleanup(
    agent_dir: Path, *, now: datetime | None = None, max_age_hours: float = 24.0
) -> bool:
    """Cheap check: is the marker file older than ``max_age_hours``?"""
    now = now or datetime.now(timezone.utc)
    marker = cleanup_marker_path(agent_dir)
    if not marker.exists():
        return True
    try:
        mtime = datetime.fromtimestamp(marker.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return True
    return (now - mtime) >= timedelta(hours=max_age_hours)


def stamp_daily_cleanup(agent_dir: Path) -> None:
    """Touch the marker file to record we just ran cleanup."""
    cleanup_marker_path(agent_dir).parent.mkdir(parents=True, exist_ok=True)
    cleanup_marker_path(agent_dir).touch()
