"""Audit log + last_changes — persistence for adaptive agent decisions.

Two files live under an agent's state dir, with strictly different semantics:

- ``audit_log.jsonl`` — append-only history of proposals. Every proposal
  the agent generates (applied or not) leaves one line here. Each line
  follows the canonical schema documented in
  ``.planning/strategy-framework/MEMORY_SPEC.md`` §F6. Fields are added
  incrementally to the SAME line as the proposal's state changes
  (pending → approved → applied → outcome_30min) — via atomic rewrite.

- ``last_changes.json`` — a small JSON map keyed by ``controller_id``,
  each value mapping field → ``{ts, old, new, patch_id, marker,
  cooldown_until}``. Used for cooldown lookups in the trigger loop —
  cheap to read, cheap to upsert. Reject/snooze also write here
  (without changing the value) so the cooldown applies the same way.

All operations are atomic against process crashes:
- JSONL append uses ``state_io.append_jsonl`` (writes to a tempfile then
  renames — single fsync barrier).
- JSON map upserts use ``state_io.upsert_json_map``.
- update-in-place of an existing audit entry uses ``state_io.rewrite_jsonl``
  (read all, mutate the matching one, atomic rewrite).

NOT thread-safe across processes. Today there's a single supervisor
agent per process, and a single Telegram handler dispatching callbacks
sequentially — fine for MVP. If two agents ever share a state dir,
add a file lock.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from condor.trading_agent.adaptive import state_io

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def audit_log_path(agent_dir: Path) -> Path:
    return agent_dir / "state" / "audit_log.jsonl"


def last_changes_path(agent_dir: Path) -> Path:
    return agent_dir / "state" / "last_changes.json"


# ---------------------------------------------------------------------------
# Patch IDs and tick IDs
# ---------------------------------------------------------------------------


def make_patch_id(now: datetime, counter: int) -> str:
    """Stable, sortable patch id.

    Format: ``p_<utc-iso-no-colons>_<3-digit-counter>``. The ``counter``
    is meant to disambiguate proposals generated in the same second
    inside one tick (multi-controller — see PROPOSE_MODE_SPEC §
    Multi-controller).
    """
    iso = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"p_{iso}_{counter:03d}"


def parse_patch_id_ts(patch_id: str) -> datetime | None:
    """Recover the timestamp embedded in a patch_id. None on bad input."""
    try:
        iso = patch_id.split("_", 2)[1]
        return datetime.strptime(iso, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except (IndexError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Audit log — append + update-in-place
# ---------------------------------------------------------------------------


def append_audit_entry(agent_dir: Path, entry: dict[str, Any]) -> None:
    """Append a brand-new audit entry. Caller built the dict.

    The entry MUST include ``patch_id``. We don't enforce schema
    completeness here — the agent loop populates fields incrementally
    (e.g. ``human_verdict=pending`` at creation, ``approved``/``applied``
    later via :func:`update_audit_entry`).
    """
    if "patch_id" not in entry:
        raise ValueError("audit entry must include `patch_id`")
    state_io.append_jsonl(audit_log_path(agent_dir), entry)


def find_audit_entry(agent_dir: Path, patch_id: str) -> dict[str, Any] | None:
    """Return the entry with the given patch_id, or None if absent."""
    path = audit_log_path(agent_dir)
    if not path.exists():
        return None
    for entry in state_io.read_jsonl_all(path):
        if entry.get("patch_id") == patch_id:
            return entry
    return None


def update_audit_entry(
    agent_dir: Path, patch_id: str, updates: dict[str, Any]
) -> bool:
    """Update an existing entry in-place. Returns True if patched, False
    if not found. Atomic rewrite of the whole file (acceptable while
    the file stays under ~10k entries — see MEMORY_SPEC pendientes).

    Caller controls which keys to update; we just shallow-merge.
    """
    path = audit_log_path(agent_dir)
    if not path.exists():
        logger.warning("update_audit_entry: %s does not exist", path)
        return False

    entries = state_io.read_jsonl_all(path)
    found = False
    for e in entries:
        if e.get("patch_id") == patch_id:
            e.update(updates)
            found = True
            break
    if not found:
        logger.warning("update_audit_entry: patch_id %s not found", patch_id)
        return False

    state_io.rewrite_jsonl(path, entries)
    return True


# ---------------------------------------------------------------------------
# last_changes — upsert + cooldown queries
# ---------------------------------------------------------------------------


def upsert_last_change(
    agent_dir: Path,
    *,
    controller_id: str,
    field: str,
    old_value: Any,
    new_value: Any,
    patch_id: str,
    ts: datetime | None = None,
    marker: str | None = None,
    cooldown_until: datetime | None = None,
) -> None:
    """Record an apply/reject/snooze in ``last_changes.json``.

    Reject and snooze pass ``old_value == new_value`` and a marker
    string (e.g. ``"rejected"`` / ``"snoozed"``) so the cooldown
    machinery treats them uniformly with applies.

    The map structure is::

        {
          "<controller_id>": {
            "<field>": {
              "ts": "...",
              "old": ...,
              "new": ...,
              "patch_id": "...",
              "marker": "applied" | "rejected" | "snoozed",
              "cooldown_until": "..."   # optional, snooze-only
            },
            ...
          },
          ...
        }
    """
    ts = ts or state_io.utc_now()
    path = last_changes_path(agent_dir)
    data = state_io.read_json_map(path)
    per_controller = data.setdefault(controller_id, {})
    per_controller[field] = {
        "ts": state_io.utc_iso(ts),
        "old": old_value,
        "new": new_value,
        "patch_id": patch_id,
        "marker": marker or "applied",
        **({"cooldown_until": state_io.utc_iso(cooldown_until)}
           if cooldown_until is not None else {}),
    }
    state_io.write_json_map(path, data)


def get_last_change(
    agent_dir: Path, controller_id: str, field: str
) -> dict[str, Any] | None:
    """Look up the last change for (controller, field). None if absent."""
    data = state_io.read_json_map(last_changes_path(agent_dir))
    return (data.get(controller_id) or {}).get(field)


def compute_cooldown_end(
    agent_dir: Path,
    controller_id: str,
    field: str,
    *,
    per_controller_per_field_minutes: float,
    per_controller_minutes: float,
    global_minutes: float,
    now: datetime | None = None,
) -> datetime | None:
    """Compute the cooldown deadline for (controller, field).

    Returns the **latest** of:
    1. The explicit ``cooldown_until`` from a snooze, if present.
    2. The most recent change to the same (controller, field) +
       ``per_controller_per_field_minutes``.
    3. The most recent change to ANY field of the same controller +
       ``per_controller_minutes``.
    4. The most recent change to ANY controller + ``global_minutes``.

    Returns ``None`` if no relevant history exists (no cooldown active).
    """
    now = now or state_io.utc_now()
    data = state_io.read_json_map(last_changes_path(agent_dir))

    deadlines: list[datetime] = []

    def _parse(ts: str) -> datetime | None:
        try:
            return state_io.parse_ts(ts)
        except Exception:
            return None

    # (1) explicit cooldown_until from snooze
    self_change = (data.get(controller_id) or {}).get(field) or {}
    cu = self_change.get("cooldown_until")
    if isinstance(cu, str):
        parsed = _parse(cu)
        if parsed:
            deadlines.append(parsed)

    # (2) per-controller per-field
    if "ts" in self_change:
        parsed = _parse(self_change["ts"])
        if parsed:
            deadlines.append(parsed + timedelta(minutes=per_controller_per_field_minutes))

    # (3) per-controller (any field)
    for f, info in (data.get(controller_id) or {}).items():
        parsed = _parse(info.get("ts", ""))
        if parsed:
            deadlines.append(parsed + timedelta(minutes=per_controller_minutes))

    # (4) global (any controller, any field)
    for cid, fields in data.items():
        for f, info in fields.items():
            parsed = _parse(info.get("ts", ""))
            if parsed:
                deadlines.append(parsed + timedelta(minutes=global_minutes))

    future = [d for d in deadlines if d > now]
    if not future:
        return None
    return max(future)


def is_on_cooldown(
    agent_dir: Path,
    controller_id: str,
    field: str,
    *,
    per_controller_per_field_minutes: float,
    per_controller_minutes: float,
    global_minutes: float,
    now: datetime | None = None,
) -> bool:
    """Convenience wrapper — True iff :func:`compute_cooldown_end` is
    in the future."""
    end = compute_cooldown_end(
        agent_dir, controller_id, field,
        per_controller_per_field_minutes=per_controller_per_field_minutes,
        per_controller_minutes=per_controller_minutes,
        global_minutes=global_minutes,
        now=now,
    )
    return end is not None


# ---------------------------------------------------------------------------
# Expire pending proposals
# ---------------------------------------------------------------------------


def expire_old_pending_proposals(
    agent_dir: Path,
    *,
    timeout_hours: float = 24.0,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Mark `pending` entries older than `timeout_hours` as `expired`.

    Returns the list of entries that were just expired (so the caller
    can edit the Telegram messages to reflect the state). Idempotent —
    running it twice on the same data is a no-op.

    Uses ``ts`` (proposal creation time) for the age check, NOT
    ``verdict_at`` (which would be null for pending entries).
    """
    now = now or state_io.utc_now()
    cutoff = now - timedelta(hours=timeout_hours)
    path = audit_log_path(agent_dir)
    if not path.exists():
        return []

    entries = state_io.read_jsonl_all(path)
    expired: list[dict[str, Any]] = []
    changed = False

    for e in entries:
        if e.get("human_verdict") != "pending":
            continue
        ts_str = e.get("ts")
        if not ts_str:
            continue
        try:
            ts = state_io.parse_ts(ts_str)
        except Exception:
            continue
        if ts > cutoff:
            continue
        e["human_verdict"] = "expired"
        e["verdict_at"] = state_io.utc_iso(now)
        e["verdict_by"] = "auto_timeout"
        expired.append(dict(e))
        changed = True

    if changed:
        state_io.rewrite_jsonl(path, entries)
    return expired
