"""Tests for condor.trading_agent.adaptive.backtest_loop.

We mock the Hummingbot backtest client so the loop runs offline.
The fakes mirror the real shape: ``client.backtesting.run(...)`` is an
async method returning a dict.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import yaml

from condor.trading_agent.adaptive import backtest_loop as loop
from condor.trading_agent.adaptive.invariants import load_invariants


UTC = timezone.utc
REPO_ROOT = Path(__file__).resolve().parents[3]
INV_PATH = REPO_ROOT / "trading_agents" / "adaptive_pmm" / "invariants.yaml"


def _inv():
    return load_invariants(INV_PATH)


def _config(tp=0.0003, vol=10):
    return {
        "_config_name": "ctrl_x",
        "controller_name": "pmm_mister",
        "controller_type": "generic",
        "connector_name": "binance",
        "trading_pair": "BTC-USDT",
        "take_profit": tp,
        "target_base_pct": 0.5,
        "max_active_executors_by_level": vol,
    }


def _proposal(dimension="take_profit", direction="increase", magnitude="medium"):
    return {
        "controller_id": "bot1::ctrl_x",
        "dimension": dimension,
        "direction": direction,
        "magnitude_qualitative": magnitude,
        "reasoning": "test",
    }


class _FakeBacktesting:
    """In-memory ``client.backtesting`` stub.

    Pass ``results_by_tp`` to return a different dict depending on the
    ``take_profit`` value of the snapshot. Useful to simulate "better
    candidate at higher TP".
    """

    def __init__(self, results_by_tp: dict[float, dict] | None = None,
                 default: dict | None = None,
                 raise_for: dict | None = None,
                 sleep_seconds: float = 0.0):
        self.calls: list[dict] = []
        self._by_tp = results_by_tp or {}
        self._default = default or {"net_pnl_quote": 0.1, "total_volume": 100}
        self._raise_for = raise_for or {}
        self._sleep = sleep_seconds

    async def run(self, *, start_time, end_time, backtesting_resolution,
                  trade_cost, config):
        tp = config.get("take_profit")
        self.calls.append({"tp": tp, "start": start_time, "end": end_time})
        if self._sleep:
            await asyncio.sleep(self._sleep)
        if tp in self._raise_for:
            raise self._raise_for[tp]
        return self._by_tp.get(tp, self._default)


class _FakeClient:
    def __init__(self, backtesting: _FakeBacktesting):
        self.backtesting = backtesting


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# should_trigger_backtest_loop
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("paused,subopt,fav,cd,expected_ok,expected_reason", [
    (True,  True,  "adverse",    False, False, "agent_paused"),
    (False, False, "adverse",    False, False, "not_suboptimal"),
    (False, True,  "optimal",    False, False, "regime_optimal"),
    (False, True,  "suboptimal", True,  False, "cooldowns_active"),
    (False, True,  "suboptimal", False, True,  "ok"),
])
def test_should_trigger(paused, subopt, fav, cd, expected_ok, expected_reason):
    ok, reason = loop.should_trigger_backtest_loop(
        agent_paused=paused, suboptimal_now=subopt,
        favorability=fav, cooldown_active=cd,
    )
    assert ok is expected_ok
    assert reason == expected_reason


# ---------------------------------------------------------------------------
# select_backtest_window
# ---------------------------------------------------------------------------


def test_window_uses_suboptimal_period():
    now = datetime(2026, 5, 12, 12, tzinfo=UTC)
    w = loop.select_backtest_window(suboptimal_period_minutes=120.0, now=now)
    assert w["duration_minutes"] == 120
    assert w["_end_dt"] == now
    assert w["_start_dt"] == now - timedelta(minutes=120)


def test_window_floor_30min():
    now = datetime(2026, 5, 12, 12, tzinfo=UTC)
    w = loop.select_backtest_window(suboptimal_period_minutes=10.0, now=now)
    assert w["duration_minutes"] == 30


def test_window_floor_when_none():
    w = loop.select_backtest_window(
        suboptimal_period_minutes=None, now=datetime(2026, 5, 12, tzinfo=UTC),
    )
    assert w["duration_minutes"] == 30


# ---------------------------------------------------------------------------
# can_backtest
# ---------------------------------------------------------------------------


def test_can_backtest_supported_connector():
    assert loop.can_backtest(connector_name="binance", invariants=_inv()) is True


def test_can_backtest_unsupported_connector():
    assert loop.can_backtest(connector_name="hyperliquid", invariants=_inv()) is False


# ---------------------------------------------------------------------------
# Full cycle — happy path
# ---------------------------------------------------------------------------


def test_cycle_winner_found(tmp_path):
    """Score formula prioritizes volume; a winner needs MORE volume
    (and non-negative PnL) than the baseline."""
    bt = _FakeBacktesting(results_by_tp={
        0.0003:   {"net_pnl_quote": 0.10, "total_volume": 1000},   # baseline
        0.00045:  {"net_pnl_quote": 0.20, "total_volume": 2000},   # 2× vol → winner
        0.0006:   {"net_pnl_quote": 0.15, "total_volume": 1500},
    })
    client = _FakeClient(bt)

    out = _run(loop.run_backtest_cycle(
        client=client, agent_dir=tmp_path,
        proposal=_proposal(),
        current_config=_config(tp=0.0003),
        controller_id="bot1::ctrl_x",
        invariants=_inv(),
        now=datetime(2026, 5, 12, 12, tzinfo=UTC),
        suboptimal_period_minutes=60,
    ))
    assert out["verdict"] == "winner_found"
    assert out["selected"]["id"] == "c1"
    assert out["selected"]["value"] == 0.00045
    assert out["baseline"]["pnl"] == 0.10
    # 1 baseline + 2 candidates = 3 calls (all misses on a fresh dir)
    assert out["cache_stats"]["hits"] == 0
    assert out["cache_stats"]["misses"] == 3


def test_cycle_uses_cache_on_baseline_repeat(tmp_path):
    """Running the cycle twice with the same window keeps baseline cached."""
    bt = _FakeBacktesting(default={"net_pnl_quote": 0.1, "total_volume": 1000})
    client = _FakeClient(bt)

    args = dict(
        client=client, agent_dir=tmp_path,
        proposal=_proposal(),
        current_config=_config(tp=0.0003),
        controller_id="bot1::ctrl_x",
        invariants=_inv(),
        now=datetime(2026, 5, 12, 12, tzinfo=UTC),
        suboptimal_period_minutes=60,
    )
    _run(loop.run_backtest_cycle(**args))
    out2 = _run(loop.run_backtest_cycle(**args))
    # Every run hits the cache now.
    assert out2["cache_stats"]["hits"] == 3
    assert out2["cache_stats"]["misses"] == 0


# ---------------------------------------------------------------------------
# Full cycle — anti-evidence
# ---------------------------------------------------------------------------


def test_cycle_all_candidates_worse(tmp_path):
    bt = _FakeBacktesting(results_by_tp={
        0.0003:  {"net_pnl_quote": 0.50, "total_volume": 5000},
        0.00045: {"net_pnl_quote": 0.30, "total_volume": 2000},  # both worse
        0.0006:  {"net_pnl_quote": 0.10, "total_volume": 1000},
    })
    out = _run(loop.run_backtest_cycle(
        client=_FakeClient(bt), agent_dir=tmp_path,
        proposal=_proposal(), current_config=_config(),
        controller_id="bot1::ctrl_x", invariants=_inv(),
        now=datetime(2026, 5, 12, 12, tzinfo=UTC),
        suboptimal_period_minutes=60,
    ))
    assert out["verdict"] == "all_candidates_worse_than_baseline"
    assert out["selected"] is None


# ---------------------------------------------------------------------------
# Full cycle — all negative PnL
# ---------------------------------------------------------------------------


def test_cycle_all_negative_pnl(tmp_path):
    bt = _FakeBacktesting(results_by_tp={
        0.0003:  {"net_pnl_quote": 0.10, "total_volume": 1000},
        0.00045: {"net_pnl_quote": -0.5, "total_volume": 1000},
        0.0006:  {"net_pnl_quote": -0.7, "total_volume": 1000},
    })
    out = _run(loop.run_backtest_cycle(
        client=_FakeClient(bt), agent_dir=tmp_path,
        proposal=_proposal(), current_config=_config(),
        controller_id="bot1::ctrl_x", invariants=_inv(),
        now=datetime(2026, 5, 12, 12, tzinfo=UTC),
        suboptimal_period_minutes=60,
    ))
    assert out["verdict"] == "all_candidates_negative_pnl"


# ---------------------------------------------------------------------------
# Connector unsupported
# ---------------------------------------------------------------------------


def test_cycle_unsupported_connector(tmp_path):
    cfg = _config()
    cfg["connector_name"] = "hyperliquid"
    out = _run(loop.run_backtest_cycle(
        client=_FakeClient(_FakeBacktesting()),
        agent_dir=tmp_path,
        proposal=_proposal(),
        current_config=cfg,
        controller_id="bot1::ctrl_x",
        invariants=_inv(),
        now=datetime(2026, 5, 12, 12, tzinfo=UTC),
        suboptimal_period_minutes=60,
    ))
    assert out["verdict"] == "connector_unsupported"
    assert out["errors"][0]["type"] == "connector_unsupported"


# ---------------------------------------------------------------------------
# Baseline failure
# ---------------------------------------------------------------------------


def test_cycle_baseline_backtest_fails(tmp_path):
    bt = _FakeBacktesting(raise_for={
        0.0003: RuntimeError("data insufficient"),
    })
    out = _run(loop.run_backtest_cycle(
        client=_FakeClient(bt), agent_dir=tmp_path,
        proposal=_proposal(), current_config=_config(),
        controller_id="bot1::ctrl_x", invariants=_inv(),
        now=datetime(2026, 5, 12, 12, tzinfo=UTC),
        suboptimal_period_minutes=60,
    ))
    assert out["verdict"] == "baseline_backtest_failed"


def test_cycle_candidate_error_does_not_break(tmp_path):
    """Candidate-level error should mark THAT candidate as failed but
    the cycle should keep evaluating the rest."""
    bt = _FakeBacktesting(
        results_by_tp={
            0.0003:  {"net_pnl_quote": 0.10, "total_volume": 1000},
            0.0006:  {"net_pnl_quote": 0.15, "total_volume": 2000},  # winner: 2× vol
        },
        raise_for={0.00045: RuntimeError("c1 backtest failed")},
    )
    out = _run(loop.run_backtest_cycle(
        client=_FakeClient(bt), agent_dir=tmp_path,
        proposal=_proposal(), current_config=_config(),
        controller_id="bot1::ctrl_x", invariants=_inv(),
        now=datetime(2026, 5, 12, 12, tzinfo=UTC),
        suboptimal_period_minutes=60,
    ))
    # c2 won
    assert out["verdict"] == "winner_found"
    assert out["selected"]["id"] == "c2"
    # c1 error recorded
    assert any(e.get("candidate_id") == "c1" for e in out["errors"])


# ---------------------------------------------------------------------------
# Expansion collapses
# ---------------------------------------------------------------------------


def test_cycle_no_valid_candidates_for_at_limit_value(tmp_path):
    """target_base_pct at 1.0 + increase → everything domain-clamps to 1.0 → 0 candidates."""
    cfg = _config()
    cfg["target_base_pct"] = 1.0
    out = _run(loop.run_backtest_cycle(
        client=_FakeClient(_FakeBacktesting()),
        agent_dir=tmp_path,
        proposal=_proposal(dimension="target_base_pct"),
        current_config=cfg,
        controller_id="bot1::ctrl_x", invariants=_inv(),
        now=datetime(2026, 5, 12, 12, tzinfo=UTC),
        suboptimal_period_minutes=60,
    ))
    assert out["verdict"] == "no_valid_candidates"
    assert out["candidates"] == []


# ---------------------------------------------------------------------------
# Anti-evidence log
# ---------------------------------------------------------------------------


def test_write_anti_evidence_appends(tmp_path):
    (tmp_path / "state").mkdir()
    cycle = {
        "baseline": {"score_decomposition": {"score": 1.3}},
        "candidates": [
            {"id": "c1", "value": 0.00045, "score": 1.0},
            {"id": "c2", "value": 0.0006,  "score": 0.8},
        ],
        "verdict": "all_candidates_worse_than_baseline",
    }
    loop.write_anti_evidence(
        agent_dir=tmp_path,
        tick_id=42,
        controller_id="bot1::ctrl_x",
        regime_observed="mean_reverting_high_vol",
        suboptimal_minutes=47,
        proposal=_proposal(),
        cycle_result=cycle,
        now=datetime(2026, 5, 12, 12, tzinfo=UTC),
    )
    log_path = tmp_path / "state" / "anti_evidence_log.jsonl"
    assert log_path.exists()
    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 1


def test_recent_anti_evidence_filters_by_controller_and_regime(tmp_path):
    (tmp_path / "state").mkdir()
    # 4 entries across 2 controllers and 2 regimes
    cycle = {
        "baseline": {"score_decomposition": {"score": 1.3}},
        "candidates": [{"id": "c1", "value": 0, "score": 1.0}],
        "verdict": "all_candidates_worse_than_baseline",
    }
    for cid, regime in [
        ("b1::c1", "mean_reverting_high_vol"),
        ("b1::c1", "trending_up_mod_vol"),
        ("b1::c1", "mean_reverting_high_vol"),
        ("b1::c2", "mean_reverting_high_vol"),
    ]:
        loop.write_anti_evidence(
            agent_dir=tmp_path, tick_id=1,
            controller_id=cid, regime_observed=regime,
            suboptimal_minutes=30, proposal=_proposal(),
            cycle_result=cycle,
            now=datetime(2026, 5, 12, 12, tzinfo=UTC),
        )
    matches = loop.recent_anti_evidence_for(
        agent_dir=tmp_path,
        controller_id="b1::c1",
        regime_observed="mean_reverting_high_vol",
        limit=10,
    )
    assert len(matches) == 2
    assert all(m["controller_id"] == "b1::c1" for m in matches)
    assert all(m["regime_observed"] == "mean_reverting_high_vol" for m in matches)
