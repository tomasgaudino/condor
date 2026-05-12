"""Tests for condor.trading_agent.adaptive.invariants.

Cover:
- Happy path: real `trading_agents/adaptive_pmm/invariants.yaml` loads
  cleanly and the cross-check against the MCP tool metadata yields zero
  drift warnings.
- Each top-level required key + sub-key missing raises InvariantsError.
- Type-map overlaps caught (pct/abs/cat).
- Allowed fields that lack a type emit a warning but don't fail.
- Forbidden ∩ allowed emits a warning (blacklist wins).
- Capital safety bounds validated.
- on_backtest_failure enum validated.
- Helpers: is_field_allowed, is_field_forbidden, field_type.
- Cross-check finds drift when invariants don't match metadata.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from condor.trading_agent.adaptive.invariants import (
    Invariants,
    InvariantsError,
    cross_check_against_field_metadata,
    load_invariants,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


REPO_ROOT = Path(__file__).resolve().parents[3]
REAL_INVARIANTS_PATH = REPO_ROOT / "trading_agents" / "adaptive_pmm" / "invariants.yaml"


def _minimal_valid_inv() -> dict:
    """A minimal but fully valid invariants doc — used to build mutants."""
    return {
        "version": "1.0",
        "allowed_fields": ["take_profit", "buy_spreads", "target_base_pct", "tick_mode"],
        "forbidden_fields": ["manual_kill_switch"],
        "default_max_delta": {
            "percentage_type": {"max_increase": 0.5, "max_decrease": 0.5},
            "absolute_type":   {"max_increase": 0.10, "max_decrease": 0.10},
        },
        "field_type_map": {
            "percentage_type": ["take_profit", "buy_spreads"],
            "absolute_type":   ["target_base_pct"],
            "categorical_type": ["tick_mode"],
        },
        "field_overrides": {},
        "cooldowns": {
            "per_controller_minutes": 30,
            "per_controller_per_field_minutes": 60,
            "global_minutes": 5,
        },
        "capital": {
            "safety_margin": 0.95,
            "block_increases_above_oversub": 0.85,
        },
        "circuit_breaker": {"enabled": False},
        "backtest": {
            "unsupported_connectors": [],
            "on_backtest_failure": "block",
        },
        "auto_mode": {
            "delta_multiplier": 0.5,
            "require_backtest_evidence": True,
            "cooldown_multiplier": 2.0,
        },
    }


def _write(tmp_path: Path, body: dict) -> Path:
    p = tmp_path / "invariants.yaml"
    p.write_text(yaml.safe_dump(body))
    return p


# ---------------------------------------------------------------------------
# Happy path on the real file
# ---------------------------------------------------------------------------


def test_real_invariants_load_without_warnings():
    inv = load_invariants(REAL_INVARIANTS_PATH)
    assert inv.version == "1.0"
    assert "take_profit" in inv.allowed_fields
    assert "manual_kill_switch" in inv.forbidden_fields
    # The current file is clean — no parse-time warnings expected.
    assert inv.warnings == [], inv.warnings


def test_real_invariants_cross_check_clean():
    inv = load_invariants(REAL_INVARIANTS_PATH)
    warns = cross_check_against_field_metadata(inv)
    assert warns == [], warns


# ---------------------------------------------------------------------------
# Minimal valid loads
# ---------------------------------------------------------------------------


def test_minimal_valid_loads(tmp_path):
    inv = load_invariants(_write(tmp_path, _minimal_valid_inv()))
    assert isinstance(inv, Invariants)
    assert inv.warnings == []


# ---------------------------------------------------------------------------
# File-level errors
# ---------------------------------------------------------------------------


def test_missing_file_raises():
    with pytest.raises(InvariantsError, match="not found"):
        load_invariants("/does/not/exist.yaml")


def test_invalid_yaml_raises(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("not: a: valid yaml: :: :")
    with pytest.raises(InvariantsError, match="not valid YAML"):
        load_invariants(p)


def test_top_level_must_be_mapping(tmp_path):
    p = tmp_path / "list.yaml"
    p.write_text("- a\n- b\n")
    with pytest.raises(InvariantsError, match="mapping at the top level"):
        load_invariants(p)


# ---------------------------------------------------------------------------
# Required keys
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("missing_key", [
    "version", "allowed_fields", "forbidden_fields",
    "default_max_delta", "field_type_map", "cooldowns",
    "capital", "circuit_breaker", "backtest", "auto_mode",
])
def test_missing_top_level_key_raises(tmp_path, missing_key):
    body = _minimal_valid_inv()
    del body[missing_key]
    with pytest.raises(InvariantsError, match="missing required top-level keys"):
        load_invariants(_write(tmp_path, body))


def test_missing_cooldown_key_raises(tmp_path):
    body = _minimal_valid_inv()
    del body["cooldowns"]["global_minutes"]
    with pytest.raises(InvariantsError, match="cooldowns missing keys"):
        load_invariants(_write(tmp_path, body))


def test_missing_capital_key_raises(tmp_path):
    body = _minimal_valid_inv()
    del body["capital"]["safety_margin"]
    with pytest.raises(InvariantsError, match="capital missing keys"):
        load_invariants(_write(tmp_path, body))


def test_missing_default_max_delta_key_raises(tmp_path):
    body = _minimal_valid_inv()
    del body["default_max_delta"]["percentage_type"]
    with pytest.raises(InvariantsError, match="default_max_delta missing keys"):
        load_invariants(_write(tmp_path, body))


def test_missing_field_type_map_key_raises(tmp_path):
    body = _minimal_valid_inv()
    del body["field_type_map"]["absolute_type"]
    with pytest.raises(InvariantsError, match="field_type_map missing keys"):
        load_invariants(_write(tmp_path, body))


# ---------------------------------------------------------------------------
# Type-map overlaps
# ---------------------------------------------------------------------------


def test_pct_abs_overlap_raises(tmp_path):
    body = _minimal_valid_inv()
    body["field_type_map"]["absolute_type"].append("take_profit")  # already in pct
    with pytest.raises(InvariantsError, match="appear in both"):
        load_invariants(_write(tmp_path, body))


def test_pct_categorical_overlap_raises(tmp_path):
    body = _minimal_valid_inv()
    body["field_type_map"]["categorical_type"].append("take_profit")
    with pytest.raises(InvariantsError, match="appear in both"):
        load_invariants(_write(tmp_path, body))


# ---------------------------------------------------------------------------
# Warnings (soft issues)
# ---------------------------------------------------------------------------


def test_warning_when_allowed_has_no_type(tmp_path):
    body = _minimal_valid_inv()
    body["allowed_fields"].append("orphan_field")
    inv = load_invariants(_write(tmp_path, body))
    assert any("orphan_field" in w for w in inv.warnings)


def test_warning_when_allowed_and_forbidden_overlap(tmp_path):
    body = _minimal_valid_inv()
    body["allowed_fields"].append("manual_kill_switch")  # already forbidden
    inv = load_invariants(_write(tmp_path, body))
    assert any("appear in both allowed_fields" in w for w in inv.warnings)
    # And `is_field_allowed` must enforce blacklist-wins.
    assert inv.is_field_allowed("manual_kill_switch") is False


# ---------------------------------------------------------------------------
# Capital / backtest validation
# ---------------------------------------------------------------------------


def test_capital_safety_margin_out_of_range(tmp_path):
    body = _minimal_valid_inv()
    body["capital"]["safety_margin"] = 1.5
    with pytest.raises(InvariantsError, match="safety_margin"):
        load_invariants(_write(tmp_path, body))


def test_capital_block_increases_zero_raises(tmp_path):
    body = _minimal_valid_inv()
    body["capital"]["block_increases_above_oversub"] = 0
    with pytest.raises(InvariantsError, match="block_increases_above_oversub"):
        load_invariants(_write(tmp_path, body))


def test_on_backtest_failure_invalid_raises(tmp_path):
    body = _minimal_valid_inv()
    body["backtest"]["on_backtest_failure"] = "panic"
    with pytest.raises(InvariantsError, match="on_backtest_failure must be"):
        load_invariants(_write(tmp_path, body))


def test_negative_cooldown_raises(tmp_path):
    body = _minimal_valid_inv()
    body["cooldowns"]["global_minutes"] = -1
    with pytest.raises(InvariantsError, match="non-negative"):
        load_invariants(_write(tmp_path, body))


# ---------------------------------------------------------------------------
# Field overrides
# ---------------------------------------------------------------------------


def test_override_missing_type_raises(tmp_path):
    body = _minimal_valid_inv()
    body["field_overrides"] = {"take_profit": {"max_increase": 2.0}}  # missing `type`
    with pytest.raises(InvariantsError, match="missing `type`"):
        load_invariants(_write(tmp_path, body))


def test_override_must_be_mapping(tmp_path):
    body = _minimal_valid_inv()
    body["field_overrides"] = {"take_profit": "not a dict"}
    with pytest.raises(InvariantsError, match="must be a mapping"):
        load_invariants(_write(tmp_path, body))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_is_field_allowed(tmp_path):
    inv = load_invariants(_write(tmp_path, _minimal_valid_inv()))
    assert inv.is_field_allowed("take_profit") is True
    assert inv.is_field_allowed("manual_kill_switch") is False
    assert inv.is_field_allowed("not_listed") is False


def test_is_field_forbidden(tmp_path):
    inv = load_invariants(_write(tmp_path, _minimal_valid_inv()))
    assert inv.is_field_forbidden("manual_kill_switch") is True
    assert inv.is_field_forbidden("take_profit") is False


def test_field_type_lookup(tmp_path):
    inv = load_invariants(_write(tmp_path, _minimal_valid_inv()))
    assert inv.field_type("take_profit") == "percentage_type"
    assert inv.field_type("target_base_pct") == "absolute_type"
    assert inv.field_type("tick_mode") == "categorical_type"
    assert inv.field_type("not_typed") is None


# ---------------------------------------------------------------------------
# Cross-check vs MCP tool metadata
# ---------------------------------------------------------------------------


def test_cross_check_real_file_no_drift():
    inv = load_invariants(REAL_INVARIANTS_PATH)
    warns = cross_check_against_field_metadata(inv)
    assert warns == [], f"drift detected — keep invariants and metadata in sync: {warns}"


def test_cross_check_detects_silent_write_risk(tmp_path):
    body = _minimal_valid_inv()
    body["allowed_fields"].append("nonexistent_field")
    body["field_type_map"]["percentage_type"].append("nonexistent_field")
    inv = load_invariants(_write(tmp_path, body))
    warns = cross_check_against_field_metadata(inv)
    # The new field is NOT in the MCP tool's whitelist → silent-write risk warning.
    assert any("silent-write risk" in w for w in warns), warns


def test_cross_check_detects_forbidden_drift(tmp_path):
    body = _minimal_valid_inv()
    body["forbidden_fields"].append("buy_spreads")  # not forbidden by the MCP tool
    inv = load_invariants(_write(tmp_path, body))
    warns = cross_check_against_field_metadata(inv)
    assert any("forbidden in invariants but not in MCP metadata" in w for w in warns), warns
