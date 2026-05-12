"""Tests for condor.trading_agent.adaptive.orchestrator.

The orchestrator is the top of the adaptive framework — every dependency
is injected (LLM, MCP apply, send_proposal, backtest client). Tests
exercise:
- pre-flight (agent paused)
- LLM prompt assembly
- LLM response parsing (happy path + error variants)
- proposal lifecycle in each mode (propose / shadow / auto)
- invariants pre-check rejection
- backtest verdict propagation (winner_found / all_worse / etc.)
- audit log + last_changes side effects per mode
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import yaml

from condor.trading_agent.adaptive import orchestrator as orch
from condor.trading_agent.adaptive import audit as audit_io
from condor.trading_agent.adaptive.invariants import load_invariants


UTC = timezone.utc
REPO_ROOT = Path(__file__).resolve().parents[3]
INV_PATH = REPO_ROOT / "trading_agents" / "adaptive_pmm" / "invariants.yaml"


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _agent_dir(tmp_path: Path) -> Path:
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _snapshots(controller_id: str = "bot1::c1") -> orch.RoutineSnapshots:
    return orch.RoutineSnapshots(
        capital_state={
            "totals": {"controllers": 1, "headroom_usd": 1000},
            "oversub_alerts": [],
        },
        market_regime={
            "pairs": ["BTC-USDT"],
            "by_pair": {"BTC-USDT": {"regime": "mean_reverting_high_vol",
                                     "favorability": "suboptimal"}},
            "counts": {"optimal": 0, "suboptimal": 1, "adverse": 0},
        },
        controller_performance={
            "controllers": [controller_id],
            "counts": {"suboptimal_now": 1, "stuck": 0, "cold_start": 0, "pnl_flat": 0},
        },
        market_regime_by_pair={
            "BTC-USDT": {"regime": "mean_reverting_high_vol",
                         "favorability": "suboptimal", "confidence": "high"},
        },
        controller_performance_by_ctrl={
            controller_id: {
                "diagnostic": {"suboptimal_now": True, "suboptimal_period_minutes": 60},
            },
        },
    )


def _ctx(
    tmp_path: Path, *,
    mode: str = "propose",
    llm_response: str = '{"action": "no_action", "reason": "all good"}',
    proposals_have_config: bool = True,
    bt_results_by_tp: dict | None = None,
) -> orch.TickContext:
    agent_dir = _agent_dir(tmp_path)
    inv = load_invariants(INV_PATH)
    snap = _snapshots()
    async def llm_call(prompt: str) -> str:
        return llm_response

    return orch.TickContext(
        agent_dir=agent_dir,
        invariants=inv,
        snapshots=snap,
        llm_call=llm_call,
        mode=mode,
        tick_id=1,
        now=datetime(2026, 5, 12, 12, tzinfo=UTC),
        propose_chat_id=-1001,
        send_proposal_fn=None,
        bot_obj=None,
        mcp_apply_fn=None,
    )


_AGENT_MD = "# Adaptive supervisor\n\nReply with no_action or propose."


# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------


def test_pause_returns_skipped(tmp_path):
    ad = _agent_dir(tmp_path)
    (ad / "state" / "agent_status.json").write_text(
        json.dumps({"status": "paused", "reason": "manual"})
    )
    ctx = _ctx(tmp_path)
    result = _run(orch.run_tick(ctx, agent_md_body=_AGENT_MD))
    assert result.skipped is True
    assert result.skipped_reason == "agent_paused"


def test_active_proceeds_to_llm(tmp_path):
    ctx = _ctx(tmp_path)
    result = _run(orch.run_tick(ctx, agent_md_body=_AGENT_MD))
    assert result.skipped is False
    assert result.llm_action == "no_action"


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------


def test_prompt_contains_agent_md_and_three_snapshots():
    snap = _snapshots()
    prompt = orch.build_llm_prompt(
        agent_md_body=_AGENT_MD, snapshots=snap, tick_id=42, mode="propose",
    )
    assert "Adaptive supervisor" in prompt
    assert "tick_id: 42" in prompt
    assert "mode: propose" in prompt
    assert "capital_state" in prompt
    assert "market_regime" in prompt
    assert "controller_performance" in prompt
    # Per-pair / per-controller blocks present when non-empty
    assert "BTC-USDT" in prompt
    assert "bot1::c1" in prompt


# ---------------------------------------------------------------------------
# LLM response parsing
# ---------------------------------------------------------------------------


def test_parse_bare_json_no_action():
    p, e = orch.parse_llm_response('{"action": "no_action", "reason": "x"}')
    assert e is None
    assert p["action"] == "no_action"


def test_parse_with_code_fence():
    raw = '```json\n{"action": "no_action", "reason": "x"}\n```'
    p, e = orch.parse_llm_response(raw)
    assert e is None
    assert p["action"] == "no_action"


def test_parse_propose_requires_proposals_list():
    p, e = orch.parse_llm_response('{"action": "propose"}')
    assert p is None
    assert "proposals must be a list" in e


def test_parse_propose_validates_fields():
    raw = json.dumps({
        "action": "propose",
        "proposals": [{"controller_id": "x", "dimension": "tp"}]
    })
    p, e = orch.parse_llm_response(raw)
    assert p is None
    assert "direction" in e


def test_parse_propose_validates_magnitude_enum():
    raw = json.dumps({
        "action": "propose",
        "proposals": [{
            "controller_id": "x", "dimension": "tp",
            "direction": "increase", "magnitude_qualitative": "huge",
        }]
    })
    p, e = orch.parse_llm_response(raw)
    assert p is None
    assert "magnitude_qualitative" in e


def test_parse_unknown_action():
    p, e = orch.parse_llm_response('{"action": "destroy"}')
    assert p is None
    assert "invalid action" in e


def test_parse_invalid_json():
    p, e = orch.parse_llm_response("not json")
    assert p is None
    assert "json decode" in e


# ---------------------------------------------------------------------------
# Invariants pre-check
# ---------------------------------------------------------------------------


def test_invariants_pre_check_blocks_forbidden():
    inv = load_invariants(INV_PATH)
    ok, reason = orch.validate_proposal_against_invariants(
        {"dimension": "manual_kill_switch"}, inv,
    )
    assert ok is False
    assert "field_forbidden" in reason


def test_invariants_pre_check_blocks_unknown_field():
    inv = load_invariants(INV_PATH)
    ok, reason = orch.validate_proposal_against_invariants(
        {"dimension": "non_existent"}, inv,
    )
    assert ok is False


def test_invariants_pre_check_allows_take_profit():
    inv = load_invariants(INV_PATH)
    ok, reason = orch.validate_proposal_against_invariants(
        {"dimension": "take_profit"}, inv,
    )
    assert ok is True


# ---------------------------------------------------------------------------
# Auto mode strict checks
# ---------------------------------------------------------------------------


def test_auto_mode_requires_winner():
    inv = load_invariants(INV_PATH)
    ok, reason = orch.auto_mode_checks_pass(
        inv, {"verdict": "no_valid_candidates"},
    )
    assert ok is False
    assert "winner" not in (reason or "")
    assert "evidence" in reason


def test_auto_mode_accepts_winner():
    inv = load_invariants(INV_PATH)
    ok, _ = orch.auto_mode_checks_pass(inv, {"verdict": "winner_found"})
    assert ok is True


# ---------------------------------------------------------------------------
# Full tick — propose mode
# ---------------------------------------------------------------------------


class _FakeBacktesting:
    def __init__(self, results_by_tp: dict[float, dict]):
        self._r = results_by_tp

    async def run(self, *, start_time, end_time, backtesting_resolution,
                  trade_cost, config):
        return self._r.get(config.get("take_profit"),
                           {"net_pnl_quote": 0.1, "total_volume": 1000})


class _FakeClient:
    def __init__(self, bt):
        self.backtesting = bt


def _propose_response(tp_dim="take_profit", direction="increase", mag="medium"):
    return json.dumps({
        "action": "propose",
        "proposals": [{
            "controller_id": "bot1::c1",
            "dimension": tp_dim,
            "direction": direction,
            "magnitude_qualitative": mag,
            "reasoning": "test",
            "caveats": [],
        }]
    })


def _inject_controller_context(proposal: dict, current_config: dict, client: Any):
    """Caller-side annotation that the orchestrator expects."""
    proposal["_current_config"] = current_config
    proposal["_client_ref"] = client


def test_tick_propose_winner_sends_telegram(tmp_path):
    """Happy path in propose mode: LLM proposes, backtest finds winner,
    send_proposal is invoked."""
    bt = _FakeBacktesting({
        0.0003:  {"net_pnl_quote": 0.10, "total_volume": 1000},
        0.00045: {"net_pnl_quote": 0.15, "total_volume": 2000},
        0.0006:  {"net_pnl_quote": 0.20, "total_volume": 1800},
    })
    client = _FakeClient(bt)

    # Wrap the LLM response so we can inject context AFTER parsing.
    # The orchestrator's contract is: the caller sets _current_config /
    # _client_ref on each proposal. To do that without owning the
    # response, we monkeypatch parse_llm_response.
    response = _propose_response()

    ctx = _ctx(tmp_path, mode="propose", llm_response=response)

    captured = {}
    async def fake_send_proposal(bot, agent_dir, entry, *, chat_id):
        captured["entry"] = entry
        captured["chat_id"] = chat_id
        return {**entry, "telegram_message_id": 9999}

    ctx.send_proposal_fn = fake_send_proposal
    ctx.bot_obj = object()  # stand-in
    # Hook backtest client + config injection via monkeypatching the
    # orchestrator's _process_proposal: a quick wrapper that mutates
    # the parsed proposals.
    orig_process = orch._process_proposal

    async def wrapper(c, p, *, counter):
        _inject_controller_context(p, {"take_profit": 0.0003,
                                       "connector_name": "binance",
                                       "trading_pair": "BTC-USDT"}, client)
        return await orig_process(c, p, counter=counter)

    import condor.trading_agent.adaptive.orchestrator as orch_mod
    orch_mod._process_proposal = wrapper
    try:
        result = _run(orch.run_tick(ctx, agent_md_body=_AGENT_MD))
    finally:
        orch_mod._process_proposal = orig_process

    assert result.llm_action == "propose"
    assert result.proposals_total == 1
    assert result.proposals_processed[0]["verdict"] == "proposed"
    assert captured["chat_id"] == -1001
    # Audit entry was created + send_proposal got it
    assert "patch_id" in captured["entry"]


def test_tick_propose_invariants_rejected(tmp_path):
    """LLM proposes a forbidden field → invariants pre-check kills it,
    no backtest runs."""
    response = json.dumps({
        "action": "propose",
        "proposals": [{
            "controller_id": "bot1::c1",
            "dimension": "manual_kill_switch",
            "direction": "increase",
            "magnitude_qualitative": "small",
            "reasoning": "let's nuke",
        }]
    })
    ctx = _ctx(tmp_path, mode="propose", llm_response=response)
    result = _run(orch.run_tick(ctx, agent_md_body=_AGENT_MD))
    assert result.proposals_processed[0]["verdict"] == "invariants_rejected"
    # Audit was still written for traceability
    entries = audit_io.find_audit_entry(ctx.agent_dir,
        result.proposals_processed[0].get("patch_id") or "")  # patch_id is None here
    # patch_id is None → we just look at the log directly
    log_path = audit_io.audit_log_path(ctx.agent_dir)
    assert log_path.exists()


# ---------------------------------------------------------------------------
# Shadow mode
# ---------------------------------------------------------------------------


def test_tick_shadow_logs_no_send(tmp_path):
    bt = _FakeBacktesting({
        0.0003:  {"net_pnl_quote": 0.10, "total_volume": 1000},
        0.00045: {"net_pnl_quote": 0.15, "total_volume": 2000},
        0.0006:  {"net_pnl_quote": 0.20, "total_volume": 1800},
    })
    client = _FakeClient(bt)

    ctx = _ctx(tmp_path, mode="shadow", llm_response=_propose_response())

    sent = AsyncMock()
    ctx.send_proposal_fn = sent

    orig_process = orch._process_proposal
    async def wrapper(c, p, *, counter):
        _inject_controller_context(p, {"take_profit": 0.0003,
                                       "connector_name": "binance",
                                       "trading_pair": "BTC-USDT"}, client)
        return await orig_process(c, p, counter=counter)
    import condor.trading_agent.adaptive.orchestrator as orch_mod
    orch_mod._process_proposal = wrapper
    try:
        result = _run(orch.run_tick(ctx, agent_md_body=_AGENT_MD))
    finally:
        orch_mod._process_proposal = orig_process

    assert result.proposals_processed[0]["verdict"] == "shadow_logged"
    sent.assert_not_called()
    # Audit entry got marked shadow_logged
    entry = audit_io.find_audit_entry(
        ctx.agent_dir, result.proposals_processed[0]["patch_id"]
    )
    assert entry["human_verdict"] == "shadow_logged"


# ---------------------------------------------------------------------------
# Auto mode
# ---------------------------------------------------------------------------


def test_tick_auto_applies_via_mcp(tmp_path):
    bt = _FakeBacktesting({
        0.0003:  {"net_pnl_quote": 0.10, "total_volume": 1000},
        0.00045: {"net_pnl_quote": 0.15, "total_volume": 2000},
        0.0006:  {"net_pnl_quote": 0.20, "total_volume": 1800},
    })
    client = _FakeClient(bt)

    ctx = _ctx(tmp_path, mode="auto", llm_response=_propose_response())

    apply_calls = []
    async def fake_apply(**kw):
        apply_calls.append(kw)
        return {
            "success": True, "old_value": 0.0003,
            "new_value": kw["value"], "caveats": [],
            "applied_at": "2026-05-12T12:00:01Z",
            "raw_api_result": {"message": "ok"},
        }
    ctx.mcp_apply_fn = fake_apply
    ctx.snapshots.controller_performance_by_ctrl["bot1::c1"][
        "diagnostic"]["suboptimal_period_minutes"] = 60

    orig_process = orch._process_proposal
    async def wrapper(c, p, *, counter):
        _inject_controller_context(p, {"take_profit": 0.0003,
                                       "connector_name": "binance",
                                       "trading_pair": "BTC-USDT"}, client)
        p["bot_name"] = "bot1"
        return await orig_process(c, p, counter=counter)
    import condor.trading_agent.adaptive.orchestrator as orch_mod
    orch_mod._process_proposal = wrapper
    try:
        result = _run(orch.run_tick(ctx, agent_md_body=_AGENT_MD))
    finally:
        orch_mod._process_proposal = orig_process

    assert result.proposals_processed[0]["verdict"] == "auto_applied"
    assert len(apply_calls) == 1
    # Audit entry shows applied=True + verdict=auto_applied
    entry = audit_io.find_audit_entry(
        ctx.agent_dir, result.proposals_processed[0]["patch_id"]
    )
    assert entry["applied"] is True
    assert entry["human_verdict"] == "auto_applied"
    # last_changes recorded
    lc = audit_io.get_last_change(ctx.agent_dir, "bot1::c1", "take_profit")
    assert lc["marker"] == "applied"


def test_tick_auto_blocks_when_no_winner(tmp_path):
    """All candidates worse than baseline → auto blocks because there's
    no backtest evidence to apply."""
    bt = _FakeBacktesting({
        0.0003:  {"net_pnl_quote": 0.50, "total_volume": 5000},
        0.00045: {"net_pnl_quote": 0.10, "total_volume": 1000},
        0.0006:  {"net_pnl_quote": 0.10, "total_volume": 800},
    })
    client = _FakeClient(bt)

    ctx = _ctx(tmp_path, mode="auto", llm_response=_propose_response())
    ctx.mcp_apply_fn = AsyncMock()
    ctx.snapshots.controller_performance_by_ctrl["bot1::c1"][
        "diagnostic"]["suboptimal_period_minutes"] = 60

    orig_process = orch._process_proposal
    async def wrapper(c, p, *, counter):
        _inject_controller_context(p, {"take_profit": 0.0003,
                                       "connector_name": "binance",
                                       "trading_pair": "BTC-USDT"}, client)
        p["bot_name"] = "bot1"
        return await orig_process(c, p, counter=counter)
    import condor.trading_agent.adaptive.orchestrator as orch_mod
    orch_mod._process_proposal = wrapper
    try:
        result = _run(orch.run_tick(ctx, agent_md_body=_AGENT_MD))
    finally:
        orch_mod._process_proposal = orig_process

    verdict = result.proposals_processed[0]["verdict"]
    assert verdict in ("all_candidates_worse_than_baseline",
                       "auto_blocked")
    ctx.mcp_apply_fn.assert_not_called()


# ---------------------------------------------------------------------------
# No-op path
# ---------------------------------------------------------------------------


def test_tick_no_action_logged(tmp_path):
    ctx = _ctx(tmp_path)
    result = _run(orch.run_tick(ctx, agent_md_body=_AGENT_MD))
    assert result.llm_action == "no_action"
    assert result.proposals_total == 0


# ---------------------------------------------------------------------------
# Parse error path
# ---------------------------------------------------------------------------


def test_tick_parse_error_recorded(tmp_path):
    ctx = _ctx(tmp_path, llm_response="not json at all")
    result = _run(orch.run_tick(ctx, agent_md_body=_AGENT_MD))
    assert result.llm_action == "parse_error"
    assert any(e["stage"] == "parse_llm" for e in result.errors)


def test_tick_llm_call_failure(tmp_path):
    ctx = _ctx(tmp_path)
    async def boom(prompt):
        raise RuntimeError("LLM is down")
    ctx.llm_call = boom
    result = _run(orch.run_tick(ctx, agent_md_body=_AGENT_MD))
    assert result.llm_action == "llm_error"
    assert any(e["stage"] == "llm_call" for e in result.errors)


# ---------------------------------------------------------------------------
# build_audit_entry
# ---------------------------------------------------------------------------


def test_build_audit_entry_has_canonical_keys():
    cycle = {
        "verdict": "winner_found",
        "old_value": 0.0003,
        "selected": {"id": "c1", "value": 0.00045},
        "baseline": {"pnl": 0.1, "vol": 1000,
                     "score_decomposition": {"score": 1.3},
                     "source": "cache_miss"},
        "candidates": [{"id": "c1", "value": 0.00045,
                        "pnl": 0.15, "vol": 2000, "score": 1.5,
                        "source": "cache_miss"}],
        "errors": [],
    }
    proposal = {
        "controller_id": "bot1::c1",
        "dimension": "take_profit",
        "direction": "increase",
        "magnitude_qualitative": "medium",
        "reasoning": "test",
    }
    entry = orch.build_audit_entry(
        tick_id=1,
        now=datetime(2026, 5, 12, 12, tzinfo=UTC),
        proposal=proposal,
        cycle_output=cycle,
        mode="propose",
        invariants_ok=True,
        invariants_reason=None,
        counter=1,
    )
    assert entry["patch_id"].startswith("p_")
    assert entry["tick_id"] == 1
    assert entry["controller_id"] == "bot1::c1"
    assert entry["llm_proposal"]["dimension"] == "take_profit"
    assert entry["selected"] == "c1"
    assert entry["selected_value"] == 0.00045
    assert entry["old_value"] == 0.0003
    assert entry["mode"] == "propose"
    assert entry["human_verdict"] is None
    assert entry["applied"] is False
    assert entry["backtests"]["baseline"]["net_pnl_quote"] == 0.1
    assert entry["backtests"]["candidate1"]["score"] == 1.5
