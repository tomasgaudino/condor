"""Adaptive Strategy Framework — MCP tools used by the adaptive agent.

Houses tooling that is specific to the LLM-driven supervisor described
in `.planning/strategy-framework/`. Today there is only one tool:
``update_controller_config`` — a strict wrapper around Hummingbot's
existing per-bot controller update endpoint.

Why a wrapper and not the existing ``update_bot_controller_config``:

- We need a **whitelist** of `is_updatable` fields. Hummingbot itself
  will silently no-op writes to non-updatable fields (the field gets
  written to the YAML on disk but the running strategy never picks it
  up). Without an explicit check, the agent thinks its change applied
  while in reality nothing happened.
- We need an **absolute blacklist**: `manual_kill_switch`, `leverage`,
  `connector_name`, etc. are humano-only by policy even when technically
  writable (D7).
- We need **type coercion** that respects whatever shape the field is
  in *today*: brigado has `buy_spreads` as a float and
  `buy_amounts_pct` as an int, contrary to what one would assume from
  the controller's source code. Coerce against `type(old_value)`.
- We need **optimistic-lock** so the agent can detect external changes
  between proposal and apply.
- We return **caveats** automatically (the human / LLM does not have
  to remember that `take_profit` only affects new executors).

Atomicity comes for free: the Hummingbot endpoint does a shallow
merge on the YAML config, so passing ``{field: value}`` writes only
that field. Validated against brigado on 2026-05-12: re-writing the
same value back returned OK and the other 33 fields stayed bit-identical.

Errors of *domain* (field forbidden, type mismatch, race detected,
…) are returned as ``{"success": False, "error_code": ...}`` and
NEVER raised — the agent must be able to reason about the failure.
Bugs (e.g. an unexpected TypeError inside the tool itself) do raise
— that's the caller's problem.

Spec: `.planning/strategy-framework/MCP_TOOL_SPEC.md`.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from mcp_servers.hummingbot_api.tools import _controller_field_metadata as meta_mod

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Hot-reload window — Hummingbot's `strategy_v2_base.config_update_interval`
# defaults to 10s. We report this as the ETA on every successful write.
HOT_RELOAD_ETA_SECONDS = 10

# Float comparison tolerance for the optimistic lock. Anything tighter
# than this and we false-flag races on round-trip serialization.
_FLOAT_LOCK_TOL = 1e-9


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _error(code: str, message: str, **details: Any) -> dict[str, Any]:
    """Build a uniform error response. `details` becomes the `details` field."""
    return {
        "success": False,
        "error_code": code,
        "message": message,
        "details": dict(details),
    }


def _utc_iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _values_equal_with_tolerance(actual: Any, expected: Any) -> bool:
    """Equality that allows for float round-trip drift.

    Lists are compared element-wise. Anything non-numeric falls back to
    plain ``==``.
    """
    if isinstance(actual, float) or isinstance(expected, float):
        try:
            return abs(float(actual) - float(expected)) < _FLOAT_LOCK_TOL
        except (TypeError, ValueError):
            return False
    if isinstance(actual, list) and isinstance(expected, list):
        if len(actual) != len(expected):
            return False
        return all(
            _values_equal_with_tolerance(a, b) for a, b in zip(actual, expected)
        )
    return actual == expected


def _coerce_value(value: Any, old_value: Any, field: str) -> Any:
    """Coerce ``value`` to match the type of ``old_value``.

    Brigado surprises:
    - ``buy_spreads`` may arrive as a scalar float or as a list[float] —
      depends on how the controller was instantiated. We respect whatever
      shape it currently has.
    - ``buy_amounts_pct`` may arrive as int even though one might expect
      float. We follow ``type(old_value)`` strictly to avoid widening
      surprises.

    Raises ``ValueError`` if coercion is impossible. The caller wraps
    this into a domain error response.
    """
    expected_type = type(old_value) if old_value is not None else None

    # Strict bool — refuse truthy ints / strings
    if expected_type is bool:
        if not isinstance(value, bool):
            raise ValueError(
                f"Field {field!r} expects strict bool, got {type(value).__name__}"
            )
        return value

    if expected_type is int:
        if isinstance(value, bool):
            raise ValueError(f"Field {field!r} expects int, got bool")
        try:
            coerced = int(value)
        except (TypeError, ValueError) as e:
            raise ValueError(
                f"Cannot coerce {value!r} to int for field {field!r}: {e}"
            ) from e
        return coerced

    if expected_type is float:
        try:
            return float(value)
        except (TypeError, ValueError) as e:
            raise ValueError(
                f"Cannot coerce {value!r} to float for field {field!r}: {e}"
            ) from e

    if expected_type is str:
        return str(value)

    if expected_type is list:
        # Accept list or CSV string. Each element gets coerced against the
        # first element of old_value (if any), else float by default.
        if isinstance(value, str):
            items = [s.strip() for s in value.split(",") if s.strip()]
        elif isinstance(value, list):
            items = list(value)
        else:
            raise ValueError(
                f"Field {field!r} expects list or CSV string, got "
                f"{type(value).__name__}"
            )
        element_type = type(old_value[0]) if old_value else float
        try:
            return [element_type(x) for x in items]
        except (TypeError, ValueError) as e:
            raise ValueError(
                f"Cannot coerce list elements to "
                f"{element_type.__name__} for field {field!r}: {e}"
            ) from e

    # Fallback: old_value is None (field never set) — pass through.
    if expected_type is None:
        return value

    # Anything else: try constructor.
    try:
        return expected_type(value)
    except Exception as e:
        raise ValueError(
            f"Cannot coerce {value!r} to {expected_type.__name__} "
            f"for field {field!r}: {e}"
        ) from e


def _find_config(configs: list[dict], controller_id: str) -> dict | None:
    """Locate a controller config by its canonical id.

    The Hummingbot API surfaces the same logical entity under different
    keys depending on context — see LEARNINGS.md L1. We try
    ``_config_name`` first (the filename of the YAML, which is the
    most reliable), then ``id``.
    """
    if not isinstance(configs, list):
        return None
    for c in configs:
        if not isinstance(c, dict):
            continue
        if c.get("_config_name") == controller_id or c.get("id") == controller_id:
            return c
    return None


# ---------------------------------------------------------------------------
# Public tool
# ---------------------------------------------------------------------------


async def update_controller_config(
    client: Any,
    bot_name: str,
    controller_id: str,
    field: str,
    value: Any,
    expected_old_value: Any | None = None,
) -> dict[str, Any]:
    """Update a single ``is_updatable`` field of a running controller.

    Args:
        client: Hummingbot API client.
        bot_name: Name of the running bot.
        controller_id: Controller's ``_config_name`` (or ``id``).
        field: The field to write.
        value: The new value. Will be coerced to ``type(old_value)``.
        expected_old_value: If provided, the write only proceeds when
            the current value equals this (with float tolerance).
            ``None`` (default) disables the optimistic lock.

    Returns:
        Either::

            {"success": True, "bot_name": ..., "controller_id": ...,
             "field": ..., "old_value": ..., "new_value": ...,
             "applied_at": ..., "hot_reload_eta_seconds": 10,
             "caveats": [...], "raw_api_result": ...}

        or::

            {"success": False, "error_code": ..., "message": ...,
             "details": {...}}
    """

    # ─────────────────────────────────────────────────────────────────
    # 1. Fetch controller configs from the bot. This also tells us
    #    whether the bot is even up.
    # ─────────────────────────────────────────────────────────────────
    try:
        configs = await client.controllers.get_bot_controller_configs(bot_name)
    except Exception as e:
        return _error(
            "bot_not_found",
            f"Could not read configs for bot {bot_name!r}: {e}",
            bot_name=bot_name,
        )

    if not isinstance(configs, list):
        return _error(
            "bot_not_found",
            f"Bot {bot_name!r} did not return a configs list (got "
            f"{type(configs).__name__})",
            bot_name=bot_name,
        )

    cfg = _find_config(configs, controller_id)
    if cfg is None:
        return _error(
            "controller_not_found",
            f"Controller {controller_id!r} not in bot {bot_name!r}",
            bot_name=bot_name,
            controller_id=controller_id,
            available=[c.get("_config_name") for c in configs if isinstance(c, dict)],
        )

    controller_name = cfg.get("controller_name", "")
    controller_type = cfg.get("controller_type", "")
    old_value = cfg.get(field)

    # ─────────────────────────────────────────────────────────────────
    # 2. Blacklist (cross-controller) — absolute veto.
    # ─────────────────────────────────────────────────────────────────
    if meta_mod.is_field_forbidden(field):
        return _error(
            "field_forbidden",
            f"Field {field!r} is on the absolute blacklist (humano-only).",
            field=field,
            controller_name=controller_name,
        )

    # ─────────────────────────────────────────────────────────────────
    # 3. Authorize the controller + whitelist the field.
    # ─────────────────────────────────────────────────────────────────
    try:
        metadata = meta_mod.get_metadata_for(controller_name)
    except NotImplementedError as e:
        return _error(
            "controller_not_supported",
            str(e),
            controller_name=controller_name,
        )

    if field not in metadata["updatable_fields"]:
        return _error(
            "field_not_updatable",
            f"Field {field!r} is not `is_updatable` for controller "
            f"{controller_name!r}. Writing it would silently no-op "
            f"until the next bot restart.",
            field=field,
            controller_name=controller_name,
            updatable_fields=sorted(metadata["updatable_fields"]),
        )

    # ─────────────────────────────────────────────────────────────────
    # 4. Type coercion.
    # ─────────────────────────────────────────────────────────────────
    try:
        coerced = _coerce_value(value, old_value, field)
    except ValueError as e:
        return _error(
            "type_coercion_failed",
            str(e),
            field=field, value=value,
            expected_type=type(old_value).__name__ if old_value is not None else None,
        )

    # ─────────────────────────────────────────────────────────────────
    # 5. Optimistic lock.
    # ─────────────────────────────────────────────────────────────────
    if expected_old_value is not None:
        if not _values_equal_with_tolerance(old_value, expected_old_value):
            return _error(
                "value_changed_externally",
                f"Expected old value {expected_old_value!r} but found "
                f"{old_value!r}",
                field=field,
                current_value=old_value,
                expected=expected_old_value,
            )

    # ─────────────────────────────────────────────────────────────────
    # 6. Pre-validate (best-effort). The endpoint validates server-side
    #    as part of the write, so this is a fast-fail. If the validate
    #    endpoint itself errors, we log and proceed — better to surface
    #    the real api_error than to invent one.
    # ─────────────────────────────────────────────────────────────────
    merged = {**cfg, field: coerced}
    try:
        validation = await client.controllers.validate_controller_config(
            controller_type, controller_name, merged
        )
        if isinstance(validation, dict) and validation.get("valid") is False:
            return _error(
                "validation_failed",
                validation.get("message", "Pydantic validation failed"),
                field=field, value=coerced,
                validation_result=validation,
            )
    except Exception as e:
        logger.warning(
            "update_controller_config: pre-validation failed for %s/%s "
            "(field=%s): %s. Proceeding to apply.",
            bot_name, controller_id, field, e,
        )

    # ─────────────────────────────────────────────────────────────────
    # 7. Apply — single-field payload, atomic via shallow merge.
    # ─────────────────────────────────────────────────────────────────
    try:
        api_result = await client.controllers.update_bot_controller_config(
            bot_name, controller_id, {field: coerced}
        )
    except Exception as e:
        return _error(
            "api_error",
            f"Hummingbot API write failed: {e}",
            field=field,
            value=coerced,
        )

    # ─────────────────────────────────────────────────────────────────
    # 8. Build success response with caveats.
    # ─────────────────────────────────────────────────────────────────
    caveats = meta_mod.caveats_for(controller_name, field)
    return {
        "success": True,
        "bot_name": bot_name,
        "controller_id": controller_id,
        "controller_name": controller_name,
        "field": field,
        "old_value": old_value,
        "new_value": coerced,
        "applied_at": _utc_iso_now(),
        "hot_reload_eta_seconds": HOT_RELOAD_ETA_SECONDS,
        "caveats": caveats,
        "raw_api_result": api_result,
    }
