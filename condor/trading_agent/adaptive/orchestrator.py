"""Adaptive Strategy Framework — agent tick orchestrator (Fase 5.7).

End-to-end pipeline that runs once per tick:

1. **Pre-flight**: agent paused? clean up old pending proposals? daily
   cache cleanup due?
2. **Read routines**: capital_state, market_regime (multi-pair),
   controller_performance (multi-controller). Extract the per-entity
   ``agent:*`` views via the routine runners.
3. **Build prompt context**: combine `agent.md` body + the three
   aggregated agent payloads. Send to the LLM via the injected
   ``llm_call`` callable.
4. **Parse + validate** the LLM response (JSON: ``no_action`` or
   ``propose``).
5. **Per proposal**: trigger check → backtest loop → mode-dependent
   action:
   - ``propose``: send Telegram message + audit entry (pending).
   - ``shadow``:  log audit entry, no Telegram, no apply.
   - ``auto``:    stricter invariants, then apply via MCP tool + audit.
6. **Return** a tick summary the caller can log or expose to the UI.

LLM client is INJECTED (``llm_call: async (prompt) -> str``) so this
module is testable without spinning up claude-code or pydantic-ai.
The integration layer that wires the real client lives in
``condor/trading_agent/adaptive/llm_runner.py`` (separate file, also
in this commit).

Persistence layout (under ``trading_agents/<agent>/state/``):
- ``audit_log.jsonl``        — appended for every tick + every proposal
- ``last_changes.json``      — cooldowns
- ``anti_evidence_log.jsonl`` — when all candidates worse than baseline
- ``backtest_cache/``        — per-(ctrl, config, window) results
- ``.last_cache_cleanup``    — marker for daily cleanup
- ``agent_status.json``      — paused/active (read by pre-flight)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from condor.trading_agent.adaptive import (
    audit as audit_io,
    backtest_cache,
    backtest_loop,
)
from condor.trading_agent.adaptive import state_io
from condor.trading_agent.adaptive.invariants import (
    Invariants,
    load_invariants,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


LLMCall = Callable[[str], Awaitable[str]]
"""Async callable that takes a prompt string and returns the LLM's
JSON-formatted response. The orchestrator does NOT enforce JSON
correctness via the API — it parses and reports parse failures."""


SendProposalFn = Callable[..., Awaitable[dict[str, Any]]]
"""Same signature as ``handlers.adaptive.send_proposal``. Injected
to keep the orchestrator decoupled from Telegram. Pass ``None`` in
shadow/auto mode (it's never called)."""


@dataclass
class RoutineSnapshots:
    """The three agent-facing payloads ready to feed into the LLM prompt.

    Built per tick by the orchestrator. Each payload is the
    ``agent:summary`` section of its routine.
    """
    capital_state: dict[str, Any]
    market_regime: dict[str, Any]
    controller_performance: dict[str, Any]
    # Per-entity payloads, keyed by entity id (canonical_id or pair):
    market_regime_by_pair: dict[str, dict[str, Any]] = field(default_factory=dict)
    controller_performance_by_ctrl: dict[str, dict[str, Any]] = field(default_factory=dict)
    capital_state_payload_full: dict[str, Any] | None = None


@dataclass
class TickContext:
    """Inputs the orchestrator needs from outside per tick."""
    agent_dir: Path
    invariants: Invariants
    snapshots: RoutineSnapshots
    llm_call: LLMCall
    mode: str                        # "propose" | "shadow" | "auto"
    tick_id: int
    now: datetime
    propose_chat_id: int | None = None
    send_proposal_fn: SendProposalFn | None = None
    bot_obj: Any | None = None       # Telegram bot for send_proposal
    mcp_apply_fn: Callable[..., Awaitable[dict[str, Any]]] | None = None  # for auto mode


@dataclass
class TickResult:
    """What the orchestrator returns. Caller logs / displays it."""
    tick_id: int
    ts: str
    mode: str
    skipped: bool
    skipped_reason: str | None = None
    llm_action: str | None = None           # "no_action" | "propose" | "parse_error"
    llm_reason: str | None = None
    proposals_total: int = 0
    proposals_processed: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------


_AGENT_STATUS_FILE = "agent_status.json"


def _read_agent_status(agent_dir: Path) -> dict[str, Any]:
    """Return ``{"status": "active"|"paused", "reason": ..., "since": ...}``.
    Defaults to active when the file is missing."""
    path = agent_dir / "state" / _AGENT_STATUS_FILE
    if not path.exists():
        return {"status": "active"}
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"status": "active"}


