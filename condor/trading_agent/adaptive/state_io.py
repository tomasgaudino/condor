"""State I/O helpers for the adaptive framework.

Two file formats are supported:

- **JSONL** (append-only line-delimited JSON): for time series and audit logs.
  Functions: `append_jsonl`, `read_jsonl_all`, `read_jsonl_window`,
  `read_jsonl_last_n`, `truncate_jsonl_older_than`.

- **JSON map** (single dict written atomically): for cooldowns and small
  mutable state. Functions: `read_json_map`, `upsert_json_map`.

All functions are pure: take a path and arguments, return values, no side
effects beyond the file system. Designed to be used from routines and the
agent engine without requiring any framework state.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Timestamp parsing — used by window-based JSONL reads
# ---------------------------------------------------------------------------


def parse_ts(value: str) -> datetime:
    """Parse an ISO 8601 timestamp into a timezone-aware datetime (UTC).

    Accepts both ``2026-05-09T14:32:18Z`` and ``2026-05-09T14:32:18+00:00``.
    Naive timestamps are assumed UTC.
    """
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def utc_now() -> datetime:
    """Return current UTC time, timezone-aware."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# JSONL — append-only line-delimited JSON
# ---------------------------------------------------------------------------


def append_jsonl(path: Path, entry: dict[str, Any]) -> None:
    """Append a single entry as a JSON line.

    Creates the parent directory if needed. Uses a single ``write`` call so a
    crash mid-write produces at worst a partial last line (which readers
    tolerate via the JSON-decode try/except).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, separators=(",", ":")) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)


def _iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """Yield entries one by one. Skips malformed lines silently (logged at debug)."""
    if not Path(path).exists():
        return
    with open(path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as e:
                logger.debug("Skipping malformed JSONL line %d in %s: %s", line_no, path, e)


def read_jsonl_all(path: Path) -> list[dict[str, Any]]:
    """Read every entry. Returns empty list if file does not exist."""
    return list(_iter_jsonl(path))


def read_jsonl_window(
    path: Path,
    since: datetime,
    until: datetime | None = None,
    ts_key: str = "ts",
) -> list[dict[str, Any]]:
    """Read entries whose ``ts_key`` falls in ``[since, until]``.

    Entries without a parseable timestamp are skipped.
    """
    out: list[dict[str, Any]] = []
    for entry in _iter_jsonl(path):
        raw_ts = entry.get(ts_key)
        if not raw_ts:
            continue
        try:
            ts = parse_ts(str(raw_ts))
        except ValueError:
            continue
        if ts < since:
            continue
        if until is not None and ts > until:
            continue
        out.append(entry)
    return out


def read_jsonl_last_n(path: Path, n: int) -> list[dict[str, Any]]:
    """Return the last ``n`` entries.

    Naive implementation: reads everything and slices. Acceptable while files
    stay small (<10k entries — see retention policies in
    `MEMORY_SPEC.md`). If files grow, switch to a tail-seek implementation.
    """
    if n <= 0:
        return []
    all_entries = read_jsonl_all(path)
    return all_entries[-n:]


def read_jsonl_filter(
    path: Path,
    predicate: Callable[[dict[str, Any]], bool],
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Return entries matching ``predicate``, optionally limited."""
    out: list[dict[str, Any]] = []
    for entry in _iter_jsonl(path):
        if predicate(entry):
            out.append(entry)
            if limit is not None and len(out) >= limit:
                break
    return out


def truncate_jsonl_older_than(
    path: Path,
    cutoff: datetime,
    ts_key: str = "ts",
) -> int:
    """Drop entries with ``ts < cutoff``. Atomic rewrite. Returns count removed."""
    path = Path(path)
    if not path.exists():
        return 0

    keep: list[dict[str, Any]] = []
    removed = 0
    for entry in _iter_jsonl(path):
        raw_ts = entry.get(ts_key)
        if raw_ts:
            try:
                if parse_ts(str(raw_ts)) < cutoff:
                    removed += 1
                    continue
            except ValueError:
                pass  # malformed ts → keep entry to be safe
        keep.append(entry)

    if removed == 0:
        return 0

    _atomic_write_jsonl(path, keep)
    return removed


def _atomic_write_jsonl(path: Path, entries: list[dict[str, Any]]) -> None:
    """Write all entries atomically: tempfile in same dir, then rename."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry, separators=(",", ":")) + "\n")
        os.replace(tmp_path, path)
    except Exception:
        # Best-effort cleanup of the temp file on error
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def rewrite_jsonl(path: Path, entries: list[dict[str, Any]]) -> None:
    """Public alias of `_atomic_write_jsonl` for full-file rewrites.

    Used by callers that need to mutate an entry in place (e.g. updating the
    outcome of a previously appended audit entry).
    """
    _atomic_write_jsonl(path, entries)


# ---------------------------------------------------------------------------
# JSON map — single mutable dict written atomically
# ---------------------------------------------------------------------------


def read_json_map(path: Path) -> dict[str, Any]:
    """Read the JSON map. Returns empty dict if missing or malformed."""
    path = Path(path)
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            logger.warning("JSON map at %s is not a dict, returning empty", path)
            return {}
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read JSON map at %s: %s", path, e)
        return {}


def write_json_map(path: Path, data: dict[str, Any]) -> None:
    """Write a JSON map atomically (tempfile + rename)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def upsert_json_map(path: Path, key: str, value: Any) -> None:
    """Set ``data[key] = value`` and write atomically.

    Read-modify-write under the assumption that there is a single writer
    (the agent process). For multi-writer scenarios a lock file would be
    required.
    """
    data = read_json_map(path)
    data[key] = value
    write_json_map(path, data)


# ---------------------------------------------------------------------------
# Identity sanitization for filenames
# ---------------------------------------------------------------------------


def canonical_id_to_filename(canonical_id: str) -> str:
    """Sanitize a canonical controller_id for use as a filename stem.

    The framework uses ``{bot_name}::{config_name}`` as canonical id (see
    FIXES_2026-05-10.md F1). Filesystems handle ``::`` poorly, so we replace
    it with ``__``.
    """
    return canonical_id.replace("::", "__")
