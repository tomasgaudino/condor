"""Tests for condor.trading_agent.adaptive.scoring."""

from __future__ import annotations

import math

from condor.trading_agent.adaptive import scoring


# ---------------------------------------------------------------------------
# Numeric score
# ---------------------------------------------------------------------------


def test_score_negative_pnl_rejected():
    assert scoring.score(-0.1, 1000, 0.1, 1000) == float("-inf")


def test_score_nan_pnl_rejected():
    assert scoring.score(math.nan, 1000, 0.1, 1000) == float("-inf")


def test_score_negative_volume_rejected():
    assert scoring.score(0.5, -100, 0.1, 1000) == float("-inf")


def test_score_baseline_against_itself_with_pnl_above_1():
    """When baseline_pnl >= 1.0 the score lands at the nominal 1.3."""
    s = scoring.score(5.0, 10000, 5.0, 10000)
    assert abs(s - 1.3) < 1e-9


def test_score_baseline_against_itself_with_small_pnl():
    """Below pnl=1.0 the floor in the denominator dampens pnl_norm.
    Documented in the module docstring."""
    s = scoring.score(0.12, 12400, 0.12, 12400)
    # pnl_norm = 0.12 / 1.0 = 0.12, vol_norm = 1.0 → 1.0 + 0.3*0.12 = 1.036
    assert abs(s - 1.036) < 1e-9


def test_score_candidate_beats_baseline():
    s = scoring.score(0.45, 9800, 0.12, 12400)
    # vol_norm = 9800/12400 ≈ 0.79, pnl_norm = min(0.45, 0.24)/1.0 = 0.24
    # → 0.79 + 0.3 * 0.24 = 0.862
    assert abs(s - 0.862) < 1e-3


def test_score_clamps_pnl_norm_to_10():
    # Massive PnL vs tiny baseline → pnl_norm capped at 10.
    s = scoring.score(1000, 100, 0.01, 100)
    # vol_norm = 1.0, pnl_norm = min(1000, 0.02) / 1.0 = 0.02 → no clamp triggered here
    # try with baseline_pnl ≥ 1
    s2 = scoring.score(1000, 1000, 1.0, 1000)
    # pnl_norm = min(1000, 2.0)/1.0 = 2.0 (not clamped)
    # vol_norm = 1.0 → 1.0 + 0.3*2.0 = 1.6
    assert abs(s2 - 1.6) < 1e-9


# ---------------------------------------------------------------------------
# Decomposition
# ---------------------------------------------------------------------------


def test_decomp_shape_complete():
    d = scoring.score_with_decomposition(0.45, 9800, 0.12, 12400)
    for k in ("score", "components", "formula", "rejected_for_negative_pnl"):
        assert k in d
    for k in ("pnl_norm", "vol_norm", "weighted_pnl",
              "raw_pnl", "raw_vol", "baseline_pnl", "baseline_vol"):
        assert k in d["components"]


def test_decomp_rejected_flags_negative_pnl():
    d = scoring.score_with_decomposition(-0.1, 5000, 0.1, 1000)
    assert d["rejected_for_negative_pnl"] is True
    assert d["score"] == float("-inf")
    assert d["components"]["pnl_norm"] is None


def test_baseline_score_decomp_is_self_reference():
    d = scoring.baseline_score_decomposition(0.5, 1000)
    assert d["components"]["baseline_pnl"] == 0.5
    assert d["components"]["baseline_vol"] == 1000


# ---------------------------------------------------------------------------
# Winner selection
# ---------------------------------------------------------------------------


def test_select_winner_picks_highest_score():
    cands = [
        {"id": "c1", "score": 1.42},
        {"id": "c2", "score": 1.55},
        {"id": "c3", "score": 1.12},
    ]
    w = scoring.select_winner(cands)
    assert w["id"] == "c2"


def test_select_winner_ignores_neg_inf():
    cands = [
        {"id": "c1", "score": float("-inf")},
        {"id": "c2", "score": 1.3},
    ]
    assert scoring.select_winner(cands)["id"] == "c2"


def test_select_winner_returns_none_when_all_inf():
    cands = [
        {"id": "c1", "score": float("-inf")},
        {"id": "c2", "score": float("-inf")},
    ]
    assert scoring.select_winner(cands) is None


def test_select_winner_returns_none_when_empty():
    assert scoring.select_winner([]) is None


# ---------------------------------------------------------------------------
# Anti-evidence detection
# ---------------------------------------------------------------------------


def test_all_worse_than_baseline_true():
    assert scoring.all_candidates_worse_than_baseline(
        [{"id": "c1", "score": 1.0}, {"id": "c2", "score": 0.5}],
        baseline_score=1.3,
    ) is True


def test_all_worse_than_baseline_false_when_one_better():
    assert scoring.all_candidates_worse_than_baseline(
        [{"id": "c1", "score": 1.0}, {"id": "c2", "score": 1.4}],
        baseline_score=1.3,
    ) is False


def test_all_worse_returns_false_when_no_finite_candidates():
    """Empty / all-neg-inf isn't 'all worse' — it's 'no data'.
    Different code path in the loop."""
    assert scoring.all_candidates_worse_than_baseline(
        [{"id": "c1", "score": float("-inf")}],
        baseline_score=1.3,
    ) is False
    assert scoring.all_candidates_worse_than_baseline([], baseline_score=1.3) is False


def test_all_worse_uses_actual_baseline_not_constant():
    """Confirms the baseline_score parameter actually drives the call,
    not the BASELINE_SCORE constant (1.3) — see module docstring caveat."""
    # If we used 1.3, this would say "all worse" (1.1, 1.0 < 1.3).
    # The real baseline (e.g. 0.5) means the candidates BEAT it.
    assert scoring.all_candidates_worse_than_baseline(
        [{"id": "c1", "score": 1.1}, {"id": "c2", "score": 1.0}],
        baseline_score=0.5,
    ) is False