def is_agent_paused(agent_dir: Path) -> bool:
    return _read_agent_status(agent_dir).get("status") == "paused"


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def build_llm_prompt(
    *,
    agent_md_body: str,
    snapshots: RoutineSnapshots,
    tick_id: int,
    mode: str,
) -> str:
    """Assemble the prompt the LLM sees this tick.

    Structure:
    1. ``agent.md`` body (scope / routines / policy / mode / output).
    2. Tick metadata (tick_id, mode, now).
    3. Routine snapshots as compact JSON.

    The LLM is expected to return EXACTLY ONE JSON object (no markdown
    fence, no commentary), shape per the contract in agent.md.
    """
    parts: list[str] = [
        "# Adaptive Supervisor — tick instructions",
        "",
        agent_md_body.strip(),
        "",
        "---",
        "",
        f"# Tick context",
        "",
        f"- tick_id: {tick_id}",
        f"- mode: {mode}",
        "",
        "## capital_state (agent view)",
        "",
        "```json",
        json.dumps(snapshots.capital_state, indent=2, default=str),
        "```",
        "",
        "## market_regime (agent:summary — all pairs)",
        "",
        "```json",
        json.dumps(snapshots.market_regime, indent=2, default=str),
        "```",
        "",
        "## controller_performance (agent:summary — all controllers)",
        "",
        "```json",
        json.dumps(snapshots.controller_performance, indent=2, default=str),
        "```",
    ]

    # Per-entity views — only emit when present (the LLM may want to
    # zoom into specific controllers/pairs).
    if snapshots.market_regime_by_pair:
        parts.append("\n## market_regime per pair\n")
        for pair, payload in snapshots.market_regime_by_pair.items():
            parts.append(f"### {pair}\n\n```json")
            parts.append(json.dumps(payload, indent=2, default=str))
            parts.append("```")
    if snapshots.controller_performance_by_ctrl:
        parts.append("\n## controller_performance per controller\n")
        for cid, payload in snapshots.controller_performance_by_ctrl.items():
            parts.append(f"### {cid}\n\n```json")
            parts.append(json.dumps(payload, indent=2, default=str))
            parts.append("```")

    parts.append("\n---\n\n**Return exactly one JSON object** matching"
                 " the contract above (`no_action` or `propose`).")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# LLM response parsing
# ---------------------------------------------------------------------------


def parse_llm_response(raw: str) -> tuple[dict[str, Any] | None, str | None]:
    """Strip optional code fences and parse. Returns (parsed, error).

    Accepts:
    - bare JSON
    - JSON inside ``` fences
    - JSON inside ```json fences

    Anything else returns ``(None, error_message)``.
    """
    if not isinstance(raw, str):
        return None, f"expected str, got {type(raw).__name__}"

    text = raw.strip()
    if text.startswith("```"):
        # Strip the opening fence and language tag if any
        text = text[3:]
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
        if text.endswith("```"):
            text = text[:-3].strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        return None, f"json decode error: {e}"

    if not isinstance(parsed, dict):
        return None, f"expected JSON object at top level, got {type(parsed).__name__}"

    action = parsed.get("action")
    if action not in ("no_action", "propose"):
        return None, f"invalid action: {action!r}"

    if action == "propose":
        proposals = parsed.get("proposals")
        if not isinstance(proposals, list):
            return None, "propose: proposals must be a list"
        for i, p in enumerate(proposals):
            for k in ("controller_id", "dimension", "direction", "magnitude_qualitative"):
                if k not in p:
                    return None, f"propose: proposals[{i}] missing {k}"
            if p["direction"] not in ("increase", "decrease"):
                return None, f"propose: proposals[{i}].direction invalid: {p['direction']!r}"
            if p["magnitude_qualitative"] not in ("small", "medium", "large"):
                return None, f"propose: proposals[{i}].magnitude_qualitative invalid: {p['magnitude_qualitative']!r}"

    return parsed, None


