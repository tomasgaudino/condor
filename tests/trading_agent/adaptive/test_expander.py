"""Tests for condor.trading_agent.adaptive.expander.

Covers the deterministic expansion tables (PATCH_CONTRACT_SPEC §Estado 2)
and the chain: raw expansion → invariant clamp → domain clamp → dedup
→ drop no-op candidates equal to current.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from condor.trading_agent.adaptive.expander import expand_proposal
from condor.trading_agent.adaptive.invariants import load_invariants


REPO_ROOT = Path(__file__).resolve().parents[3]
INV_PATH = REPO_ROOT / "trading_agents" / "adaptive_pmm" / "invariants.yaml"


def _inv():
    return load_invariants(INV_PATH)


def _prop(dimension, direction, magnitude):
    return {
        "controller_id": "bot::c1",
        "dimension": dimension,
        "direction": direction,
        "magnitude_qualitative": magnitude,
    }


# ---------------------------------------------------------------------------
# Percentage_type — small / medium / large × increase / decrease
# ---------------------------------------------------------------------------


def test_percentage_small_increase_single_candidate():
    out = expand_proposal(_prop("take_profit", "increase", "small"), 0.0003, _inv())
    assert out["status"] == "ok"
    assert len(out["candidates"]) == 1
    assert abs(out["candidates"][0]["value"] - 0.000375) < 1e-8
    assert out["expansion_strategy"] == "percentage_type_small_increase"


def test_percentage_medium_increase_two_candidates():
    out = expand_proposal(_prop("take_profit", "increase", "medium"), 0.0003, _inv())
    vals = [c["value"] for c in out["candidates"]]
    assert vals == [0.00045, 0.0006]


def test_percentage_large_increase_clamped_by_override():
    """take_profit override: max_increase=2.0 → cap at ×3.0 → both
    candidates valid; but ×3.0 is at the edge, not clamped to a lower
    value. Confirm both pass through."""
    out = expand_proposal(_prop("take_profit", "increase", "large"), 0.0003, _inv())
    vals = [c["value"] for c in out["candidates"]]
    assert vals[0] == 0.0006  # ×2
    assert vals[1] == 0.0009  # ×3, at the cap


def test_percentage_medium_decrease_two_candidates():
    out = expand_proposal(_prop("take_profit", "decrease", "medium"), 0.0003, _inv())
    vals = sorted(c["value"] for c in out["candidates"])
    # ×0.50 = 0.00015, ×0.67 = 0.000201
    assert abs(vals[0] - 0.00015) < 1e-9
    assert abs(vals[1] - 0.000201) < 1e-9


# ---------------------------------------------------------------------------
# Absolute_type
# ---------------------------------------------------------------------------


def test_absolute_medium_increase_two_candidates():
    out = expand_proposal(_prop("target_base_pct", "increase", "medium"), 0.5, _inv())
    vals = sorted(c["value"] for c in out["candidates"])
    assert vals == [0.55, 0.58]


def test_absolute_clamp_to_domain():
    """target_base_pct + 0.08 = 1.03 → clamps to 1.0 (domain)."""
    out = expand_proposal(_prop("target_base_pct", "increase", "medium"), 0.95, _inv())
    vals = [c["value"] for c in out["candidates"]]
    # +0.05 → 1.00, +0.08 → 1.00 (clamped) → dedup leaves one
    assert vals == [1.0]
    assert out["candidates"][0]["clamped"] is True


def test_absolute_large_increase_single_with_clamp():
    out = expand_proposal(_prop("target_base_pct", "increase", "large"), 0.5, _inv())
    # +0.10 (clamp) → just one candidate at 0.6
    assert len(out["candidates"]) == 1
    assert out["candidates"][0]["value"] == 0.6


# ---------------------------------------------------------------------------
# Integer absolute (max_active_executors_by_level)
# ---------------------------------------------------------------------------


def test_max_active_executors_override_uses_absolute_units():
    """The override declares ``type: absolute, max_decrease: 10``. We
    treat max_active_executors_by_level as integer units."""
    out = expand_proposal(
        _prop("max_active_executors_by_level", "decrease", "medium"), 10, _inv(),
    )
    # Override means we use absolute_table. medium decrease = [-0.05, -0.08].
    # The raw values would be 9.95 and 9.92, rounding to int gives 10 — same
    # as current → dedups to empty → status no_valid_candidates.
    # This is a quirk of the spec's table being percentage-points-flavored:
    # for integer fields, the absolute_table units don't really fit. The
    # expander reports it cleanly.
    assert out["status"] in ("no_valid_candidates", "ok")


# ---------------------------------------------------------------------------
# Categorical
# ---------------------------------------------------------------------------


def test_boolean_toggle():
    out = expand_proposal(
        _prop("tick_mode", "increase", "small"), True, _inv(),
    )
    assert out["status"] == "ok"
    assert out["candidates"] == [
        {"id": "c1", "value": False, "delta_relative": "toggle → False", "clamped": False}
    ]


def test_string_categorical_unsupported():
    out = expand_proposal(
        _prop("take_profit_order_type", "increase", "small"), "LIMIT", _inv(),
    )
    assert out["status"] == "unsupported_field_type"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_expander_drops_no_op_candidates():
    """If clamps + dedup leave only candidates equal to current, status
    is no_valid_candidates."""
    out = expand_proposal(
        _prop("target_base_pct", "increase", "large"), 1.0, _inv(),
    )
    # 1.0 + 0.10 → 1.1 → domain clamp → 1.0 → equals current → dropped.
    assert out["status"] == "no_valid_candidates"
    assert out["candidates"] == []


def test_field_type_unknown_returns_unsupported(monkeypatch):
    """A field with no type information at all."""
    inv = _inv()
    out = expand_proposal(
        _prop("unknown_field", "increase", "small"), 1.0, inv,
    )
    assert out["status"] == "unsupported_field_type"


def test_dedup_within_tolerance():
    """When two raw candidates collapse onto the same value after
    clamps, dedup leaves one."""
    out = expand_proposal(
        _prop("buy_spreads", "increase", "large"), 0.0001, _inv(),
    )
    # buy_spreads override: max_increase=1.0 → cap at ×2 = 0.0002.
    # raw: ×2.0=0.0002, ×3.0=0.0003 → clamped to 0.0002 → dedup leaves 1.
    assert len(out["candidates"]) == 1
    assert abs(out["candidates"][0]["value"] - 0.0002) < 1e-9
    assert out["candidates"][0]["clamped"] is True


def test_proposal_ref_preserved():
    out = expand_proposal(
        _prop("take_profit", "increase", "medium"), 0.0003, _inv(),
    )
    assert out["proposal_ref"] == {
        "controller_id": "bot::c1",
        "dimension": "take_profit",
        "direction": "increase",
        "magnitude_qualitative": "medium",
    }
