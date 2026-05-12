"""Tests for _controller_field_metadata.

Coverage:
- supported_controllers / get_metadata_for surface contract.
- pmm_mister whitelist includes the fields the spec calls out.
- Absolute blacklist enforced for the right keys.
- is_field_updatable / is_field_forbidden / caveats_for surfaces.
- Unknown controllers raise NotImplementedError.
- Caveats include the per-field warning AND the global one.
"""

from __future__ import annotations

import pytest

from mcp_servers.hummingbot_api.tools import _controller_field_metadata as meta


def test_supported_controllers_lists_pmm_mister():
    assert meta.supported_controllers() == ["pmm_mister"]


def test_get_metadata_for_unknown_controller_raises():
    with pytest.raises(NotImplementedError):
        meta.get_metadata_for("does_not_exist")


def test_pmm_mister_whitelist_includes_core_levers():
    m = meta.get_metadata_for("pmm_mister")
    wl = m["updatable_fields"]
    for f in (
        "take_profit", "buy_spreads", "sell_spreads",
        "buy_amounts_pct", "sell_amounts_pct",
        "target_base_pct", "portfolio_allocation",
        "max_active_executors_by_level",
    ):
        assert f in wl, f"core lever {f!r} missing from whitelist"


def test_blacklist_blocks_dangerous_fields():
    for f in (
        "manual_kill_switch", "leverage", "connector_name",
        "trading_pair", "position_mode", "controller_name",
        "controller_type", "id", "_config_name",
        "global_take_profit", "global_stop_loss",
    ):
        assert meta.is_field_forbidden(f), f"{f!r} should be forbidden"


def test_blacklist_does_not_block_normal_fields():
    assert meta.is_field_forbidden("take_profit") is False
    assert meta.is_field_forbidden("buy_spreads") is False


def test_is_field_updatable_pmm_mister_true_for_whitelisted():
    assert meta.is_field_updatable("pmm_mister", "take_profit") is True


def test_is_field_updatable_pmm_mister_false_for_unknown_field():
    assert meta.is_field_updatable("pmm_mister", "this_field_doesnt_exist") is False


def test_is_field_updatable_unknown_controller_false_not_raise():
    # Unknown controllers always return False — never crash.
    assert meta.is_field_updatable("xyz", "take_profit") is False


def test_caveats_for_take_profit_includes_specific_and_global():
    cav = meta.caveats_for("pmm_mister", "take_profit")
    assert any("executors created" in c.lower() for c in cav), \
        "expected per-field caveat about new executors"
    assert any("hot-reload" in c.lower() for c in cav), \
        "expected global hot-reload caveat"


def test_caveats_for_field_without_specific_only_returns_global():
    # `buy_spreads` has no per-field caveat → only the global one.
    cav = meta.caveats_for("pmm_mister", "buy_spreads")
    assert len(cav) == 1
    assert "hot-reload" in cav[0].lower()


def test_caveats_for_unknown_controller_empty():
    assert meta.caveats_for("xyz", "take_profit") == []


def test_leverage_is_in_whitelist_BUT_also_forbidden():
    """`leverage` is technically is_updatable but our policy keeps it
    humano-only. The blacklist must take precedence (D7)."""
    m = meta.get_metadata_for("pmm_mister")
    assert "leverage" in m["updatable_fields"]
    assert meta.is_field_forbidden("leverage") is True