# ---------------------------------------------------------------------------
# Per-proposal pre-validation against invariants
# ---------------------------------------------------------------------------


def validate_proposal_against_invariants(
    proposal: dict[str, Any], invariants: Invariants,
) -> tuple[bool, str | None]:
    """Single-field policy checks the agent loop runs BEFORE the
    expander. Returns ``(ok, reason)``."""
    field_name = proposal["dimension"]
    if invariants.is_field_forbidden(field_name):
        return False, f"field_forbidden:{field_name}"
    if not invariants.is_field_allowed(field_name):
        return False, f"field_not_allowed:{field_name}"
    return True, None


# ---------------------------------------------------------------------------
# Auto-mode strict checks
# ---------------------------------------------------------------------------


def auto_mode_checks_pass(
    invariants: Invariants, cycle_output: dict[str, Any],
) -> tuple[bool, str | None]:
    """Auto mode (D4): require backtest evidence and respect the
    delta_multiplier on top of regular invariants.

    The delta_multiplier is already considered by the expander's
    clamping (max_increase/max_decrease ARE the invariant). Auto-only
    adds:
    - require_backtest_evidence (no fallback to ``allow_with_flag``)
    """
    auto = invariants.auto_mode or {}
    if auto.get("require_backtest_evidence", True) and cycle_output["verdict"] != "winner_found":
        return False, f"auto_requires_backtest_evidence (verdict={cycle_output['verdict']})"
    return True, None


# ---------------------------------------------------------------------------
# Audit entry construction
# ---------------------------------------------------------------------------


def build_audit_entry(
    *,
    tick_id: int,
    now: datetime,
    proposal: dict[str, Any],
    cycle_output: dict[str, Any],
    mode: str,
    invariants_ok: bool,
    invariants_reason: str | None,
    counter: int = 1,
) -> dict[str, Any]:
    """Build the canonical audit entry per MEMORY_SPEC §F6.

    Fills the fields available at proposal-creation time. The
    apply/verdict/outcome fields stay null/false until later steps
    update them in-place.
    """
    patch_id = audit_io.make_patch_id(now, counter)
    selected = cycle_output.get("selected") or {}
    return {
        "patch_id": patch_id,
        "tick_id": tick_id,
        "ts": state_io.utc_iso(now),
        "controller_id": proposal["controller_id"],
        "bot_name": proposal.get("bot_name"),
        "config_name": proposal.get("config_name"),
        "trading_pair": proposal.get("trading_pair"),
        "connector_name": proposal.get("connector_name"),
        "regime_observed": proposal.get("regime_observed"),
        "suboptimal_minutes": proposal.get("suboptimal_minutes"),
        "warmup_status": proposal.get("warmup_status"),
        "llm_proposal": {
            "dimension": proposal["dimension"],
            "direction": proposal["direction"],
            "magnitude_qualitative": proposal["magnitude_qualitative"],
            "reasoning": proposal.get("reasoning", ""),
            "caveats": list(proposal.get("caveats") or []),
        },
        "expanded_candidates": cycle_output.get("candidates", []),
        "backtests": _backtests_summary(cycle_output),
        "selected": selected.get("id"),
        "selected_value": selected.get("value"),
        "old_value": cycle_output.get("old_value"),
        "invariants_check": {
            "ok": invariants_ok,
            "reason": invariants_reason,
        },
        "mode": mode,
        "telegram_chat_id": None,
        "telegram_message_id": None,
        "human_verdict": None,
        "verdict_at": None,
        "verdict_chose_candidate": None,
        "verdict_by": None,
        "snoozed_until_ts": None,
        "pre_apply_snapshot": None,
        "applied": False,
        "applied_at": None,
        "apply_result": None,
        "apply_error": None,
        "outcome_30min": None,
        "cycle_verdict": cycle_output.get("verdict"),
        "cycle_errors": cycle_output.get("errors", []),
    }


