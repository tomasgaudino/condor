"""Scoring of backtest results against the baseline.

Spec: ``.planning/strategy-framework/BACKTEST_LOOP_SPEC.md`` §2.5.4
and ``PATCH_CONTRACT_SPEC.md`` §Estado 3.

User criterion (D9):

    "PnL ≈ 0+ con mucho volumen > PnL alto con poco volumen"

Formula:

    pnl_norm = min(pnl, baseline_pnl * 2) / max(baseline_pnl, 1.0)
    vol_norm = vol / max(baseline_vol, 1.0)
    pnl_norm, vol_norm = clamp to 10.0
    score = vol_norm + 0.3 * pnl_norm

with a hard reject when ``pnl < 0`` (score = -inf, candidate dropped
upstream).

**Caveat on the baseline score**: the spec asserts "baseline = 1.3 by
construction" but that only holds when ``baseline_pnl >= 1.0`` (in
quote currency). Below that, the ``max(baseline_pnl, 1.0)`` floor in
the denominator dampens ``pnl_norm``, so the actual baseline score
slides below 1.3. We surface the real baseline score from
:func:`baseline_score_decomposition` rather than hard-coding it — the
"is candidate better than baseline?" check uses the actual numeric
comparison, not the nominal 1.3 reference.

The function returns a decomposition dict so the audit log keeps
every term — useful for both human-facing report and post-hoc
calibration (Phase F2).
"""

from __future__ import annotations

import math
from typing import Any


BASELINE_SCORE = 1.0 + 0.3  # exactly 1.3
NORM_CLAMP = 10.0
PNL_WEIGHT = 0.3


def score(pnl: float, volume: float, baseline_pnl: float, baseline_vol: float) -> float:
    """Numeric score of one candidate against the baseline.

    Returns ``-inf`` if PnL is strictly negative — the loop treats this
    as a hard reject. NaN inputs propagate to ``-inf`` (defensive).
    """
    if pnl is None or _isnan(pnl) or pnl < 0:
        return float("-inf")
    if volume is None or _isnan(volume) or volume < 0:
        return float("-inf")

    pnl_norm = _pnl_norm(pnl, baseline_pnl)
    vol_norm = _vol_norm(volume, baseline_vol)
    return vol_norm + PNL_WEIGHT * pnl_norm


def score_with_decomposition(
    pnl: float, volume: float,
    baseline_pnl: float, baseline_vol: float,
) -> dict[str, Any]:
    """Score plus the full breakdown of every term.

    Shape mirrors the spec exactly — DON'T silently rename keys, the
    audit log + reports depend on them.
    """
    rejected = (
        pnl is None or _isnan(pnl) or pnl < 0
        or volume is None or _isnan(volume) or volume < 0
    )
    pnl_norm = None if rejected else _pnl_norm(pnl, baseline_pnl)
    vol_norm = None if rejected else _vol_norm(volume, baseline_vol)

    if rejected:
        s = float("-inf")
        weighted_pnl = None
    else:
        s = vol_norm + PNL_WEIGHT * pnl_norm
        weighted_pnl = PNL_WEIGHT * pnl_norm

    return {
        "score": s,
        "components": {
            "pnl_norm": pnl_norm,
            "vol_norm": vol_norm,
            "weighted_pnl": weighted_pnl,
            "raw_pnl": pnl,
            "raw_vol": volume,
            "baseline_pnl": baseline_pnl,
            "baseline_vol": baseline_vol,
        },
        "formula": "vol_norm + 0.3 * pnl_norm",
        "rejected_for_negative_pnl": pnl is not None and not _isnan(pnl) and pnl < 0,
    }


def baseline_score_decomposition(
    pnl: float, volume: float
) -> dict[str, Any]:
    """Decomposition for the baseline itself — by construction score = 1.3."""
    return score_with_decomposition(pnl, volume, pnl, volume)


def select_winner(
    candidates_with_scores: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Pick the candidate with the highest score.

    ``candidates_with_scores`` is a list of dicts each with at least
    ``id``, ``value``, ``score``, ``score_decomposition``. Candidates
    with ``score = -inf`` are excluded automatically.

    Returns ``None`` when no candidate has a finite score.
    """
    finite = [c for c in candidates_with_scores
              if isinstance(c.get("score"), (int, float)) and not _isnan(c["score"])
              and c["score"] != float("-inf")]
    if not finite:
        return None
    return max(finite, key=lambda c: c["score"])


def all_candidates_worse_than_baseline(
    candidates_with_scores: list[dict[str, Any]],
    baseline_score: float,
) -> bool:
    """True when every (finite-score) candidate scores below the
    actual baseline.

    Pass the baseline's real score (from
    :func:`baseline_score_decomposition`), NOT the nominal
    ``BASELINE_SCORE = 1.3`` constant — they only line up when the
    baseline PnL is ≥ 1.0 in quote currency, see the module docstring.
    """
    finite = [c for c in candidates_with_scores
              if isinstance(c.get("score"), (int, float))
              and c["score"] != float("-inf") and not _isnan(c["score"])]
    if not finite:
        return False  # everything is negative-PnL or NaN — different path
    return all(c["score"] < baseline_score for c in finite)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pnl_norm(pnl: float, baseline_pnl: float) -> float:
    capped = min(pnl, max(baseline_pnl, 0) * 2)
    denom = max(baseline_pnl, 1.0)
    return min(capped / denom, NORM_CLAMP)


def _vol_norm(volume: float, baseline_vol: float) -> float:
    return min(volume / max(baseline_vol, 1.0), NORM_CLAMP)


def _isnan(x: Any) -> bool:
    return isinstance(x, float) and math.isnan(x)
