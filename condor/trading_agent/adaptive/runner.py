"""Adaptive agent runner — the entry point that actually runs the framework.

The orchestrator (``orchestrator.run_tick``) is pure: every external
dependency is injected. This module is the **glue** that turns those
injection points into real dependencies — a Hummingbot client, the three
routines, an LLM client — and assembles them into a runnable service.

CLI::

    python -m condor.trading_agent.adaptive.runner <agent-slug> \\
        --server <name>                # default: from agent.default_config
        --mode propose|shadow|auto     # default: from agent.default_config
        --llm real|mock-no-action|mock-propose
        --once                         # single tick, then exit (default)
        --loop                         # run forever with frequency_sec sleep
        --max-ticks N                  # cap for --loop (default: 0 = unlimited)
        --tick-id N                    # starting tick id (default: 1)

Architecture choice: we call the routines' **pure functions** rather
than their async ``run()`` entry point. Reasons:

- ``run()`` takes a ``ContextTypes.DEFAULT_TYPE`` (Telegram context) and
  drives side-effects (persisting reports, sending Telegram messages).
  Off-Telegram we want neither.
- The agent only needs the structured ``agent:*`` payloads — the
  user-facing rendering (KPIs, tables, narrative, report HTML) is dead
  weight for the supervisor.
- LEARNINGS L5: ``WebRoutineContext`` with chat_id=0 has subtle bugs
  around server resolution that we already learned to avoid.

So we re-build the agent payloads by calling the routines' pure helpers
directly. The pieces involved per routine are documented inline below.

LLM clients: ``real`` uses ``agent.agent_key`` to route to claude-code
(ACP subprocess) or pydantic-ai. The mock variants are pure functions
useful for smoke tests and CI.

Telegram: by default the runner does NOT need Telegram (modes
``shadow`` and ``auto`` don't). When mode is ``propose`` and the agent
config has ``notifications.propose_chat_id``, the runner builds a
minimal aiohttp-based Bot via python-telegram-bot. If the bot is
unavailable, the orchestrator falls back to "pending_send_skipped" and
keeps going — the audit log shows it, no crash.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from condor.trading_agent.adaptive import audit as audit_io
from condor.trading_agent.adaptive import orchestrator as orch
from condor.trading_agent.adaptive.invariants import load_invariants
from condor.trading_agent.strategy import StrategyStore

logger = logging.getLogger(__name__)


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_AGENTS_ROOT = _REPO_ROOT / "trading_agents"


# ---------------------------------------------------------------------------
# Agent loading
# ---------------------------------------------------------------------------


def load_agent(slug: str) -> tuple[Any, str, dict, Any]:
    """Load the agent's metadata + ``agent.md`` body + ``invariants.yaml``.

    Returns ``(strategy, agent_md_body, adaptive_config, invariants)``.

    ``strategy`` is the parsed ``Strategy`` object Condor uses (from
    ``StrategyStore``). ``agent_md_body`` is the markdown body the LLM
    sees. ``adaptive_config`` is the ``default_config.adaptive`` block
    that holds scope, routines, backtest, notifications.
    """
    store = StrategyStore()
    strategy = store.get_by_slug(slug)
    if strategy is None:
        # Fall back: legacy strategies may have an id-keyed entry.
        strategy = store.get(slug)
    if strategy is None:
        raise ValueError(f"agent not found: {slug!r}")

    agent_dir = _AGENTS_ROOT / slug
    if not agent_dir.exists():
        raise ValueError(f"agent dir does not exist: {agent_dir}")

    inv_path = agent_dir / "invariants.yaml"
    if not inv_path.exists():
        raise ValueError(
            f"{slug!r} is not an adaptive-framework agent — "
            f"no invariants.yaml at {inv_path}"
        )
    invariants = load_invariants(inv_path)

    adaptive_config = (strategy.default_config or {}).get("adaptive") or {}

    return strategy, strategy.instructions, adaptive_config, invariants


# ---------------------------------------------------------------------------
# Snapshot building — call routines' pure helpers directly
# ---------------------------------------------------------------------------


async def _fetch_routine_payloads(
    client: Any, adaptive_config: dict
) -> orch.RoutineSnapshots:
    """Run the three routines off-Telegram and pull out the agent
    payloads.

    We avoid the ``run()`` entry points because they take a Telegram
    context and produce reports + sections we don't need. Instead we
    reproduce the agent-view-building portion using the same helpers
    the routines expose for testing.
    """
    # Local imports keep the test suite snappy (these modules pull in
    # numpy / Pydantic / Telegram via the routine bases).
    from routines import capital_state as cs
    from routines import market_regime as mr
    from routines import controller_performance as cp

    capital_payload = await _build_capital_state_payload(client, cs, adaptive_config)
    market_regime_summary, mr_by_pair = await _build_market_regime_payloads(
        client, mr, adaptive_config,
    )
    perf_summary, perf_by_ctrl = await _build_controller_performance_payloads(
        client, cp, adaptive_config,
    )

    return orch.RoutineSnapshots(
        capital_state=capital_payload,
        market_regime=market_regime_summary,
        controller_performance=perf_summary,
        market_regime_by_pair=mr_by_pair,
        controller_performance_by_ctrl=perf_by_ctrl,
    )


async def _build_capital_state_payload(client, cs_mod, adaptive_config) -> dict:
    """Replay capital_state's pipeline without the report/Telegram side.

    Mirrors the structure inside ``routines/capital_state.py::run`` up
    to the agent-payload step. We only need the agent view, not the
    user-facing rendering.
    """
    bots_data = await cs_mod._get_active_bots(client)
    if not bots_data:
        return {"controllers": [], "wallet": {}, "oversub_alerts": [],
                "totals": {"controllers": 0}}

    scope = adaptive_config.get("scope") or {}
    bot_names_filter = scope.get("bot_names") or []
    if bot_names_filter:
        bots_data = {k: v for k, v in bots_data.items() if k in bot_names_filter}

    bot_names = list(bots_data.keys())
    configs_per_bot = await asyncio.gather(
        *(cs_mod._get_bot_configs(client, b) for b in bot_names)
    )

    controllers: list[dict] = []
    connector_names: set[str] = set()
    controller_filter = scope.get("controller_filter") or "pmm_mister"

    for bot_name, cfgs in zip(bot_names, configs_per_bot):
        bot_data = bots_data.get(bot_name) or {}
        perf_by_id = bot_data.get("performance", {}) if isinstance(bot_data, dict) else {}
        for cfg in cfgs or []:
            if not isinstance(cfg, dict):
                continue
            if cfg.get("controller_name") != controller_filter:
                continue
            config_name = cfg.get("_config_name") or cfg.get("id")
            if not config_name:
                continue
            perf = cs_mod._resolve_perf(perf_by_id, cfg)
            connector_names.add(cfg.get("connector_name") or "")
            controllers.append(cs_mod._compute_controller_block(
                bot_name, config_name, cfg, perf,
            ))

    accounts = scope.get("accounts") or []
    balances_raw = await cs_mod._get_balances(
        client, accounts, sorted(c for c in connector_names if c),
    )
    wallet_by_token = cs_mod._flatten_balances(balances_raw)

    global_block = cs_mod._compute_global_block(
        controllers, wallet_by_token, safety_margin=0.95,
    )
    wallet_total = sum(w["value_usd"] for w in wallet_by_token.values())
    summary = cs_mod._compute_summary(controllers, global_block, wallet_total)

    full_payload = {
        "ts": _utc_iso_now(),
        "account": accounts[0] if accounts else "all",
        "global": global_block,
        "controllers": controllers,
        "summary": summary,
    }
    return cs_mod._build_agent_payload(full_payload)


async def _build_market_regime_payloads(
    client, mr_mod, adaptive_config,
) -> tuple[dict, dict[str, dict]]:
    """Returns (summary, by_pair) — both shaped per
    AGENT_VS_USER_VIEW.md §market_regime."""
    cfg = mr_mod.Config(
        connector_name=adaptive_config.get("routines", {})
                       .get("market_regime", {}).get("connector_name", "binance"),
        trading_pairs=[],   # autodetect from active controllers
    )

    pairs = list(cfg.trading_pairs or [])
    if not pairs and cfg.trading_pair:
        pairs = [cfg.trading_pair]
    if not pairs:
        pairs = await mr_mod._autodetect_pairs(client, cfg.connector_name)
    if not pairs:
        return ({"pairs": [], "by_pair": {},
                 "counts": {"optimal": 0, "suboptimal": 0, "adverse": 0},
                 "worst": None, "ts": _utc_iso_now()}, {})

    results = await asyncio.gather(
        *(mr_mod._process_one_pair(client, cfg, p) for p in pairs),
        return_exceptions=True,
    )
    payloads_by_pair: dict[str, dict] = {}
    for pair, res in zip(pairs, results):
        if isinstance(res, Exception):
            logger.warning("market_regime: pair %s failed: %s", pair, res)
            continue
        if res:
            payloads_by_pair[pair] = res

    aggregate = mr_mod._compose_aggregate(payloads_by_pair)
    summary = mr_mod._build_agent_summary(aggregate)
    by_pair_agent = {
        pair: mr_mod._build_agent_payload(p)
        for pair, p in payloads_by_pair.items()
    }
    return summary, by_pair_agent


async def _build_controller_performance_payloads(
    client, cp_mod, adaptive_config,
) -> tuple[dict, dict[str, dict]]:
    """Returns (summary, by_controller)."""
    routines_cfg = (adaptive_config.get("routines") or {}).get(
        "controller_performance", {}
    )
    cp_config = cp_mod.Config(
        controller_ids=[],
        bot_names=adaptive_config.get("scope", {}).get("bot_names") or [],
        windows_hours=list(routines_cfg.get("windows_hours") or [1.0, 6.0, 24.0]),
        compute_market_share=bool(routines_cfg.get("compute_market_share", True)),
        persist_snapshot=True,
    )

    discovered = await cp_mod._autodetect_controllers(
        client, cp_config.bot_names or None,
    )
    if not discovered:
        return ({"controllers": [], "counts": {}, "worst": None,
                 "ts": _utc_iso_now()}, {})

    bots_raw = await client.bot_orchestration.get_active_bots_status()
    bots_data = bots_raw.get("data", {}) if isinstance(bots_raw, dict) else {}
    now = datetime.now(timezone.utc)

    from condor.trading_agent.adaptive import state_io

    sem = asyncio.Semaphore(8)

    async def _process(bot_name, config_name, cfg_raw):
        canonical_id = f"{bot_name}::{config_name}"
        perfs_map = (bots_data.get(bot_name) or {}).get("performance", {}) or {}
        perf = cp_mod._resolve_perf(perfs_map, cfg_raw)
        if perf is None:
            return canonical_id, None

        history = state_io.read_jsonl_all(cp_mod._history_path(canonical_id))
        market_cross: dict = {}
        if cp_config.compute_market_share:
            snap_now = cp_mod._extract_snapshot(perf)
            async with sem:
                market_cross = await cp_mod._maybe_fetch_market_share(
                    client,
                    cfg_raw.get("connector_name") or "",
                    cfg_raw.get("trading_pair") or "",
                    snap_now["volume_traded"],
                )

        payload = cp_mod._build_controller_payload(
            canonical_id=canonical_id, bot_name=bot_name, config_name=config_name,
            cfg_raw=cfg_raw, perf=perf, history=history, now=now,
            cfg=cp_config, market_cross=market_cross,
        )
        if cp_config.persist_snapshot:
            cp_mod._persist_snapshot(canonical_id, now, cp_mod._extract_snapshot(perf))
        return canonical_id, payload

    results = await asyncio.gather(
        *(_process(b, c, raw) for b, c, raw in discovered),
        return_exceptions=True,
    )
    payloads_by_id: dict[str, dict] = {}
    for r in results:
        if isinstance(r, Exception):
            continue
        cid, payload = r
        if payload:
            payloads_by_id[cid] = payload

    aggregate = cp_mod._compose_aggregate(payloads_by_id)
    summary = cp_mod._build_agent_summary(aggregate, payloads_by_id)
    by_ctrl = {cid: cp_mod._build_agent_payload(p)
               for cid, p in payloads_by_id.items()}
    return summary, by_ctrl


# ---------------------------------------------------------------------------
# LLM client factory
# ---------------------------------------------------------------------------


def build_llm_client(
    *, kind: str, agent_key: str = "claude-code",
) -> orch.LLMCall:
    """Return an async ``llm_call(prompt) -> str`` per the kind selected.

    Kinds:
    - ``mock-no-action``: returns ``{"action":"no_action","reason":"mock"}``
    - ``mock-propose``: returns a deterministic D5-rule propose for the
      first controller in the prompt (parses the prompt to find one).
    - ``real``: routes via ``agent_key`` — claude-code (ACP) or
      pydantic-ai. Not wired in this commit because it requires the
      Condor ACP infrastructure to be running; the runner refuses with
      a clear error pointing at the next steps.
    """
    if kind == "mock-no-action":
        async def _no_action(_prompt: str) -> str:
            return json.dumps({"action": "no_action", "reason": "mock"})
        return _no_action

    if kind == "mock-propose":
        async def _propose(prompt: str) -> str:
            ctrl = _extract_first_controller(prompt)
            if ctrl is None:
                return json.dumps({"action": "no_action",
                                   "reason": "mock: no controllers in prompt"})
            return json.dumps({
                "action": "propose",
                "proposals": [{
                    "controller_id": ctrl,
                    "dimension": "take_profit",
                    "direction": "increase",
                    "magnitude_qualitative": "medium",
                    "reasoning": "Mock LLM: applying MVP D5 rule (raise TP in "
                                 "mean-reverting high-vol regimes).",
                    "caveats": ["take_profit only affects new executors"],
                }],
            })
        return _propose

    if kind == "real":
        return _build_real_llm_client(agent_key)

    raise ValueError(f"unknown --llm kind: {kind!r}")


def _extract_first_controller(prompt: str) -> str | None:
    """Quick'n'dirty scan for a controller_id mention in the prompt JSON
    blocks. Good enough for the mock — production LLM doesn't need this.

    Filters out placeholders that look like ``<bot_name>::<config_name>``
    coming from the agent.md output-format example, plus any line that
    has markdown noise (backticks, brackets).
    """
    import re

    # IDs are of the shape ``bot_name::config_name`` where each side is
    # made of word chars, dashes, dots, or digits. The actual canonical
    # IDs from brigado look like
    # ``experiment-legacy-20_v2_rebalanced-20260509-005920::pmm_mister_binance_btcusdt-1-5_exp_a_legacy``.
    pattern = re.compile(r"\b([A-Za-z0-9._\-]+::[A-Za-z0-9._\-]+)\b")

    for line in prompt.splitlines():
        # Skip example placeholders and markdown noise
        if "<" in line or ">" in line or "`" in line:
            continue
        m = pattern.search(line)
        if m:
            return m.group(1)
    return None


def _build_real_llm_client(agent_key: str) -> orch.LLMCall:
    """Wires the real LLM client via Condor's ACP / pydantic-ai layers.

    This needs a long-running ACP session, which is wired today only
    via the Telegram handler stack. The right way to call into ACP
    headlessly is to import ``condor.acp.client.AcpClient``, start a
    session with the agent.md prompt, and ``send_message`` per tick.

    For this commit the function is a stub that points to the next
    integration step — the runner advertises ``--llm real`` is not
    available yet so users land on a clear message instead of a
    cryptic stack trace.
    """
    async def _stub(_prompt: str) -> str:
        raise NotImplementedError(
            f"--llm real (agent_key={agent_key!r}) not implemented in this "
            f"commit. Use --llm mock-no-action or --llm mock-propose for "
            f"smoke tests. The integration with ACP / pydantic-ai is the "
            f"next deliverable (see TASKS.md Fase 5.7 pending bullet)."
        )
    return _stub


# ---------------------------------------------------------------------------
# Proposal context enrichment
# ---------------------------------------------------------------------------


def _make_process_wrapper(client, bots_data: dict, snapshots: orch.RoutineSnapshots):
    """Returns a wrapper of ``orchestrator._process_proposal`` that
    enriches the proposal with the data the backtest loop needs.

    The orchestrator can't pull this context itself because that would
    couple it to the routine internals. The runner — which already has
    the client and the snapshots — is the right place to inject.
    """
    orig_process = orch._process_proposal

    async def _wrapper(c, proposal, *, counter):
        cid_full = proposal.get("controller_id", "")
        bot_name, _, config_name = cid_full.partition("::")
        proposal["bot_name"] = bot_name
        proposal["config_name"] = config_name
        proposal["_client_ref"] = client

        # Pull regime info for the audit + anti-evidence.
        regime_view = snapshots.market_regime_by_pair
        pair = (snapshots.controller_performance_by_ctrl
                .get(cid_full, {})
                .get("trading_pair"))
        if pair and pair in regime_view:
            proposal["regime_observed"] = regime_view[pair].get("regime")

        ctrl_diag = (snapshots.controller_performance_by_ctrl
                     .get(cid_full, {})
                     .get("diagnostic", {}))
        proposal["suboptimal_minutes"] = ctrl_diag.get("suboptimal_period_minutes")
        proposal["warmup_status"] = ctrl_diag.get("warmup_status")
        proposal["trading_pair"] = pair
        proposal["connector_name"] = "binance"  # MVP: pmm_mister is binance spot

        # Fetch the controller's actual config (for backtest baseline +
        # type coercion in the apply path).
        try:
            cfgs = await client.controllers.get_bot_controller_configs(bot_name)
            cfg = next(
                (c for c in cfgs or []
                 if isinstance(c, dict)
                 and (c.get("_config_name") == config_name or c.get("id") == config_name)),
                None,
            )
            proposal["_current_config"] = cfg or {}
        except Exception as e:
            logger.warning("runner: failed to fetch config for %s: %s", cid_full, e)
            proposal["_current_config"] = {}

        return await orig_process(c, proposal, counter=counter)

    return orig_process, _wrapper


# ---------------------------------------------------------------------------
# Single tick
# ---------------------------------------------------------------------------


async def tick_once(
    *,
    agent_slug: str,
    server_name: str | None = None,
    mode: str | None = None,
    llm_kind: str = "mock-no-action",
    tick_id: int = 1,
    bot_obj: Any | None = None,
    send_proposal_fn: orch.SendProposalFn | None = None,
    mcp_apply_fn: Callable[..., Awaitable[dict[str, Any]]] | None = None,
) -> orch.TickResult:
    """Run a single tick of the adaptive supervisor.

    The CLI calls this with --once; --loop calls it in a loop. The
    function is itself pure-ish: every IO it does is mediated by the
    HB client (transient) and the agent's state dir (persistent).
    """
    strategy, agent_md_body, adaptive_config, invariants = load_agent(agent_slug)

    # Resolve mode (CLI > agent config)
    effective_mode = mode or adaptive_config.get("mode", "propose")
    if effective_mode not in ("propose", "shadow", "auto"):
        raise ValueError(f"invalid mode: {effective_mode!r}")

    # Resolve server (CLI > strategy default_config > strategy server_name)
    effective_server = (
        server_name
        or (strategy.default_config or {}).get("server_name")
        or "local"
    )

    agent_dir = _AGENTS_ROOT / agent_slug

    # Connect HB client. ``with_client`` caches the connection; we
    # close it at the end via ``cleanup`` so the CLI doesn't leak
    # aiohttp warnings.
    from condor.tools.condor_inspect import with_client, cleanup

    async with with_client(effective_server) as client:
        # Pull snapshots from the three routines
        snapshots = await _fetch_routine_payloads(client, adaptive_config)

        # Build the proposal wrapper so the backtest loop has the right context
        orig_process, wrapper = _make_process_wrapper(client, {}, snapshots)
        import condor.trading_agent.adaptive.orchestrator as orch_mod
        orch_mod._process_proposal = wrapper

        try:
            llm_call = build_llm_client(kind=llm_kind, agent_key=strategy.agent_key)
            ctx = orch.TickContext(
                agent_dir=agent_dir,
                invariants=invariants,
                snapshots=snapshots,
                llm_call=llm_call,
                mode=effective_mode,
                tick_id=tick_id,
                now=datetime.now(timezone.utc),
                propose_chat_id=(adaptive_config.get("notifications") or {})
                                .get("propose_chat_id"),
                send_proposal_fn=send_proposal_fn,
                bot_obj=bot_obj,
                mcp_apply_fn=mcp_apply_fn,
            )
            result = await orch.run_tick(ctx, agent_md_body=agent_md_body)
        finally:
            orch_mod._process_proposal = orig_process

    # Close cached HTTP clients to avoid aiohttp's "unclosed session"
    # warning on CLI exit.
    await cleanup()

    return result


# ---------------------------------------------------------------------------
# Loop
# ---------------------------------------------------------------------------


async def run_forever(
    *,
    agent_slug: str,
    server_name: str | None,
    mode: str | None,
    llm_kind: str,
    max_ticks: int = 0,
) -> None:
    """Run ``tick_once`` in a loop with the agent's configured
    ``frequency_sec`` sleep between ticks.

    Stops after ``max_ticks`` if set, or on KeyboardInterrupt.
    """
    strategy, _body, adaptive_config, _inv = load_agent(agent_slug)
    freq = float((strategy.default_config or {}).get("frequency_sec") or 600)

    tick_id = 1
    while True:
        logger.info("runner: starting tick %d", tick_id)
        try:
            result = await tick_once(
                agent_slug=agent_slug,
                server_name=server_name,
                mode=mode,
                llm_kind=llm_kind,
                tick_id=tick_id,
            )
            logger.info(
                "runner: tick %d done — llm=%s proposals=%d skipped=%s",
                tick_id, result.llm_action, result.proposals_total,
                result.skipped,
            )
        except KeyboardInterrupt:
            logger.info("runner: interrupted, exiting")
            return
        except Exception as e:
            logger.exception("runner: tick %d crashed: %s", tick_id, e)
            # Don't abort the loop on transient errors — back off and
            # try the next tick.

        tick_id += 1
        if 0 < max_ticks < tick_id:
            logger.info("runner: max_ticks reached, exiting")
            return

        try:
            await asyncio.sleep(freq)
        except KeyboardInterrupt:
            return


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _utc_iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="condor.trading_agent.adaptive.runner",
        description="Run the adaptive strategy framework for a given agent.",
    )
    p.add_argument("agent_slug", help="Slug of the agent (e.g. adaptive_pmm)")
    p.add_argument("--server", default=None,
                   help="HB server name (default: from agent default_config)")
    p.add_argument("--mode", choices=("propose", "shadow", "auto"),
                   default=None,
                   help="Override the mode in agent default_config")
    p.add_argument("--llm", default="mock-no-action",
                   choices=("real", "mock-no-action", "mock-propose"),
                   help="LLM client kind (default: mock-no-action)")
    p.add_argument("--once", action="store_true",
                   help="Run a single tick and exit (default)")
    p.add_argument("--loop", action="store_true",
                   help="Run continuously with frequency_sec sleep")
    p.add_argument("--max-ticks", type=int, default=0,
                   help="Cap for --loop (0 = unlimited)")
    p.add_argument("--tick-id", type=int, default=1,
                   help="Starting tick id for --once (default: 1)")
    p.add_argument("--verbose", "-v", action="count", default=0,
                   help="-v INFO, -vv DEBUG")
    return p


def _configure_logging(verbose: int) -> None:
    level = logging.WARNING
    if verbose >= 2:
        level = logging.DEBUG
    elif verbose >= 1:
        level = logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


async def _main_async(args: argparse.Namespace) -> int:
    if args.loop and args.once:
        print("error: --once and --loop are mutually exclusive", file=sys.stderr)
        return 2

    if args.loop:
        await run_forever(
            agent_slug=args.agent_slug,
            server_name=args.server,
            mode=args.mode,
            llm_kind=args.llm,
            max_ticks=args.max_ticks,
        )
        return 0

    # Default: --once
    result = await tick_once(
        agent_slug=args.agent_slug,
        server_name=args.server,
        mode=args.mode,
        llm_kind=args.llm,
        tick_id=args.tick_id,
    )
    print(json.dumps({
        "tick_id": result.tick_id,
        "ts": result.ts,
        "mode": result.mode,
        "skipped": result.skipped,
        "skipped_reason": result.skipped_reason,
        "llm_action": result.llm_action,
        "llm_reason": result.llm_reason,
        "proposals_total": result.proposals_total,
        "proposals_processed": result.proposals_processed,
        "errors": result.errors,
    }, indent=2, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)
    try:
        return asyncio.run(_main_async(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