def _backtests_summary(cycle: dict[str, Any]) -> dict[str, Any]:
    """Compact backtest map for the audit log: baseline + candidates."""
    out: dict[str, Any] = {}
    baseline = cycle.get("baseline") or {}
    if baseline:
        out["baseline"] = {
            "net_pnl_quote": baseline.get("pnl"),
            "volume": baseline.get("vol"),
            "score": (baseline.get("score_decomposition") or {}).get("score"),
            "source": baseline.get("source"),
        }
    for i, c in enumerate(cycle.get("candidates", []), start=1):
        out[f"candidate{i}"] = {
            "id": c.get("id"),
            "value": c.get("value"),
            "net_pnl_quote": c.get("pnl"),
            "volume": c.get("vol"),
            "score": c.get("score"),
            "source": c.get("source"),
        }
    return out


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def run_tick(ctx: TickContext, *, agent_md_body: str) -> TickResult:
    """Run one tick. Returns the structured result.

    Never raises — every error path becomes an entry in
    ``result.errors`` so the caller can log uniformly.
    """
    result = TickResult(
        tick_id=ctx.tick_id,
        ts=state_io.utc_iso(ctx.now),
        mode=ctx.mode,
        skipped=False,
    )

    # 1. Pre-flight: agent paused
    if is_agent_paused(ctx.agent_dir):
        result.skipped = True
        result.skipped_reason = "agent_paused"
        return result

    # 2. Background tasks (expire pending + cache cleanup)
    try:
        expired = audit_io.expire_old_pending_proposals(ctx.agent_dir, now=ctx.now)
        if expired:
            logger.info("orchestrator: expired %d pending proposals", len(expired))
        if backtest_cache.should_run_daily_cleanup(ctx.agent_dir, now=ctx.now):
            cutoff = ctx.now - timedelta(days=30)
            removed = backtest_cache.purge_older_than(ctx.agent_dir, cutoff)
            backtest_cache.stamp_daily_cleanup(ctx.agent_dir)
            logger.info("orchestrator: cache cleanup removed %d entries", removed)
    except Exception as e:
        result.errors.append({"stage": "background", "error": str(e)})

    # 3. LLM call
    prompt = build_llm_prompt(
        agent_md_body=agent_md_body, snapshots=ctx.snapshots,
        tick_id=ctx.tick_id, mode=ctx.mode,
    )
    try:
        llm_raw = await ctx.llm_call(prompt)
    except Exception as e:
        result.errors.append({"stage": "llm_call", "error": str(e)})
        result.llm_action = "llm_error"
        return result

    parsed, parse_err = parse_llm_response(llm_raw)
    if parsed is None:
        result.llm_action = "parse_error"
        result.errors.append({"stage": "parse_llm", "error": parse_err})
        return result

    result.llm_action = parsed["action"]
    if parsed["action"] == "no_action":
        result.llm_reason = parsed.get("reason", "")
        return result

    proposals = parsed["proposals"]
    result.proposals_total = len(proposals)

    # 4. Process each proposal
    for i, proposal in enumerate(proposals, start=1):
        try:
            entry_result = await _process_proposal(
                ctx, proposal, counter=i,
            )
        except Exception as e:
            logger.exception("orchestrator: proposal %d raised", i)
            entry_result = {
                "controller_id": proposal.get("controller_id"),
                "verdict": "exception",
                "error": str(e),
                "patch_id": None,
            }
        result.proposals_processed.append(entry_result)

    return result


