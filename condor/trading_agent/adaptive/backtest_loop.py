"""Backtest Evidence Loop — Fase 2.5 implementation.

Spec: ``.planning/strategy-framework/BACKTEST_LOOP_SPEC.md``.

The loop turns an LLM proposal into a SELECTED PATCH (the data the
agent ships to ``handlers/adaptive.send_proposal``). Pipeline:

1. **Trigger check** — should this proposal even enter the loop?
   Order of cheapest-first checks: agent paused → not suboptimal →
   regime optimal → anti-evidence streak → cooldowns active.
2. **Window selection** — use the controller's
   ``suboptimal_period_minutes``, floor at 30 min.
3. **Expand** the proposal into deterministic candidates.
4. **Backtest** baseline + each candidate (serialized through a global
   asyncio lock to avoid engine collisions per D9). Cached by
   sha256 of (controller, config, window).
5. **Score** every candidate against the actual baseline result.
6. **Select winner** OR mark anti-evidence when all candidates worse.

The loop is **stateless across ticks**: it doesn't write anything by
itself. The caller (Phase 5.7 orchestrator) decides which dict it
persists to the audit log.

All errors propagate as dict outputs (``verdict: <reason>``) instead
of exceptions, so the orchestrator can audit-log everything uniformly.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from condor.trading_agent.adaptive import backtest_cache as bt_cache
from condor.trading_agent.adaptive import audit as audit_io
from condor.trading_agent.adaptive import expander
from condor.trading_agent.adaptive import scoring
from condor.trading_agent.adaptive.invariants import Invariants

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MIN_WINDOW_MINUTES = 30.0
CYCLE_TIMEOUT_SECONDS = 300  # D9: 5 min hard cap
DEFAULT_RESOLUTION = "1m"
DEFAULT_TRADE_COST = 0.0006


# Module-level lock to serialize backtests against Hummingbot's
# semi-singleton engine (D9 §Engine compartido).
_BACKTEST_LOCK = asyncio.Lock()


# ---------------------------------------------------------------------------
# Trigger
# ---------------------------------------------------------------------------


def should_trigger_backtest_loop(
    *,
    agent_paused: bool,
    suboptimal_now: bool,
    favorability: str,
    cooldown_active: bool,
) -> tuple[bool, str]:
    """Spec §2.5.1, in cheapest-first order.

    The anti-evidence check requires the LLM proposal in hand, so the
    orchestrator runs it AFTER expansion (not here). We keep the cheap
    pre-checks centralized so the same logic powers both
    ``audit_log no_trigger`` entries and the loop entry.
    """
    if agent_paused:
        return False, "agent_paused"
    if not suboptimal_now:
        return False, "not_suboptimal"
    if favorability == "optimal":
        return False, "regime_optimal"
    if cooldown_active:
        return False, "cooldowns_active"
    return True, "ok"


# ---------------------------------------------------------------------------
# Window selection
# ---------------------------------------------------------------------------


def select_backtest_window(
    *,
    suboptimal_period_minutes: float | None,
    now: datetime,
) -> dict[str, Any]:
    """Pick the window. No upper cap (D9): if the regime has been
    subóptimo for 3 days, backtest 3 days. Lower floor: 30 minutes.

    The real protection against ridiculous windows is the cycle-level
    timeout (``CYCLE_TIMEOUT_SECONDS``).
    """
    minutes = max(suboptimal_period_minutes or 0.0, MIN_WINDOW_MINUTES)
    start = now - timedelta(minutes=minutes)
    return {
        "start_ts": start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end_ts":   now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "duration_minutes": minutes,
        "resolution": DEFAULT_RESOLUTION,
        "_start_dt": start,
        "_end_dt": now,
    }


# ---------------------------------------------------------------------------
# Connector support (D9)
# ---------------------------------------------------------------------------


def can_backtest(
    *, connector_name: str, invariants: Invariants
) -> bool:
    """False when connector is on the unsupported list per
    ``invariants.backtest.unsupported_connectors``."""
    unsupported = set((invariants.backtest or {}).get("unsupported_connectors") or [])
    return connector_name not in unsupported


# ---------------------------------------------------------------------------
# Single backtest (cached, locked, timed-out)
# ---------------------------------------------------------------------------


async def _run_backtest_cached(
    *,
    client: Any,
    agent_dir: Path,
    controller_id: str,
    config_snapshot: dict,
    window: dict[str, Any],
    timeout_seconds: float,
) -> tuple[dict | None, str, dict | None]:
    """Run one backtest with cache + lock + timeout.

    Returns ``(result, source, error)`` where:
    - ``result``  — the dict returned by the HB backtest endpoint (or
      None on error).
    - ``source``  — ``"cache_hit"`` | ``"cache_miss"`` | ``"error"``.
    - ``error``   — None on success; a dict describing the failure
      otherwise.
    """
    key = bt_cache.cache_key(
        controller_id=controller_id,
        config_snapshot=config_snapshot,
        start_ts=window["_start_dt"],
        end_ts=window["_end_dt"],
        resolution=window["resolution"],
    )

    cached = bt_cache.get(agent_dir, key)
    if cached is not None:
        return cached.get("result"), "cache_hit", None

    # The backtest endpoint validates the config with Pydantic
    # ``extra="forbid"`` — but ``get_bot_controller_configs`` injects
    # underscore-prefixed metadata (``_config_name``) that the model
    # doesn't declare. Strip those before sending. Same asymmetry
    # noted in LEARNINGS L11 for ``validate_controller_config``.
    clean_config = {k: v for k, v in config_snapshot.items()
                    if not (isinstance(k, str) and k.startswith("_"))}

    async with _BACKTEST_LOCK:
        try:
            result = await asyncio.wait_for(
                client.backtesting.run(
                    start_time=int(window["_start_dt"].timestamp()),
                    end_time=int(window["_end_dt"].timestamp()),
                    backtesting_resolution=window["resolution"],
                    trade_cost=DEFAULT_TRADE_COST,
                    config=clean_config,
                ),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            return None, "error", {
                "type": "timeout",
                "detail": f"exceeded {timeout_seconds}s",
            }
        except Exception as e:
            return None, "error", {
                "type": "backtest_error",
                "detail": str(e),
            }

    # Hummingbot sometimes returns HTTP 200 with ``{"error": "..."}``
    # instead of raising — surface those as backtest_error so the
    # cycle classifies the verdict correctly. We DON'T cache errors.
    if isinstance(result, dict) and result.get("error") and "results" not in result:
        return None, "error", {
            "type": "backtest_error",
            "detail": str(result["error"]),
        }

    bt_cache.put(
        agent_dir, key, result,
        meta={
            "controller_id": controller_id,
            "window_start": window["start_ts"],
            "window_end":   window["end_ts"],
        },
    )
    return result, "cache_miss", None


def _extract_result_metrics(raw: dict | None) -> tuple[float | None, float | None]:
    """Pull ``net_pnl_quote`` and a volume figure from the backtest
    response.

    The HB endpoint shape varies a bit across versions; we probe a few
    likely keys and fall back to ``None`` if nothing matches — the
    scorer treats ``None`` as a hard reject.
    """
    if not isinstance(raw, dict):
        return None, None
    # net_pnl
    pnl = raw.get("net_pnl_quote")
    if pnl is None:
        results = raw.get("results") or {}
        pnl = results.get("net_pnl_quote") or results.get("net_pnl")
    # volume
    vol = raw.get("total_volume") or raw.get("volume_traded")
    if vol is None:
        results = raw.get("results") or {}
        vol = results.get("total_volume") or results.get("volume_traded")

    def _maybe_float(x):
        try:
            return float(x) if x is not None else None
        except (TypeError, ValueError):
            return None

    return _maybe_float(pnl), _maybe_float(vol)


# ---------------------------------------------------------------------------
# Full cycle
# ---------------------------------------------------------------------------


async def run_backtest_cycle(
    *,
    client: Any,
    agent_dir: Path,
    proposal: dict[str, Any],
    current_config: dict[str, Any],
    controller_id: str,
    invariants: Invariants,
    now: datetime,
    suboptimal_period_minutes: float | None,
) -> dict[str, Any]:
    """End-to-end loop for ONE proposal.

    Returns the spec's "output del ciclo" dict (see
    BACKTEST_LOOP_SPEC §Estructura del output), with ``verdict`` set
    to one of:

    - ``winner_found``                  → ``selected`` populated, ready
      for propose mode.
    - ``no_valid_candidates``           → expansion collapsed to nothing.
    - ``all_candidates_negative_pnl``   → every candidate had PnL < 0.
    - ``all_candidates_worse_than_baseline`` → anti-evidence trigger.
    - ``baseline_backtest_failed``      → can't score without baseline.
    - ``cycle_timeout``                 → 5-min cap hit during
      candidate runs.
    - ``connector_unsupported``         → connector on the blacklist;
      caller applies ``on_backtest_failure`` policy.
    """
    t0 = time.monotonic()
    cache_stats = {"hits": 0, "misses": 0}

    field = proposal["dimension"]
    connector = current_config.get("connector_name", "")

    # 0. Connector support gate
    if not can_backtest(connector_name=connector, invariants=invariants):
        return _empty_cycle(
            proposal=proposal, field=field,
            verdict="connector_unsupported",
            errors=[{"type": "connector_unsupported", "detail": connector}],
            cache_stats=cache_stats,
            t0=t0,
        )

    # 1. Window selection
    window = select_backtest_window(
        suboptimal_period_minutes=suboptimal_period_minutes,
        now=now,
    )

    # 2. Expand
    expansion = expander.expand_proposal(proposal, current_config.get(field), invariants)
    if expansion["status"] != "ok" or not expansion["candidates"]:
        return _empty_cycle(
            proposal=proposal, field=field,
            verdict=expansion.get("status", "no_valid_candidates"),
            errors=[],
            cache_stats=cache_stats,
            t0=t0,
            window=window,
            expansion=expansion,
        )

    candidates = expansion["candidates"]
    old_value = expansion["current_value"]

    # 3. Cycle-level timeout wraps baseline + candidates
    try:
        async with asyncio.timeout(CYCLE_TIMEOUT_SECONDS):
            baseline_pnl, baseline_vol, baseline_source, baseline_err = await _run_one(
                client=client, agent_dir=agent_dir,
                controller_id=controller_id, config_snapshot=current_config,
                window=window,
            )
            _bump(cache_stats, baseline_source)

            if baseline_err is not None:
                return _empty_cycle(
                    proposal=proposal, field=field,
                    verdict="baseline_backtest_failed",
                    errors=[baseline_err],
                    cache_stats=cache_stats, t0=t0,
                    window=window, expansion=expansion,
                )

            # 4. Run each candidate (serialized via lock inside _run_one)
            cand_runs: list[dict[str, Any]] = []
            for cand in candidates:
                cand_cfg = {**current_config, field: cand["value"]}
                pnl, vol, source, err = await _run_one(
                    client=client, agent_dir=agent_dir,
                    controller_id=controller_id, config_snapshot=cand_cfg,
                    window=window,
                )
                _bump(cache_stats, source)
                cand_runs.append({
                    "id": cand["id"],
                    "value": cand["value"],
                    "delta_relative": cand.get("delta_relative"),
                    "clamped": cand.get("clamped", False),
                    "pnl": pnl, "vol": vol,
                    "source": source, "error": err,
                })
    except asyncio.TimeoutError:
        return _empty_cycle(
            proposal=proposal, field=field,
            verdict="cycle_timeout",
            errors=[{"type": "cycle_timeout", "detail": f"{CYCLE_TIMEOUT_SECONDS}s"}],
            cache_stats=cache_stats, t0=t0,
            window=window, expansion=expansion,
        )

    # 5. Score everything against the real baseline
    baseline_decomp = scoring.baseline_score_decomposition(baseline_pnl or 0.0, baseline_vol or 0.0)
    baseline_score = baseline_decomp["score"]

    candidates_with_scores: list[dict[str, Any]] = []
    for r in cand_runs:
        if r["error"] is not None or r["pnl"] is None or r["vol"] is None:
            candidates_with_scores.append({
                **r,
                "score": float("-inf"),
                "score_decomposition": {"score": float("-inf"),
                                         "error": r["error"] or "missing_metrics"},
            })
            continue
        decomp = scoring.score_with_decomposition(
            r["pnl"], r["vol"], baseline_pnl or 0.0, baseline_vol or 0.0,
        )
        candidates_with_scores.append({
            **r,
            "score": decomp["score"],
            "score_decomposition": decomp,
        })

    # 6. Pick winner / handle anti-evidence
    winner = scoring.select_winner(candidates_with_scores)
    verdict, rejected = _classify_verdict(
        candidates_with_scores, baseline_score=baseline_score, winner=winner,
    )

    selected = None
    if verdict == "winner_found" and winner is not None:
        selected = {
            "id": winner["id"],
            "value": winner["value"],
            "score": winner["score"],
            "vs_baseline_delta": winner["score"] - baseline_score,
        }

    return {
        "controller_id": controller_id,
        "field": field,
        "old_value": old_value,
        "window": _public_window(window),
        "baseline": {
            "value": old_value,
            "pnl": baseline_pnl, "vol": baseline_vol,
            "score_decomposition": baseline_decomp,
            "source": baseline_source,
        },
        "candidates": [_public_candidate(c) for c in candidates_with_scores],
        "selected": selected,
        "rejected": rejected,
        "verdict": verdict,
        "errors": _collect_errors(cand_runs),
        "total_duration_seconds": round(time.monotonic() - t0, 2),
        "cache_stats": cache_stats,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _run_one(
    *,
    client: Any,
    agent_dir: Path,
    controller_id: str,
    config_snapshot: dict,
    window: dict[str, Any],
) -> tuple[float | None, float | None, str, dict | None]:
    """Run one backtest, extract metrics. ``source`` is propagated for
    cache stats."""
    raw, source, err = await _run_backtest_cached(
        client=client, agent_dir=agent_dir,
        controller_id=controller_id, config_snapshot=config_snapshot,
        window=window, timeout_seconds=CYCLE_TIMEOUT_SECONDS,
    )
    if err is not None:
        return None, None, source, err
    pnl, vol = _extract_result_metrics(raw)
    return pnl, vol, source, None


def _classify_verdict(
    candidates_with_scores: list[dict[str, Any]],
    *, baseline_score: float, winner: dict | None,
) -> tuple[str, list[dict]]:
    """Resolve the verdict + rejected list per the truth table in
    PATCH_CONTRACT_SPEC.md §D-Q3."""
    if not candidates_with_scores:
        return "no_valid_candidates", []

    all_neg_pnl = all(c.get("pnl") is not None and c["pnl"] < 0
                      for c in candidates_with_scores)
    if all_neg_pnl:
        return "all_candidates_negative_pnl", []

    if winner is None:
        # No finite score → most likely all errors. Fall through to the
        # baseline-comparison check is meaningless, so return a
        # generic verdict and let errors[] explain.
        return "no_valid_candidates", []

    if scoring.all_candidates_worse_than_baseline(candidates_with_scores, baseline_score):
        return "all_candidates_worse_than_baseline", [
            {"id": c["id"], "score": c["score"],
             "reason": "score < baseline" }
            for c in candidates_with_scores
        ]

    # Winner found — everyone else is "lower score"
    rejected = [
        {"id": c["id"], "score": c["score"], "reason": "lower score"}
        for c in candidates_with_scores
        if c["id"] != winner["id"]
    ]
    return "winner_found", rejected


def _public_window(w: dict[str, Any]) -> dict[str, Any]:
    """Strip internal `_start_dt` / `_end_dt` from the public view."""
    return {k: v for k, v in w.items() if not k.startswith("_")}


def _public_candidate(c: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": c["id"],
        "value": c["value"],
        "delta_relative": c.get("delta_relative"),
        "clamped": c.get("clamped", False),
        "pnl": c.get("pnl"),
        "vol": c.get("vol"),
        "score": c.get("score"),
        "score_decomposition": c.get("score_decomposition"),
        "source": c.get("source"),
        "error": c.get("error"),
    }


def _collect_errors(cand_runs: list[dict[str, Any]]) -> list[dict]:
    return [
        {"candidate_id": r["id"], **(r["error"] or {})}
        for r in cand_runs if r["error"] is not None
    ]


def _bump(cache_stats: dict, source: str) -> None:
    if source == "cache_hit":
        cache_stats["hits"] += 1
    elif source == "cache_miss":
        cache_stats["misses"] += 1


def _empty_cycle(
    *,
    proposal: dict,
    field: str,
    verdict: str,
    errors: list[dict],
    cache_stats: dict,
    t0: float,
    window: dict[str, Any] | None = None,
    expansion: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a partial cycle output for early-exit paths."""
    return {
        "controller_id": proposal["controller_id"],
        "field": field,
        "old_value": expansion.get("current_value") if expansion else None,
        "window": _public_window(window) if window else None,
        "baseline": None,
        "candidates": [],
        "selected": None,
        "rejected": [],
        "verdict": verdict,
        "errors": errors,
        "total_duration_seconds": round(time.monotonic() - t0, 2),
        "cache_stats": cache_stats,
    }


