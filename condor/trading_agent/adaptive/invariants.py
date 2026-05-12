"""Loader + sanity-check for `invariants.yaml`.

The file is the single source of truth for the agent's hard limits
(allowed/forbidden fields, deltas, cooldowns, capital safety, modes —
see `.planning/strategy-framework/AGENT_SCHEMA_SPEC.md`). At
agent-loop startup (Phase 5.6) this module loads the file, validates
its internal consistency, and cross-checks it against the MCP tool's
field metadata so a drift between the two is caught immediately
rather than at the first proposal.

Errors are typed: `InvariantsError` is raised when the file is
unreadable, malformed, or internally inconsistent. **Warnings** are
returned as a list (not raised) — they let the agent start with
caveats logged but functional.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


class InvariantsError(Exception):
    """Raised when invariants.yaml cannot be loaded or is inconsistent."""


@dataclass
class Invariants:
    """Parsed + validated invariants for one agent."""

    version: str
    allowed_fields: frozenset[str]
    forbidden_fields: frozenset[str]
    default_max_delta: dict[str, dict[str, float]]
    field_type_map: dict[str, frozenset[str]]
    field_overrides: dict[str, dict[str, Any]]
    cooldowns: dict[str, float]
    capital: dict[str, float]
    circuit_breaker: dict[str, Any]
    backtest: dict[str, Any]
    auto_mode: dict[str, Any]

    # Warnings emitted by the cross-check (e.g. drift vs. metadata module).
    # Empty when everything lines up.
    warnings: list[str] = field(default_factory=list)

    # ─── Convenience helpers ────────────────────────────────────────

    def is_field_allowed(self, field_name: str) -> bool:
        """A field is allowed iff in `allowed_fields` AND NOT in `forbidden_fields`.

        The blacklist always wins — even when a field is technically in
        both lists, this returns False. Matches the MCP tool semantics.
        """
        if field_name in self.forbidden_fields:
            return False
        return field_name in self.allowed_fields

    def is_field_forbidden(self, field_name: str) -> bool:
        return field_name in self.forbidden_fields

    def field_type(self, field_name: str) -> str | None:
        """Return ``"percentage_type"`` or ``"absolute_type"``, or None
        if the field has a per-field override (caller looks up
        `field_overrides[field_name]` directly)."""
        for kind, fields in self.field_type_map.items():
            if field_name in fields:
                return kind
        return None


# ---------------------------------------------------------------------------
# Required-keys schema (kept declarative on purpose — easy to audit)
# ---------------------------------------------------------------------------


_REQUIRED_TOP_KEYS = {
    "version", "allowed_fields", "forbidden_fields",
    "default_max_delta", "field_type_map", "cooldowns",
    "capital", "circuit_breaker", "backtest", "auto_mode",
}

_REQUIRED_DEFAULT_MAX_DELTA = {"percentage_type", "absolute_type"}

_REQUIRED_FIELD_TYPE_MAP = {"percentage_type", "absolute_type"}

_REQUIRED_COOLDOWN_KEYS = {
    "per_controller_minutes",
    "per_controller_per_field_minutes",
    "global_minutes",
}

_REQUIRED_CAPITAL_KEYS = {"safety_margin", "block_increases_above_oversub"}

_REQUIRED_AUTO_MODE_KEYS = {
    "delta_multiplier",
    "require_backtest_evidence",
    "cooldown_multiplier",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_invariants(path: Path | str) -> Invariants:
    """Load and validate an ``invariants.yaml``.

    Raises ``InvariantsError`` for fatal issues. Returns an
    ``Invariants`` dataclass with ``warnings`` populated for soft
    issues (e.g. drift from the MCP tool's metadata module — they
    don't prevent the agent from running but should be visible).
    """
    p = Path(path)
    if not p.exists():
        raise InvariantsError(f"invariants.yaml not found at {p}")

    try:
        raw = yaml.safe_load(p.read_text())
    except yaml.YAMLError as e:
        raise InvariantsError(f"invariants.yaml is not valid YAML: {e}") from e

    if not isinstance(raw, dict):
        raise InvariantsError(
            f"invariants.yaml must be a mapping at the top level, got "
            f"{type(raw).__name__}"
        )

    return _parse_and_validate(raw, source=str(p))


def _parse_and_validate(raw: dict, *, source: str) -> Invariants:
    # ── Top-level keys
    missing = _REQUIRED_TOP_KEYS - raw.keys()
    if missing:
        raise InvariantsError(
            f"{source}: missing required top-level keys: {sorted(missing)}"
        )

    # ── allowed/forbidden fields
    allowed = _require_list_of_str(raw, "allowed_fields", source)
    forbidden = _require_list_of_str(raw, "forbidden_fields", source)

    # No allowed-only field can be in the absolute blacklist — that's the
    # invariant of the file itself. (Forbidden-wins is enforced at lookup
    # time by `is_field_allowed`, but having a field in both lists is a
    # smell; flag it.)
    overlap = set(allowed) & set(forbidden)
    warnings: list[str] = []
    if overlap:
        warnings.append(
            f"{source}: {len(overlap)} field(s) appear in both allowed_fields "
            f"and forbidden_fields: {sorted(overlap)}. Blacklist wins — "
            f"consider removing them from allowed_fields."
        )

    # ── default_max_delta
    dmd = raw["default_max_delta"]
    if not isinstance(dmd, dict):
        raise InvariantsError(f"{source}: default_max_delta must be a mapping")
    missing_dmd = _REQUIRED_DEFAULT_MAX_DELTA - dmd.keys()
    if missing_dmd:
        raise InvariantsError(
            f"{source}: default_max_delta missing keys: {sorted(missing_dmd)}"
        )
    for kind in _REQUIRED_DEFAULT_MAX_DELTA:
        sub = dmd[kind]
        if not isinstance(sub, dict) or not {"max_increase", "max_decrease"} <= sub.keys():
            raise InvariantsError(
                f"{source}: default_max_delta.{kind} must have max_increase + max_decrease"
            )

    # ── field_type_map
    ftm = raw["field_type_map"]
    if not isinstance(ftm, dict):
        raise InvariantsError(f"{source}: field_type_map must be a mapping")
    missing_ftm = _REQUIRED_FIELD_TYPE_MAP - ftm.keys()
    if missing_ftm:
        raise InvariantsError(
            f"{source}: field_type_map missing keys: {sorted(missing_ftm)}"
        )
    typed_buckets: dict[str, set[str]] = {
        "percentage_type": set(_require_list_of_str(ftm, "percentage_type", source)),
        "absolute_type":   set(_require_list_of_str(ftm, "absolute_type", source)),
    }
    if "categorical_type" in ftm:
        typed_buckets["categorical_type"] = set(
            _require_list_of_str(ftm, "categorical_type", source)
        )
    # No field may appear in more than one bucket.
    bucket_names = sorted(typed_buckets.keys())
    for i, name_a in enumerate(bucket_names):
        for name_b in bucket_names[i + 1:]:
            overlap_ab = typed_buckets[name_a] & typed_buckets[name_b]
            if overlap_ab:
                raise InvariantsError(
                    f"{source}: field(s) {sorted(overlap_ab)} appear in both "
                    f"field_type_map.{name_a} and .{name_b}"
                )
    typed_pct = typed_buckets["percentage_type"]
    typed_abs = typed_buckets["absolute_type"]
    typed_cat = typed_buckets.get("categorical_type", set())

    # Every allowed field must have a type (either via field_type_map or
    # via a per-field override). Otherwise the expander can't know how
    # to interpret deltas.
    overrides = raw.get("field_overrides") or {}
    if not isinstance(overrides, dict):
        raise InvariantsError(f"{source}: field_overrides must be a mapping")

    typed_via_map = typed_pct | typed_abs | typed_cat
    overridden = set(overrides.keys())
    untyped = set(allowed) - typed_via_map - overridden
    if untyped:
        warnings.append(
            f"{source}: allowed_fields without a type assignment "
            f"(no field_type_map and no override): {sorted(untyped)}. "
            f"The deterministic expander won't know how to scale deltas."
        )

    # Sanity-check the overrides shape.
    for fname, ov in overrides.items():
        if not isinstance(ov, dict):
            raise InvariantsError(
                f"{source}: field_overrides[{fname!r}] must be a mapping"
            )
        if "type" not in ov:
            raise InvariantsError(
                f"{source}: field_overrides[{fname!r}] missing `type`"
            )

    # ── cooldowns
    cd = raw["cooldowns"]
    if not isinstance(cd, dict):
        raise InvariantsError(f"{source}: cooldowns must be a mapping")
    missing_cd = _REQUIRED_COOLDOWN_KEYS - cd.keys()
    if missing_cd:
        raise InvariantsError(f"{source}: cooldowns missing keys: {sorted(missing_cd)}")
    for k, v in cd.items():
        if not isinstance(v, (int, float)) or v < 0:
            raise InvariantsError(f"{source}: cooldowns.{k} must be a non-negative number")

    # ── capital
    cap = raw["capital"]
    if not isinstance(cap, dict):
        raise InvariantsError(f"{source}: capital must be a mapping")
    missing_cap = _REQUIRED_CAPITAL_KEYS - cap.keys()
    if missing_cap:
        raise InvariantsError(f"{source}: capital missing keys: {sorted(missing_cap)}")
    sm = float(cap["safety_margin"])
    bi = float(cap["block_increases_above_oversub"])
    if not (0 < sm <= 1):
        raise InvariantsError(f"{source}: capital.safety_margin must be in (0, 1], got {sm}")
    if not (0 < bi <= 1):
        raise InvariantsError(
            f"{source}: capital.block_increases_above_oversub must be in (0, 1], got {bi}"
        )

    # ── auto_mode
    am = raw["auto_mode"]
    if not isinstance(am, dict):
        raise InvariantsError(f"{source}: auto_mode must be a mapping")
    missing_am = _REQUIRED_AUTO_MODE_KEYS - am.keys()
    if missing_am:
        raise InvariantsError(f"{source}: auto_mode missing keys: {sorted(missing_am)}")

    # ── backtest
    bt = raw["backtest"]
    if not isinstance(bt, dict):
        raise InvariantsError(f"{source}: backtest must be a mapping")
    if "on_backtest_failure" in bt and bt["on_backtest_failure"] not in ("block", "allow_with_flag"):
        raise InvariantsError(
            f"{source}: backtest.on_backtest_failure must be 'block' or "
            f"'allow_with_flag', got {bt['on_backtest_failure']!r}"
        )

    return Invariants(
        version=str(raw["version"]),
        allowed_fields=frozenset(allowed),
        forbidden_fields=frozenset(forbidden),
        default_max_delta=dmd,
        field_type_map={
            "percentage_type": frozenset(typed_pct),
            "absolute_type": frozenset(typed_abs),
            "categorical_type": frozenset(typed_cat),
        },
        field_overrides=dict(overrides),
        cooldowns={k: float(v) for k, v in cd.items()},
        capital={"safety_margin": sm, "block_increases_above_oversub": bi},
        circuit_breaker=dict(raw["circuit_breaker"]),
        backtest=dict(bt),
        auto_mode=dict(am),
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Cross-check against the MCP tool's field metadata (FIXES F5)
# ---------------------------------------------------------------------------


def cross_check_against_field_metadata(
    invariants: Invariants, controller_name: str = "pmm_mister"
) -> list[str]:
    """Verify invariants.yaml is consistent with the MCP tool's metadata.

    Returns a list of warning strings (empty if everything lines up).
    Drift here means the agent can propose changes that the tool will
    reject (or vice-versa) — annoying for the user, so we want it
    surfaced at startup.

    Special case: ``leverage`` is in the metadata's `updatable_fields`
    (Hummingbot does allow writing it) AND in `forbidden_fields` of
    both sides. That's not drift — it's the safety net the spec
    documents. We accept the asymmetry as long as forbidden matches.
    """
    try:
        from mcp_servers.hummingbot_api.tools._controller_field_metadata import (
            get_metadata_for,
        )
    except ImportError as e:
        return [f"could not import MCP tool metadata module: {e}"]

    try:
        meta = get_metadata_for(controller_name)
    except NotImplementedError:
        return [
            f"controller {controller_name!r} not registered in MCP tool metadata"
        ]

    meta_updatable = set(meta["updatable_fields"])
    meta_forbidden = set(meta["forbidden_fields"])

    warnings: list[str] = []

    # Anything the invariants allow MUST be `is_updatable` in the
    # metadata — otherwise the agent can propose a field that the tool
    # will silently no-op on.
    allow_minus_meta = set(invariants.allowed_fields) - meta_updatable
    if allow_minus_meta:
        warnings.append(
            f"invariants.allowed_fields not in MCP metadata.updatable_fields "
            f"(silent-write risk): {sorted(allow_minus_meta)}"
        )

    # Anything the metadata says is updatable BUT the invariants don't
    # allow — soft warning. Either the human chose not to expose it
    # (e.g. leverage) or it's a drift the human should fix. Skip the
    # warning when the field is forbidden on both sides (intentional).
    meta_minus_allow = meta_updatable - set(invariants.allowed_fields)
    intentional_skip = meta_minus_allow & meta_forbidden & invariants.forbidden_fields
    drift_extra = meta_minus_allow - intentional_skip
    if drift_extra:
        warnings.append(
            f"MCP metadata.updatable_fields not in invariants.allowed_fields "
            f"(human chose to not expose? or drift): {sorted(drift_extra)}"
        )

    # Forbidden lists should be identical (modulo ordering). If a field
    # is forbidden on one side and not the other, the agent could
    # propose something the tool will refuse, or vice-versa.
    only_in_inv = invariants.forbidden_fields - meta_forbidden
    only_in_meta = meta_forbidden - invariants.forbidden_fields
    if only_in_inv:
        warnings.append(
            f"forbidden in invariants but not in MCP metadata: {sorted(only_in_inv)}"
        )
    if only_in_meta:
        warnings.append(
            f"forbidden in MCP metadata but not in invariants: {sorted(only_in_meta)}"
        )

    return warnings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_list_of_str(d: dict, key: str, source: str) -> list[str]:
    v = d.get(key)
    if not isinstance(v, list):
        raise InvariantsError(f"{source}: {key} must be a list")
    out = []
    for x in v:
        if not isinstance(x, str):
            raise InvariantsError(
                f"{source}: {key} entries must be strings, got {x!r}"
            )
        out.append(x)
    return out