async def _process_proposal(
    ctx: TickContext, proposal: dict[str, Any], *, counter: int,
) -> dict[str, Any]:
    """Validate + backtest + mode-dependent action for ONE proposal.

    Always writes an audit entry. Returns a small summary for the
    tick result.
    """
    # 1. Invariants pre-check (cheap)
    inv_ok, inv_reason = validate_proposal_against_invariants(proposal, ctx.invariants)
    if not inv_ok:
        audit_io.append_audit_entry(ctx.agent_dir, build_audit_entry(
            tick_id=ctx.tick_id, now=ctx.now,
            proposal=proposal,
            cycle_output={"verdict": "invariants_rejected",
                          "candidates": [], "errors": []},
            mode=ctx.mode,
            invariants_ok=False,
            invariants_reason=inv_reason,
            counter=counter,
        ))
        return {
            "controller_id": proposal["controller_id"],
            "verdict": "invariants_rejected",
            "reason": inv_reason,
            "patch_id": None,
        }

    # 2. Pull controller context for the backtest loop
    ctrl_perf = (ctx.snapshots.controller_performance_by_ctrl
                 .get(proposal["controller_id"]) or {})
    diagnostic = ctrl_perf.get("diagnostic") or {}
    suboptimal_minutes = diagnostic.get("suboptimal_period_minutes")
    # We require the orchestrator caller to have stashed the actual
    # `current_config` into the proposal under `_current_config`. The
    # LLM doesn't see it — it's filled by the caller after the LLM
    # responds. Same for routing identifiers below.
    current_config = proposal.get("_current_config") or {}

    cycle = await backtest_loop.run_backtest_cycle(
        client=proposal.get("_client_ref"),  # injected by caller
        agent_dir=ctx.agent_dir,
        proposal=proposal,
        current_config=current_config,
        controller_id=proposal["controller_id"],
        invariants=ctx.invariants,
        now=ctx.now,
        suboptimal_period_minutes=suboptimal_minutes,
    )

    # 3. Anti-evidence handling
    if cycle["verdict"] == "all_candidates_worse_than_baseline":
        backtest_loop.write_anti_evidence(
            agent_dir=ctx.agent_dir,
            tick_id=ctx.tick_id,
            controller_id=proposal["controller_id"],
            regime_observed=proposal.get("regime_observed", ""),
            suboptimal_minutes=suboptimal_minutes,
            proposal=proposal,
            cycle_result=cycle,
            now=ctx.now,
        )

    # 4. Append audit entry (with the full cycle output)
    entry = build_audit_entry(
        tick_id=ctx.tick_id, now=ctx.now,
        proposal=proposal,
        cycle_output=cycle,
        mode=ctx.mode,
        invariants_ok=True,
        invariants_reason=None,
        counter=counter,
    )
    audit_io.append_audit_entry(ctx.agent_dir, entry)

    # 5. Mode-dependent action — only if a winner emerged
    if cycle["verdict"] != "winner_found":
        return {
            "controller_id": proposal["controller_id"],
            "verdict": cycle["verdict"],
            "patch_id": entry["patch_id"],
        }

    if ctx.mode == "shadow":
        # Log-only. The audit entry already captures the proposal.
        audit_io.update_audit_entry(ctx.agent_dir, entry["patch_id"], {
            "human_verdict": "shadow_logged",
        })
        return {
            "controller_id": proposal["controller_id"],
            "verdict": "shadow_logged",
            "patch_id": entry["patch_id"],
        }

    if ctx.mode == "auto":
        return await _handle_auto(ctx, entry, cycle)

    # 6. Propose mode (default)
    if ctx.send_proposal_fn is None or ctx.bot_obj is None or ctx.propose_chat_id is None:
        # Misconfigured: write audit entry as pending but skip the
        # Telegram send — easier to debug than silently failing.
        audit_io.update_audit_entry(ctx.agent_dir, entry["patch_id"], {
            "human_verdict": "pending_send_skipped",
            "apply_error": "send_proposal_fn missing in TickContext",
        })
        return {
            "controller_id": proposal["controller_id"],
            "verdict": "pending_send_skipped",
            "patch_id": entry["patch_id"],
        }

    try:
        # send_proposal will mark verdict pending + write message_id back
        updated = await ctx.send_proposal_fn(
            ctx.bot_obj, ctx.agent_dir, entry,
            chat_id=ctx.propose_chat_id,
        )
    except Exception as e:
        logger.exception("orchestrator: send_proposal failed")
        audit_io.update_audit_entry(ctx.agent_dir, entry["patch_id"], {
            "apply_error": f"send_proposal failed: {e}",
        })
        return {
            "controller_id": proposal["controller_id"],
            "verdict": "send_failed",
            "patch_id": entry["patch_id"],
            "error": str(e),
        }

    return {
        "controller_id": proposal["controller_id"],
        "verdict": "proposed",
        "patch_id": entry["patch_id"],
        "telegram_message_id": updated.get("telegram_message_id"),
    }