# ---------------------------------------------------------------------------
# Anti-evidence log writer (PATCH_CONTRACT_SPEC §Anti-evidence log)
# ---------------------------------------------------------------------------


def write_anti_evidence(
    *,
    agent_dir: Path,
    tick_id: int,
    controller_id: str,
    regime_observed: str,
    suboptimal_minutes: float | None,
    proposal: dict[str, Any],
    cycle_result: dict[str, Any],
    now: datetime,
) -> None:
    """Append an entry to ``state/anti_evidence_log.jsonl``.

    Called by the orchestrator when the cycle returns
    ``all_candidates_worse_than_baseline``.
    """
    from condor.trading_agent.adaptive import state_io

    path = agent_dir / "state" / "anti_evidence_log.jsonl"
    entry = {
        "ts": state_io.utc_iso(now),
        "controller_id": controller_id,
        "tick_id": tick_id,
        "regime_observed": regime_observed,
        "suboptimal_minutes": suboptimal_minutes,
        "llm_proposal": {
            "dimension": proposal["dimension"],
            "direction": proposal["direction"],
            "magnitude_qualitative": proposal["magnitude_qualitative"],
        },
        "llm_reasoning": proposal.get("reasoning", ""),
        "candidates_tested": [
            {"id": c["id"], "value": c["value"],
             "score": c["score"],
             "vs_baseline": c["score"] - (cycle_result.get("baseline", {}) or {})
                            .get("score_decomposition", {}).get("score", 0.0)}
            for c in cycle_result.get("candidates", [])
        ],
        "verdict": cycle_result.get("verdict"),
        "interpretation_hint": "LLM may have wrong direction in this regime",
    }
    state_io.append_jsonl(path, entry)


def recent_anti_evidence_for(
    *,
    agent_dir: Path,
    controller_id: str,
    regime_observed: str,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Read the last ``limit`` anti-evidence entries that match
    (controller, regime). Used to detect streaks (spec §2.5.1 step 4)."""
    from condor.trading_agent.adaptive import state_io

    path = agent_dir / "state" / "anti_evidence_log.jsonl"
    if not path.exists():
        return []
    matches: list[dict] = []
    for entry in reversed(state_io.read_jsonl_all(path)):
        if entry.get("controller_id") != controller_id:
            continue
        if entry.get("regime_observed") != regime_observed:
            continue
        matches.append(entry)
        if len(matches) >= limit:
            break
    return list(reversed(matches))
