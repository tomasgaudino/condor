"""Tests for condor.trading_agent.adaptive.audit.

Cover:
- patch_id formatting + parse round-trip
- append_audit_entry / find_audit_entry / update_audit_entry (atomic in-place)
- upsert_last_change + get_last_change
- compute_cooldown_end with the three tiers (per-field, per-controller, global)
- snooze cooldown_until takes precedence
- expire_old_pending_proposals only touches pending + old entries
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from condor.trading_agent.adaptive import audit, state_io

UTC = timezone.utc


def _agent_dir(tmp_path: Path) -> Path:
    (tmp_path / "state").mkdir()
    return tmp_path


# ---------------------------------------------------------------------------
# patch_id
# ---------------------------------------------------------------------------


def test_make_patch_id_is_sortable():
    now1 = datetime(2026, 5, 12, 14, 32, 18, tzinfo=UTC)
    now2 = now1 + timedelta(seconds=1)
    p1 = audit.make_patch_id(now1, 1)
    p2 = audit.make_patch_id(now2, 1)
    assert p1 < p2
    assert p1.startswith("p_2026")


def test_make_patch_id_disambiguates_within_second():
    now = datetime(2026, 5, 12, 14, 32, 18, tzinfo=UTC)
    p1 = audit.make_patch_id(now, 1)
    p7 = audit.make_patch_id(now, 7)
    assert p1 != p7
    assert p1 < p7  # 001 < 007 lex


def test_parse_patch_id_ts_round_trip():
    now = datetime(2026, 5, 12, 14, 32, 18, tzinfo=UTC)
    pid = audit.make_patch_id(now, 42)
    parsed = audit.parse_patch_id_ts(pid)
    assert parsed == now


def test_parse_patch_id_ts_invalid_returns_none():
    assert audit.parse_patch_id_ts("not_a_patch_id") is None
    assert audit.parse_patch_id_ts("p_") is None


# ---------------------------------------------------------------------------
# audit log
# ---------------------------------------------------------------------------


def test_append_then_find(tmp_path):
    d = _agent_dir(tmp_path)
    entry = {"patch_id": "p1", "human_verdict": "pending", "ts": "2026-05-12T12:00:00Z"}
    audit.append_audit_entry(d, entry)
    found = audit.find_audit_entry(d, "p1")
    assert found is not None
    assert found["patch_id"] == "p1"
    assert found["human_verdict"] == "pending"


def test_append_requires_patch_id(tmp_path):
    d = _agent_dir(tmp_path)
    with pytest.raises(ValueError, match="patch_id"):
        audit.append_audit_entry(d, {"human_verdict": "pending"})


def test_find_missing_returns_none(tmp_path):
    d = _agent_dir(tmp_path)
    assert audit.find_audit_entry(d, "nope") is None


def test_update_audit_entry_atomic(tmp_path):
    d = _agent_dir(tmp_path)
    audit.append_audit_entry(d, {"patch_id": "p1", "human_verdict": "pending"})
    audit.append_audit_entry(d, {"patch_id": "p2", "human_verdict": "pending"})

    ok = audit.update_audit_entry(d, "p1", {"human_verdict": "approved", "verdict_at": "2026-05-12T12:01:00Z"})
    assert ok is True

    p1 = audit.find_audit_entry(d, "p1")
    p2 = audit.find_audit_entry(d, "p2")
    assert p1["human_verdict"] == "approved"
    assert p1["verdict_at"] == "2026-05-12T12:01:00Z"
    # p2 must NOT have been touched
    assert p2["human_verdict"] == "pending"


def test_update_missing_returns_false(tmp_path):
    d = _agent_dir(tmp_path)
    audit.append_audit_entry(d, {"patch_id": "p1"})
    assert audit.update_audit_entry(d, "nonexistent", {"x": 1}) is False


def test_update_when_file_missing_returns_false(tmp_path):
    d = _agent_dir(tmp_path)
    # state/ exists but no audit_log yet
    assert audit.update_audit_entry(d, "p1", {"x": 1}) is False


# ---------------------------------------------------------------------------
# last_changes
# ---------------------------------------------------------------------------


def test_upsert_then_get(tmp_path):
    d = _agent_dir(tmp_path)
    audit.upsert_last_change(
        d, controller_id="bot::c1", field="take_profit",
        old_value=0.0001, new_value=0.0002, patch_id="p1",
        ts=datetime(2026, 5, 12, 12, 0, tzinfo=UTC),
    )
    got = audit.get_last_change(d, "bot::c1", "take_profit")
    assert got is not None
    assert got["new"] == 0.0002
    assert got["marker"] == "applied"
    assert got["patch_id"] == "p1"


def test_upsert_marker_reject_keeps_value(tmp_path):
    d = _agent_dir(tmp_path)
    audit.upsert_last_change(
        d, controller_id="bot::c1", field="take_profit",
        old_value=0.0001, new_value=0.0001, patch_id="p1",
        marker="rejected",
    )
    got = audit.get_last_change(d, "bot::c1", "take_profit")
    assert got["marker"] == "rejected"
    assert got["old"] == got["new"] == 0.0001


def test_upsert_snooze_records_cooldown_until(tmp_path):
    d = _agent_dir(tmp_path)
    now = datetime(2026, 5, 12, 12, 0, tzinfo=UTC)
    audit.upsert_last_change(
        d, controller_id="bot::c1", field="take_profit",
        old_value=0.0001, new_value=0.0001, patch_id="p1",
        ts=now, marker="snoozed",
        cooldown_until=now + timedelta(hours=1),
    )
    got = audit.get_last_change(d, "bot::c1", "take_profit")
    assert got["marker"] == "snoozed"
    assert got["cooldown_until"] == "2026-05-12T13:00:00Z"


def test_get_missing_last_change(tmp_path):
    d = _agent_dir(tmp_path)
    assert audit.get_last_change(d, "bot::c1", "take_profit") is None


# ---------------------------------------------------------------------------
# Cooldown computation
# ---------------------------------------------------------------------------


def _cooldown_cfg() -> dict:
    return {
        "per_controller_per_field_minutes": 60,
        "per_controller_minutes": 30,
        "global_minutes": 5,
    }


def test_cooldown_none_when_no_history(tmp_path):
    d = _agent_dir(tmp_path)
    end = audit.compute_cooldown_end(d, "bot::c1", "take_profit", **_cooldown_cfg())
    assert end is None


def test_cooldown_per_field_takes_60min(tmp_path):
    d = _agent_dir(tmp_path)
    now = datetime(2026, 5, 12, 12, 0, tzinfo=UTC)
    # apply 30 minutes ago — per-field cooldown is 60 → still active.
    audit.upsert_last_change(
        d, controller_id="bot::c1", field="take_profit",
        old_value=0.0001, new_value=0.0002, patch_id="p1",
        ts=now - timedelta(minutes=30),
    )
    end = audit.compute_cooldown_end(
        d, "bot::c1", "take_profit", now=now, **_cooldown_cfg()
    )
    assert end is not None
    # 30 min ago + 60 min = 30 min from now
    expected = now + timedelta(minutes=30)
    assert end == expected


def test_cooldown_other_field_takes_30min_per_controller(tmp_path):
    d = _agent_dir(tmp_path)
    now = datetime(2026, 5, 12, 12, 0, tzinfo=UTC)
    # apply to a DIFFERENT field 20 minutes ago
    audit.upsert_last_change(
        d, controller_id="bot::c1", field="buy_spreads",
        old_value=0.0001, new_value=0.0002, patch_id="p1",
        ts=now - timedelta(minutes=20),
    )
    # Querying for `take_profit` on the same controller:
    # - no per-field history (skip tier 2)
    # - per-controller tier 3: 20 ago + 30 = 10 from now ✓
    end = audit.compute_cooldown_end(
        d, "bot::c1", "take_profit", now=now, **_cooldown_cfg()
    )
    assert end == now + timedelta(minutes=10)


def test_cooldown_other_controller_takes_5min_global(tmp_path):
    d = _agent_dir(tmp_path)
    now = datetime(2026, 5, 12, 12, 0, tzinfo=UTC)
    audit.upsert_last_change(
        d, controller_id="bot::OTHER", field="take_profit",
        old_value=0.0001, new_value=0.0002, patch_id="p1",
        ts=now - timedelta(minutes=3),
    )
    end = audit.compute_cooldown_end(
        d, "bot::c1", "take_profit", now=now, **_cooldown_cfg()
    )
    # Only the global tier applies: 3 ago + 5 = 2 from now
    assert end == now + timedelta(minutes=2)


def test_cooldown_snooze_extends_beyond_default(tmp_path):
    d = _agent_dir(tmp_path)
    now = datetime(2026, 5, 12, 12, 0, tzinfo=UTC)
    far_future = now + timedelta(hours=24)
    audit.upsert_last_change(
        d, controller_id="bot::c1", field="take_profit",
        old_value=0.0001, new_value=0.0001, patch_id="p1",
        ts=now - timedelta(minutes=1),
        marker="snoozed", cooldown_until=far_future,
    )
    end = audit.compute_cooldown_end(
        d, "bot::c1", "take_profit", now=now, **_cooldown_cfg()
    )
    # The snooze deadline must dominate.
    assert end == far_future


def test_cooldown_takes_max_deadline(tmp_path):
    """If multiple tiers apply, the LATEST one wins."""
    d = _agent_dir(tmp_path)
    now = datetime(2026, 5, 12, 12, 0, tzinfo=UTC)
    # per-field 10 min ago → +60 = 50 from now
    audit.upsert_last_change(
        d, controller_id="bot::c1", field="take_profit",
        old_value=0, new_value=1, patch_id="p1",
        ts=now - timedelta(minutes=10),
    )
    # also a "newer" event 1 min ago on another controller → +5 global = 4 from now
    audit.upsert_last_change(
        d, controller_id="bot::OTHER", field="x",
        old_value=0, new_value=1, patch_id="p2",
        ts=now - timedelta(minutes=1),
    )
    end = audit.compute_cooldown_end(
        d, "bot::c1", "take_profit", now=now, **_cooldown_cfg()
    )
    assert end == now + timedelta(minutes=50)


def test_is_on_cooldown_true_false(tmp_path):
    d = _agent_dir(tmp_path)
    now = datetime(2026, 5, 12, 12, 0, tzinfo=UTC)
    assert audit.is_on_cooldown(d, "bot::c1", "x", now=now, **_cooldown_cfg()) is False
    audit.upsert_last_change(
        d, controller_id="bot::c1", field="x",
        old_value=0, new_value=1, patch_id="p1",
        ts=now - timedelta(minutes=1),
    )
    assert audit.is_on_cooldown(d, "bot::c1", "x", now=now, **_cooldown_cfg()) is True


# ---------------------------------------------------------------------------
# Expire
# ---------------------------------------------------------------------------


def test_expire_marks_old_pending_only(tmp_path):
    d = _agent_dir(tmp_path)
    now = datetime(2026, 5, 12, 12, 0, tzinfo=UTC)
    # 25h ago, pending → should expire
    audit.append_audit_entry(d, {
        "patch_id": "old_pending",
        "ts": state_io.utc_iso(now - timedelta(hours=25)),
        "human_verdict": "pending",
    })
    # 1h ago, pending → still alive
    audit.append_audit_entry(d, {
        "patch_id": "young_pending",
        "ts": state_io.utc_iso(now - timedelta(hours=1)),
        "human_verdict": "pending",
    })
    # 25h ago, approved → leave alone
    audit.append_audit_entry(d, {
        "patch_id": "approved",
        "ts": state_io.utc_iso(now - timedelta(hours=25)),
        "human_verdict": "approved",
    })

    expired = audit.expire_old_pending_proposals(d, now=now)
    assert [e["patch_id"] for e in expired] == ["old_pending"]

    # State on disk
    e_old = audit.find_audit_entry(d, "old_pending")
    e_young = audit.find_audit_entry(d, "young_pending")
    e_approved = audit.find_audit_entry(d, "approved")
    assert e_old["human_verdict"] == "expired"
    assert e_old["verdict_by"] == "auto_timeout"
    assert e_young["human_verdict"] == "pending"
    assert e_approved["human_verdict"] == "approved"


def test_expire_idempotent(tmp_path):
    d = _agent_dir(tmp_path)
    now = datetime(2026, 5, 12, 12, 0, tzinfo=UTC)
    audit.append_audit_entry(d, {
        "patch_id": "p1",
        "ts": state_io.utc_iso(now - timedelta(hours=25)),
        "human_verdict": "pending",
    })
    assert len(audit.expire_old_pending_proposals(d, now=now)) == 1
    # Already expired — second call returns empty
    assert audit.expire_old_pending_proposals(d, now=now) == []


def test_expire_no_file_returns_empty(tmp_path):
    d = _agent_dir(tmp_path)
    assert audit.expire_old_pending_proposals(d) == []
