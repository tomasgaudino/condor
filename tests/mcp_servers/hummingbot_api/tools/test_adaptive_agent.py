"""Tests for the `update_controller_config` MCP tool.

Coverage matrix (all paths through the algorithm):
1. bot_not_found (API throws / returns non-list).
2. controller_not_found (id missing from configs).
3. field_forbidden (absolute blacklist).
4. controller_not_supported (unknown controller_name).
5. field_not_updatable (not in whitelist).
6. type_coercion_failed (bool/int/float/list).
7. value_changed_externally (optimistic-lock race).
8. validation_failed (server says invalid).
9. validation endpoint errors but write proceeds (graceful degrade).
10. api_error on write.
11. success path: response shape, caveats present, atomic single-field payload.
12. _coerce_value pure-function variants.
13. _values_equal_with_tolerance corner cases.
"""

from __future__ import annotations

import asyncio
import math

import pytest

from mcp_servers.hummingbot_api.tools import adaptive_agent as tool


# ---------------------------------------------------------------------------
# Fake client
# ---------------------------------------------------------------------------


class _FakeControllers:
    """In-memory stand-in for `client.controllers.*` used by the tool."""

    def __init__(self, configs: list[dict] | Exception):
        self._configs = configs
        self.validate_called_with: tuple | None = None
        self.update_called_with: tuple | None = None
        self.validate_response: dict = {"valid": True}
        self.validate_error: Exception | None = None
        self.update_error: Exception | None = None
        self.update_response: dict = {"message": "ok"}

    async def get_bot_controller_configs(self, bot_name: str):
        if isinstance(self._configs, Exception):
            raise self._configs
        return self._configs

    async def validate_controller_config(self, controller_type, controller_name, config):
        self.validate_called_with = (controller_type, controller_name, dict(config))
        if self.validate_error:
            raise self.validate_error
        return self.validate_response

    async def update_bot_controller_config(self, bot_name, controller_id, config):
        self.update_called_with = (bot_name, controller_id, dict(config))
        if self.update_error:
            raise self.update_error
        return self.update_response


class _FakeClient:
    def __init__(self, controllers):
        self.controllers = controllers


