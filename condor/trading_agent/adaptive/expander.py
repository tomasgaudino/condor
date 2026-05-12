"""Deterministic candidate expander.

Spec: ``.planning/strategy-framework/PATCH_CONTRACT_SPEC.md`` §Estado 2,
``.planning/strategy-framework/BACKTEST_LOOP_SPEC.md`` §2.5.3.

Given the LLM's qualitative proposal (``dimension + direction +
magnitude``), expand it into 1–3 specific numeric candidates. No LLM
involved — same input ⇒ same output.

Three field types from invariants.yaml:

- ``percentage_type``: multiplicative (``current × multiplier``).
- ``absolute_type``:   additive (``current + delta``), expressed in
  percentage points for ratios like ``target_base_pct``, in units for
  ``max_active_executors_by_level``.
- ``categorical_type``: toggle/switch. The expander emits the OPPOSITE
  value (``not bool`` for booleans; for string enums it leaves a
  placeholder — the LLM should call out the new value explicitly via
  a future schema extension).

Per the spec, ``small`` magnitude generates 1 candidate; ``medium``
and ``large`` generate 2 each.

After raw expansion the candidates pass through:
1. Per-field invariants overrides (``max_increase`` / ``max_decrease``
   in ``invariants.field_overrides``), tagging candidates that hit the
   limit with ``clamped: True``.
2. Domain clamps (e.g. ratios must stay in ``[0, 1]``).
3. Dedup by value (within float tolerance).

If everything collapses to the baseline, the loop above handles the
``no_valid_candidates`` case.
"""

from __future__ import annotations

import logging
from typing import Any

from condor.trading_agent.adaptive.invariants import Invariants

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Expansion tables (spec §Tablas de expansión)
# ---------------------------------------------------------------------------


_PCT_TABLE: dict[str, dict[str, list[float]]] = {
    "small":  {"increase": [1.25],         "decrease": [0.80]},
    "medium": {"increase": [1.5, 2.0],     "decrease": [0.67, 0.50]},
    "large":  {"increase": [2.0, 3.0],     "decrease": [0.50, 0.33]},
}


_ABS_TABLE: dict[str, dict[str, list[float]]] = {
    "small":  {"increase": [0.02],         "decrease": [-0.02]},
    "medium": {"increase": [0.05, 0.08],   "decrease": [-0.05, -0.08]},
    "large":  {"increase": [0.10],         "decrease": [-0.10]},
}


# Fields where the "absolute" delta is in integer units, not in
# percentage points. Override on case-by-case from invariants.field_overrides.
_INTEGER_ABSOLUTE_FIELDS: frozenset[str] = frozenset({"max_active_executors_by_level"})


# Domain clamps — basic sanity. The spec calls this "clamp por dominio".
_DOMAIN_CLAMPS: dict[str, tuple[float, float]] = {
    "target_base_pct":  (0.0, 1.0),
    "min_base_pct":     (0.0, 1.0),
    "max_base_pct":     (0.0, 1.0),
    "portfolio_allocation": (0.0, 1.0),
    "buy_amounts_pct":  (0.0, 1.0),
    "sell_amounts_pct": (0.0, 1.0),
    "min_skew":         (0.0, 1.0),
    # Take profit / spreads / cooldowns / executor_refresh_time can't be
    # negative; their upper bound is left open (overridden by invariants).
    "take_profit":      (0.0, float("inf")),
    "buy_spreads":      (0.0, float("inf")),
    "sell_spreads":     (0.0, float("inf")),
    "executor_refresh_time":      (0.0, float("inf")),
    "buy_cooldown_time":          (0.0, float("inf")),
    "sell_cooldown_time":         (0.0, float("inf")),
    "price_distance_tolerance":   (0.0, float("inf")),
    "refresh_tolerance":          (0.0, float("inf")),
    "tolerance_scaling":          (0.0, float("inf")),
    "max_active_executors_by_level": (1.0, float("inf")),
}


