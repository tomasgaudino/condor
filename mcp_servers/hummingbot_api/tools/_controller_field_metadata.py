"""Static metadata per controller for the `update_controller_config` MCP tool.

Single source of truth for:
- `updatable_fields`: which fields the agent is allowed to write (mirrors
  `is_updatable=True` from the controller's Pydantic schema).
- `forbidden_fields`: our own policy blacklist (D7, D11, AGENT_SCHEMA_SPEC).
  Some are technically `is_updatable` (e.g. `leverage`) but kept off-limits
  for the agent because the human is the one who decides those.
- `caveats_by_field`: per-field warnings that the tool returns alongside
  successful writes — what the human/LLM needs to know about the side
  effects of changing this field.

Whenever a new controller is added to the adaptive framework, extend
`_METADATA_BY_CONTROLLER` with its own block (and add tests).

See `.planning/strategy-framework/MCP_TOOL_SPEC.md` §4.2.
"""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# pmm_mister
# ---------------------------------------------------------------------------

# Mirrored from Hummingbot's pmm_mister Pydantic schema (is_updatable=True).
# Add NEW fields here ONLY if you've confirmed they actually hot-reload —
# silent writes are the most insidious bug class for this tool.
_PMM_MISTER_UPDATABLE: frozenset[str] = frozenset({
    # Spreads y amounts
    "buy_spreads", "sell_spreads", "buy_amounts_pct", "sell_amounts_pct",
    # Take profit
    "take_profit", "take_profit_order_type", "open_order_type",
    # Inventory targets
    "target_base_pct", "min_base_pct", "max_base_pct", "min_skew",
    # Capital
    "portfolio_allocation", "total_amount_quote", "max_active_executors_by_level",
    # Timing
    "executor_refresh_time", "buy_cooldown_time", "sell_cooldown_time",
    "buy_position_effectivization_time", "sell_position_effectivization_time",
    # Tolerances
    "price_distance_tolerance", "refresh_tolerance", "tolerance_scaling",
    # Mode and protection
    "tick_mode", "position_profit_protection",
    # Globals (technically updatable, used by ControllerBase)
    "leverage",
})


# Policy blacklist. NEVER let the agent touch these, even if Hummingbot
# would technically accept the write. Some are immutable structural
# (connector_name, trading_pair); others are humano-only by policy
# (manual_kill_switch, leverage).
_ABSOLUTE_FORBIDDEN: frozenset[str] = frozenset({
    "manual_kill_switch",
    "leverage",
    "connector_name",
    "trading_pair",
    "position_mode",
    "controller_name",
    "controller_type",
    "id",
    "_config_name",
    "global_take_profit",
    "global_stop_loss",
})


_PMM_MISTER_CAVEATS: dict[str, list[str]] = {
    "take_profit": [
        "Only affects executors created after the hot-reload (~10s). "
        "Active executors keep their original TP until they close.",
    ],
    "take_profit_order_type": [
        "Only affects executors created after the hot-reload.",
    ],
    "open_order_type": [
        "Only affects executors created after the hot-reload.",
    ],
    "tick_mode": [
        "Changing tick_mode affects how spreads are interpreted. "
        "Verify existing spreads still make sense in the new mode.",
    ],
    "position_profit_protection": [
        "Binary switch. Enabling with adverse inventory can temporarily "
        "block one side until conditions normalize.",
    ],
    "max_active_executors_by_level": [
        "Reducing this does not cancel existing executors; new ones will "
        "respect the new cap. Increasing it may trigger immediate fan-out.",
    ],
    "portfolio_allocation": [
        "Direct lever on capital exposure. Verify wallet headroom before "
        "increasing — see capital_state routine.",
    ],
    "total_amount_quote": [
        "Direct lever on capital exposure. Verify wallet headroom before "
        "increasing — see capital_state routine.",
    ],
}


_GLOBAL_CAVEATS: list[str] = [
    "Hot-reload typically takes up to 10s to propagate to the running bot.",
]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


_METADATA_BY_CONTROLLER: dict[str, dict[str, Any]] = {
    "pmm_mister": {
        "updatable_fields": _PMM_MISTER_UPDATABLE,
        "caveats": _PMM_MISTER_CAVEATS,
    },
    # Future controllers extend here.
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_metadata_for(controller_name: str) -> dict[str, Any]:
    """Return metadata for a controller.

    Raises ``NotImplementedError`` for controllers we haven't authorized
    explicitly. The agent should never be able to touch a controller
    whose constraints we haven't reviewed.
    """
    if controller_name not in _METADATA_BY_CONTROLLER:
        raise NotImplementedError(
            f"Controller '{controller_name}' is not authorized for "
            f"`update_controller_config`. Supported: "
            f"{sorted(_METADATA_BY_CONTROLLER.keys())}"
        )
    meta = _METADATA_BY_CONTROLLER[controller_name]
    return {
        "controller_name": controller_name,
        "updatable_fields": meta["updatable_fields"],
        "forbidden_fields": _ABSOLUTE_FORBIDDEN,
        "caveats_by_field": meta["caveats"],
        "global_caveats": list(_GLOBAL_CAVEATS),
    }


def is_field_updatable(controller_name: str, field: str) -> bool:
    """Quick check: is this field on the whitelist for this controller?"""
    try:
        meta = get_metadata_for(controller_name)
    except NotImplementedError:
        return False
    return field in meta["updatable_fields"]


def is_field_forbidden(field: str) -> bool:
    """Is this field on the absolute blacklist (any controller)?"""
    return field in _ABSOLUTE_FORBIDDEN


def caveats_for(controller_name: str, field: str) -> list[str]:
    """Return per-field caveats plus global caveats. Empty list if none."""
    try:
        meta = get_metadata_for(controller_name)
    except NotImplementedError:
        return []
    return list(meta["caveats_by_field"].get(field, [])) + list(meta["global_caveats"])


def supported_controllers() -> list[str]:
    return sorted(_METADATA_BY_CONTROLLER.keys())