def _pmm_cfg(**overrides) -> dict:
    """Realistic pmm_mister config shape (matches brigado snapshot)."""
    base = {
        "_config_name": "btcusdt-1-5",
        "id": "btcusdt-1-5",
        "controller_type": "generic",
        "controller_name": "pmm_mister",
        "connector_name": "binance",
        "trading_pair": "BTC-USDT",
        "take_profit": 0.0001,
        "buy_spreads": 5e-05,
        "sell_spreads": 5e-05,
        "buy_amounts_pct": 1,
        "sell_amounts_pct": 1,
        "target_base_pct": 0.5,
        "max_active_executors_by_level": 10,
        "portfolio_allocation": 0.03,
        "total_amount_quote": 1000.0,
        "position_profit_protection": False,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Coercion (pure function)
# ---------------------------------------------------------------------------


def test_coerce_int_to_int():
    assert tool._coerce_value(5, old_value=10, field="x") == 5


def test_coerce_float_string_to_float():
    assert tool._coerce_value("0.00045", old_value=0.0001, field="tp") == 0.00045


def test_coerce_bool_rejects_truthy():
    with pytest.raises(ValueError):
        tool._coerce_value(1, old_value=False, field="flag")
    with pytest.raises(ValueError):
        tool._coerce_value("true", old_value=False, field="flag")


def test_coerce_int_rejects_bool():
    with pytest.raises(ValueError):
        tool._coerce_value(True, old_value=10, field="n")


def test_coerce_list_from_csv():
    out = tool._coerce_value("0.001, 0.002, 0.003", old_value=[0.0], field="spreads")
    assert out == [0.001, 0.002, 0.003]


def test_coerce_list_from_list():
    out = tool._coerce_value([1, 2, 3], old_value=[0.0], field="spreads")
    assert out == [1.0, 2.0, 3.0]


def test_coerce_passthrough_when_old_value_none():
    assert tool._coerce_value({"a": 1}, old_value=None, field="x") == {"a": 1}


def test_coerce_invalid_float_string():
    with pytest.raises(ValueError):
        tool._coerce_value("not_a_number", old_value=0.0, field="tp")


# ---------------------------------------------------------------------------
# Optimistic-lock helper
# ---------------------------------------------------------------------------


def test_values_equal_float_within_tolerance():
    assert tool._values_equal_with_tolerance(0.0001, 0.00010000000001) is True


def test_values_equal_float_outside_tolerance():
    assert tool._values_equal_with_tolerance(0.0001, 0.0002) is False


def test_values_equal_lists_elementwise():
    assert tool._values_equal_with_tolerance([0.001, 0.002], [0.001, 0.002]) is True
    assert tool._values_equal_with_tolerance([0.001, 0.002], [0.001]) is False


def test_values_equal_strict_for_non_numeric():
    assert tool._values_equal_with_tolerance("a", "a") is True
    assert tool._values_equal_with_tolerance(True, False) is False


# ---------------------------------------------------------------------------
# Find config
# ---------------------------------------------------------------------------


def test_find_config_by_config_name():
    out = tool._find_config([{"_config_name": "x"}, {"_config_name": "y"}], "y")
    assert out == {"_config_name": "y"}


def test_find_config_by_id_fallback():
    out = tool._find_config([{"id": "x"}, {"id": "y"}], "y")
    assert out == {"id": "y"}


def test_find_config_none_when_missing():
    assert tool._find_config([{"_config_name": "x"}], "y") is None


def test_find_config_non_list_returns_none():
    assert tool._find_config("not a list", "y") is None


# ---------------------------------------------------------------------------
# Tool flow — error paths
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


def test_bot_not_found_when_api_throws():
    ctrl = _FakeControllers(configs=RuntimeError("bot not running"))
    res = _run(tool.update_controller_config(
        _FakeClient(ctrl), "missing_bot", "x", "take_profit", 0.0002,
    ))
    assert res["success"] is False
    assert res["error_code"] == "bot_not_found"


def test_bot_not_found_when_response_not_list():
    ctrl = _FakeControllers(configs={"unexpected": "dict"})
    res = _run(tool.update_controller_config(
        _FakeClient(ctrl), "bot", "x", "take_profit", 0.0002,
    ))
    assert res["error_code"] == "bot_not_found"


def test_controller_not_found():
    ctrl = _FakeControllers(configs=[_pmm_cfg()])
    res = _run(tool.update_controller_config(
        _FakeClient(ctrl), "bot", "different_id", "take_profit", 0.0002,
    ))
    assert res["error_code"] == "controller_not_found"
    assert "available" in res["details"]


def test_field_forbidden():
    ctrl = _FakeControllers(configs=[_pmm_cfg()])
    res = _run(tool.update_controller_config(
        _FakeClient(ctrl), "bot", "btcusdt-1-5", "manual_kill_switch", True,
    ))
    assert res["error_code"] == "field_forbidden"


def test_field_forbidden_takes_precedence_over_whitelist():
    """`leverage` is in pmm_mister whitelist BUT also in blacklist —
    blacklist wins (D7)."""
    ctrl = _FakeControllers(configs=[_pmm_cfg(leverage=1)])
    res = _run(tool.update_controller_config(
        _FakeClient(ctrl), "bot", "btcusdt-1-5", "leverage", 5,
    ))
    assert res["error_code"] == "field_forbidden"


def test_controller_not_supported_unknown_controller_name():
    cfg = _pmm_cfg(controller_name="some_random_controller")
    ctrl = _FakeControllers(configs=[cfg])
    res = _run(tool.update_controller_config(
        _FakeClient(ctrl), "bot", "btcusdt-1-5", "take_profit", 0.0002,
    ))
    assert res["error_code"] == "controller_not_supported"


def test_field_not_updatable():
    ctrl = _FakeControllers(configs=[_pmm_cfg()])
    res = _run(tool.update_controller_config(
        _FakeClient(ctrl), "bot", "btcusdt-1-5", "some_random_field", 1,
    ))
    assert res["error_code"] == "field_not_updatable"
    assert "updatable_fields" in res["details"]


def test_type_coercion_failed_bool():
    cfg = _pmm_cfg(position_profit_protection=False)
    ctrl = _FakeControllers(configs=[cfg])
    res = _run(tool.update_controller_config(
        _FakeClient(ctrl), "bot", "btcusdt-1-5",
        "position_profit_protection", "yes",  # not strict bool
    ))
    assert res["error_code"] == "type_coercion_failed"


def test_value_changed_externally():
    cfg = _pmm_cfg(take_profit=0.0001)
    ctrl = _FakeControllers(configs=[cfg])
    res = _run(tool.update_controller_config(
        _FakeClient(ctrl), "bot", "btcusdt-1-5",
        "take_profit", 0.0005,
        expected_old_value=0.0002,  # mismatch
    ))
    assert res["error_code"] == "value_changed_externally"
    assert res["details"]["current_value"] == 0.0001


def test_validation_failed():
    ctrl = _FakeControllers(configs=[_pmm_cfg()])
    ctrl.validate_response = {"valid": False, "message": "TP out of range"}
    res = _run(tool.update_controller_config(
        _FakeClient(ctrl), "bot", "btcusdt-1-5", "take_profit", 99.0,
    ))
    assert res["error_code"] == "validation_failed"


def test_validation_endpoint_error_proceeds_to_apply():
    """If the validation endpoint itself errors (e.g. method missing in
    server build), the tool logs and proceeds — and the real api_error
    surfaces if the write fails downstream."""
    ctrl = _FakeControllers(configs=[_pmm_cfg()])
    ctrl.validate_error = RuntimeError("validate endpoint not available")
    res = _run(tool.update_controller_config(
        _FakeClient(ctrl), "bot", "btcusdt-1-5", "take_profit", 0.0002,
    ))
    # Should NOT be validation_failed — the write proceeded.
    assert res["success"] is True
    assert res["new_value"] == 0.0002


def test_api_error_on_write():
    ctrl = _FakeControllers(configs=[_pmm_cfg()])
    ctrl.update_error = RuntimeError("connection refused")
    res = _run(tool.update_controller_config(
        _FakeClient(ctrl), "bot", "btcusdt-1-5", "take_profit", 0.0002,
    ))
    assert res["error_code"] == "api_error"


# ---------------------------------------------------------------------------
# Tool flow — success path
# ---------------------------------------------------------------------------


def test_success_atomic_payload_single_field_only():
    """The API write must receive ONLY `{field: value}` — that's what
    enables the shallow-merge atomicity confirmed against brigado."""
    ctrl = _FakeControllers(configs=[_pmm_cfg()])
    res = _run(tool.update_controller_config(
        _FakeClient(ctrl), "bot", "btcusdt-1-5", "take_profit", 0.0005,
    ))
    assert res["success"] is True
    assert ctrl.update_called_with[2] == {"take_profit": 0.0005}


def test_success_response_shape():
    ctrl = _FakeControllers(configs=[_pmm_cfg()])
    res = _run(tool.update_controller_config(
        _FakeClient(ctrl), "bot", "btcusdt-1-5", "take_profit", 0.0005,
    ))
    for key in (
        "success", "bot_name", "controller_id", "controller_name",
        "field", "old_value", "new_value",
        "applied_at", "hot_reload_eta_seconds", "caveats",
        "raw_api_result",
    ):
        assert key in res, f"missing key {key!r} in success response"
    assert res["old_value"] == 0.0001
    assert res["new_value"] == 0.0005
    assert res["hot_reload_eta_seconds"] == tool.HOT_RELOAD_ETA_SECONDS


def test_success_caveats_include_field_specific():
    ctrl = _FakeControllers(configs=[_pmm_cfg()])
    res = _run(tool.update_controller_config(
        _FakeClient(ctrl), "bot", "btcusdt-1-5", "take_profit", 0.0005,
    ))
    assert any("executors created" in c.lower() for c in res["caveats"])


def test_success_optimistic_lock_matches():
    ctrl = _FakeControllers(configs=[_pmm_cfg(take_profit=0.0001)])
    res = _run(tool.update_controller_config(
        _FakeClient(ctrl), "bot", "btcusdt-1-5",
        "take_profit", 0.0005,
        expected_old_value=0.0001,
    ))
    assert res["success"] is True


def test_success_coerces_string_to_float():
    ctrl = _FakeControllers(configs=[_pmm_cfg()])
    res = _run(tool.update_controller_config(
        _FakeClient(ctrl), "bot", "btcusdt-1-5", "take_profit", "0.00045",
    ))
    assert res["success"] is True
    # Coerced
    assert res["new_value"] == 0.00045
    assert ctrl.update_called_with[2] == {"take_profit": 0.00045}


def test_success_int_field_coerces_int():
    """max_active_executors_by_level is an int — coercion must give int, not float."""
    ctrl = _FakeControllers(configs=[_pmm_cfg()])
    res = _run(tool.update_controller_config(
        _FakeClient(ctrl), "bot", "btcusdt-1-5",
        "max_active_executors_by_level", 5,
    ))
    assert res["success"] is True
    assert isinstance(res["new_value"], int)
    assert res["new_value"] == 5


def test_success_validates_with_merged_config():
    """The validate call must use the full merged config (not just {field: value})."""
    ctrl = _FakeControllers(configs=[_pmm_cfg()])
    res = _run(tool.update_controller_config(
        _FakeClient(ctrl), "bot", "btcusdt-1-5", "take_profit", 0.0005,
    ))
    assert res["success"] is True
    sent_cfg = ctrl.validate_called_with[2]
    # merged config should keep all the original fields
    assert sent_cfg["trading_pair"] == "BTC-USDT"
    assert sent_cfg["connector_name"] == "binance"
    # ...with the new field value
    assert sent_cfg["take_profit"] == 0.0005


def test_success_resolves_controller_by_id_fallback():
    """If the controller dict only has `id` (no `_config_name`), find it anyway."""
    cfg = _pmm_cfg()
    del cfg["_config_name"]  # leave only `id`
    ctrl = _FakeControllers(configs=[cfg])
    res = _run(tool.update_controller_config(
        _FakeClient(ctrl), "bot", "btcusdt-1-5", "take_profit", 0.0005,
    ))
    assert res["success"] is True