_FLOAT_EQ_TOL = 1e-9


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def expand_proposal(
    proposal: dict[str, Any],
    current_value: Any,
    invariants: Invariants,
) -> dict[str, Any]:
    """Expand one LLM proposal into N candidates.

    Returns the CANDIDATES dict per spec, including the
    ``expansion_strategy`` label, the per-candidate value/delta/clamped
    fields, and a ``status`` of ``ok`` | ``no_valid_candidates`` |
    ``unsupported_field_type``.

    The caller (backtest loop) is responsible for skipping the
    proposal when status != ``ok``.
    """
    field = proposal["dimension"]
    direction = proposal["direction"]
    magnitude = proposal["magnitude_qualitative"]

    field_type = invariants.field_type(field)
    if field_type is None:
        # Could be in field_overrides without a top-level type — check
        # the override declares the type explicitly.
        override = invariants.field_overrides.get(field, {})
        otype = override.get("type")
        if otype == "percentage_of_current":
            field_type = "percentage_type"
        elif otype == "absolute":
            field_type = "absolute_type"

    if field_type is None or field_type == "categorical_type":
        if field_type == "categorical_type":
            return _expand_categorical(proposal, current_value, field)
        return {
            "proposal_ref": _proposal_ref(proposal),
            "current_value": current_value,
            "field_type": field_type,
            "expansion_strategy": "n/a",
            "candidates": [],
            "status": "unsupported_field_type",
        }

    # Numeric expansion
    if field_type == "percentage_type":
        table = _PCT_TABLE
    elif field_type == "absolute_type":
        table = _ABS_TABLE
    else:
        return {
            "proposal_ref": _proposal_ref(proposal),
            "current_value": current_value,
            "field_type": field_type,
            "expansion_strategy": "n/a",
            "candidates": [],
            "status": "unsupported_field_type",
        }

    factors = table.get(magnitude, {}).get(direction, [])
    if not factors:
        return {
            "proposal_ref": _proposal_ref(proposal),
            "current_value": current_value,
            "field_type": field_type,
            "expansion_strategy": "n/a",
            "candidates": [],
            "status": "unsupported_field_type",
        }

    # Raw candidates from the table
    raw = []
    for f in factors:
        if field_type == "percentage_type":
            raw.append(_round_field(field, current_value * f))
        else:
            raw.append(_round_field(field, current_value + f))

    # Per-field invariant clamps (max_increase / max_decrease relative
    # to current value).
    override = invariants.field_overrides.get(field, {})
    max_inc = float(override.get("max_increase", _default_max(invariants, field_type, "max_increase")))
    max_dec = float(override.get("max_decrease", _default_max(invariants, field_type, "max_decrease")))

    # The clamp depends on the override type. For
    # "percentage_of_current" the limits are multiplicative bounds on
    # the relative change. For "absolute" they're additive bounds.
    # When the override is missing entirely, the global defaults from
    # default_max_delta apply: percentage_type → multiplicative,
    # absolute_type → additive in pp.
    ov_type = override.get("type")
    use_multiplicative = (
        ov_type == "percentage_of_current"
        or (ov_type is None and field_type == "percentage_type")
    )

    candidates = []
    for i, val in enumerate(raw):
        clamped_val, was_clamped = _clamp_value(
            current=current_value,
            new=val,
            max_increase=max_inc,
            max_decrease=max_dec,
            multiplicative=use_multiplicative,
        )
        clamped_val = _round_field(field, clamped_val)
        # Domain clamp on top.
        domain_lo, domain_hi = _DOMAIN_CLAMPS.get(field, (-float("inf"), float("inf")))
        domain_clamped_val = max(domain_lo, min(domain_hi, clamped_val))
        if not _approx_eq(domain_clamped_val, clamped_val):
            was_clamped = True
            clamped_val = domain_clamped_val
        candidates.append({
            "id": f"c{i + 1}",
            "value": _round_field(field, clamped_val),
            "delta_relative": _format_delta(field_type, current_value, factors[i]),
            "clamped": was_clamped,
        })

    # Dedup by value, then drop candidates equal to current (they would
    # be no-ops). Re-ID c1..cN.
    deduped = _dedup_keep_order(candidates)
    non_trivial = [c for c in deduped if not _approx_eq(c["value"], current_value)]
    for i, c in enumerate(non_trivial):
        c["id"] = f"c{i + 1}"

    status = "ok" if non_trivial else "no_valid_candidates"

    return {
        "proposal_ref": _proposal_ref(proposal),
        "current_value": current_value,
        "field_type": field_type,
        "expansion_strategy": f"{field_type}_{magnitude}_{direction}",
        "candidates": non_trivial,
        "status": status,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _proposal_ref(p: dict[str, Any]) -> dict[str, Any]:
    return {
        "controller_id": p["controller_id"],
        "dimension": p["dimension"],
        "direction": p["direction"],
        "magnitude_qualitative": p["magnitude_qualitative"],
    }


def _default_max(invariants: Invariants, field_type: str, key: str) -> float:
    dmd = invariants.default_max_delta
    return float(dmd.get(field_type, {}).get(key, 0.5))


def _clamp_value(
    *,
    current: float,
    new: float,
    max_increase: float,
    max_decrease: float,
    multiplicative: bool,
) -> tuple[float, bool]:
    """Apply per-field max_increase / max_decrease.

    ``multiplicative=True``: limits are scaling factors relative to
    current. e.g. ``max_increase=1.0`` ⇒ cap at ``2 × current``.
    ``multiplicative=False``: limits are absolute deltas (in same units
    as the field).
    """
    if multiplicative:
        if current == 0:
            return new, False  # nothing to scale against
        upper = current * (1 + max_increase)
        lower = current * (1 - max_decrease)
    else:
        upper = current + max_increase
        lower = current - max_decrease

    if new > upper:
        return upper, True
    if new < lower:
        return lower, True
    return new, False


def _approx_eq(a: float, b: float) -> bool:
    try:
        return abs(float(a) - float(b)) < _FLOAT_EQ_TOL
    except (TypeError, ValueError):
        return a == b


def _dedup_keep_order(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop duplicates by value, preserving the first occurrence.

    Sticky clamped flag: when a later candidate is dropped because it
    collides with an earlier one AND the dropped candidate was
    ``clamped: True``, propagate that flag to the survivor. This way
    the report tells the human "this value was reached by a clamp"
    even if the surviving raw candidate happened to land on the same
    number without clamping.
    """
    out: list[dict[str, Any]] = []
    for c in candidates:
        existing = next((o for o in out if _approx_eq(c["value"], o["value"])), None)
        if existing is not None:
            if c.get("clamped"):
                existing["clamped"] = True
            continue
        out.append(c)
    return out


def _round_field(field: str, value: float) -> float:
    """Integer fields round to int; others to a reasonable precision.

    We over-precision aggressively on small fields like ``take_profit``
    (down to 1e-8). The backtest engine doesn't care, but humans do
    when reading the audit log.
    """
    if field in _INTEGER_ABSOLUTE_FIELDS:
        return int(round(value))
    # Sane precision floor: round to 1e-8 (e.g. take_profit 0.0001).
    return round(float(value), 8)


def _format_delta(field_type: str, current: float, factor_or_delta: float) -> str:
    if field_type == "percentage_type":
        return f"× {factor_or_delta:.2f}"
    # absolute
    sign = "+" if factor_or_delta >= 0 else ""
    return f"{sign}{factor_or_delta:.3f}"


# ---------------------------------------------------------------------------
# Categorical expansion (boolean toggles, mostly)
# ---------------------------------------------------------------------------


def _expand_categorical(
    proposal: dict[str, Any], current_value: Any, field: str
) -> dict[str, Any]:
    """Boolean fields: emit the opposite. Other categorical fields
    (string enums like ``take_profit_order_type``) are NOT supported
    by the deterministic expander yet — they need explicit values from
    the LLM. We return ``unsupported_field_type`` for those so the
    caller can skip cleanly.
    """
    if isinstance(current_value, bool):
        new = not current_value
        return {
            "proposal_ref": _proposal_ref(proposal),
            "current_value": current_value,
            "field_type": "categorical_type",
            "expansion_strategy": "boolean_toggle",
            "candidates": [{
                "id": "c1",
                "value": new,
                "delta_relative": f"toggle → {new}",
                "clamped": False,
            }],
            "status": "ok",
        }
    return {
        "proposal_ref": _proposal_ref(proposal),
        "current_value": current_value,
        "field_type": "categorical_type",
        "expansion_strategy": "n/a",
        "candidates": [],
        "status": "unsupported_field_type",
    }