async def _handle_auto(
    ctx: TickContext, entry: dict[str, Any], cycle: dict[str, Any],
) -> dict[str, Any]:
    """Auto-mode: apply directly if the stricter checks pass."""
    ok, reason = auto_mode_checks_pass(ctx.invariants, cycle)
    if not ok:
        audit_io.update_audit_entry(ctx.agent_dir, entry["patch_id"], {
            "applied": False,
            "apply_error": f"auto_mode_blocked: {reason}",
            "human_verdict": "auto_blocked",
        })
        return {
            "controller_id": entry["controller_id"],
            "verdict": "auto_blocked",
            "patch_id": entry["patch_id"],
            "reason": reason,
        }

    if ctx.mcp_apply_fn is None:
        audit_io.update_audit_entry(ctx.agent_dir, entry["patch_id"], {
            "applied": False,
            "apply_error": "mcp_apply_fn missing in TickContext",
        })
        return {
            "controller_id": entry["controller_id"],
            "verdict": "auto_apply_missing_fn",
            "patch_id": entry["patch_id"],
        }

    selected = cycle.get("selected") or {}
    proposal = entry["llm_proposal"]
    controller_full = entry["controller_id"]
    config_name = controller_full.split("::", 1)[1] if "::" in controller_full else controller_full

    try:
        api_result = await ctx.mcp_apply_fn(
            bot_name=entry["bot_name"],
            controller_id=config_name,
            field=proposal["dimension"],
            value=selected.get("value"),
            expected_old_value=entry.get("old_value"),
        )
    except Exception as e:
        audit_io.update_audit_entry(ctx.agent_dir, entry["patch_id"], {
            "applied": False,
            "apply_error": f"mcp_apply raised: {e}",
            "human_verdict": "auto_apply_failed",
        })
        return {
            "controller_id": controller_full,
            "verdict": "auto_apply_failed",
            "patch_id": entry["patch_id"],
            "error": str(e),
        }

    if not api_result.get("success"):
        audit_io.update_audit_entry(ctx.agent_dir, entry["patch_id"], {
            "applied": False,
            "apply_error": api_result,
            "human_verdict": "auto_apply_failed",
        })
        return {
            "controller_id": controller_full,
            "verdict": "auto_apply_failed",
            "patch_id": entry["patch_id"],
            "error_code": api_result.get("error_code"),
        }

    audit_io.update_audit_entry(ctx.agent_dir, entry["patch_id"], {
        "applied": True,
        "applied_at": state_io.utc_iso(ctx.now),
        "apply_result": api_result,
        "human_verdict": "auto_applied",
        "verdict_at": state_io.utc_iso(ctx.now),
        "verdict_by": "auto_mode",
    })
    audit_io.upsert_last_change(
        ctx.agent_dir,
        controller_id=controller_full,
        field=proposal["dimension"],
        old_value=entry.get("old_value"),
        new_value=selected.get("value"),
        patch_id=entry["patch_id"],
        ts=ctx.now,
        marker="applied",
    )

    return {
        "controller_id": controller_full,
        "verdict": "auto_applied",
        "patch_id": entry["patch_id"],
    }
