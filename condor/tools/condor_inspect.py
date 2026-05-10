"""Condor Inspect — high-signal helpers to query a live Condor server.

Designed for on-demand inspection from one-off Python invocations
(typically from the agent's environment, via the `condor-express`
skill). NOT meant to be used from production code paths — for that,
use the Condor handlers/routines/MCP tools directly.

Key design decisions:

- Re-uses ``config_manager.get_config_manager()`` for credentials, so
  whatever servers the local user has configured are immediately
  available. No extra auth.
- Each helper opens a client, runs the query, and closes the client —
  no shared state, no singletons. Safe to call from short-lived scripts.
- Returns plain dicts/lists (not Pydantic models). Easy to print, dump
  to JSON, or process further.
- Errors are NOT swallowed. If the server is unreachable or the API
  returns an error, the exception bubbles up with the full traceback.

Typical usage from a one-shot invocation::

    .venv/bin/python -c "
    import asyncio, json
    from condor.tools.condor_inspect import get_controller_state, cleanup

    async def main():
        out = await get_controller_state(
            'brigado',
            'experiment-legacy-20_v2_rebalanced-20260509-005920',
            'pmm_mister_binance_btcusdt-1-5_exp_a_legacy',
        )
        print(json.dumps(out, indent=2, default=str))
        await cleanup()

    asyncio.run(main())
    "

For more complex queries, use the lower-level ``with_client`` context
manager. Always finish your script with ``await cleanup()`` to avoid
``Unclosed client session`` warnings.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from config_manager import get_config_manager

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------


@asynccontextmanager
async def with_client(server_name: str) -> AsyncIterator[Any]:
    """Async context manager that yields a connected hummingbot-api client.

    The client is **cached** by the underlying ``ConfigManager`` and shared
    across calls in the same process — we do NOT close it on exit. To
    cleanup at the end of a one-shot script, call :func:`cleanup` once.

    Usage::

        async with with_client("brigado") as client:
            data = await client.bot_orchestration.get_active_bots_status()

        await cleanup()  # at the very end of your script
    """
    cm = get_config_manager()
    client = await cm.get_client(server_name)
    if client is None:
        raise RuntimeError(
            f"Could not connect to server '{server_name}'. "
            f"Available servers: {sorted((cm.list_servers() or {}).keys())}"
        )
    yield client


async def cleanup() -> None:
    """Close all cached HTTP clients. Call once at the end of a script.

    Avoids the ``Unclosed client session`` warnings from aiohttp when the
    process exits with cached connections still open.
    """
    cm = get_config_manager()
    try:
        await cm.close_all_clients()
    except Exception as e:
        logger.debug("close_all_clients failed: %s", e)


def list_known_servers() -> list[str]:
    """List server names configured locally. No network call."""
    cm = get_config_manager()
    return sorted((cm.list_servers() or {}).keys())


# ---------------------------------------------------------------------------
# Bots and controllers — discovery
# ---------------------------------------------------------------------------


async def list_active_bots(server_name: str) -> list[str]:
    """Names of bots currently active on the server."""
    async with with_client(server_name) as client:
        raw = await client.bot_orchestration.get_active_bots_status()
        data = raw.get("data", {}) if isinstance(raw, dict) else {}
        return sorted(data.keys())


async def list_controllers(
    server_name: str,
    bot_name: str | None = None,
) -> list[dict[str, Any]]:
    """Flat list of controllers across one or all active bots.

    Each entry has: bot_name, config_name, controller_name,
    controller_type, connector_name, trading_pair, total_amount_quote,
    portfolio_allocation, max_active_executors_by_level, plus the full
    raw config under ``_raw``.
    """
    async with with_client(server_name) as client:
        bots_status = await client.bot_orchestration.get_active_bots_status()
        bots_data = bots_status.get("data", {}) if isinstance(bots_status, dict) else {}
        target_bots = [bot_name] if bot_name else list(bots_data.keys())

        out: list[dict[str, Any]] = []
        for b in target_bots:
            try:
                cfgs = await client.controllers.get_bot_controller_configs(b)
            except Exception as e:
                logger.warning("Could not fetch configs for %s: %s", b, e)
                continue
            if not isinstance(cfgs, list):
                continue
            for cfg in cfgs:
                if not isinstance(cfg, dict):
                    continue
                out.append({
                    "bot_name": b,
                    "config_name": cfg.get("_config_name") or cfg.get("id"),
                    "controller_name": cfg.get("controller_name"),
                    "controller_type": cfg.get("controller_type"),
                    "connector_name": cfg.get("connector_name"),
                    "trading_pair": cfg.get("trading_pair"),
                    "total_amount_quote": cfg.get("total_amount_quote"),
                    "portfolio_allocation": cfg.get("portfolio_allocation"),
                    "max_active_executors_by_level": cfg.get("max_active_executors_by_level"),
                    "_raw": cfg,
                })
        return out


# ---------------------------------------------------------------------------
# Per-controller deep state
# ---------------------------------------------------------------------------


async def get_controller_state(
    server_name: str,
    bot_name: str,
    config_name: str,
) -> dict[str, Any]:
    """Snapshot of a single controller: config + performance + computed fields.

    Returns:
        {
          "bot_name", "config_name",
          "config": <full YAML config dict>,
          "performance": <inner performance dict from MQTT or None>,
          "positions_summary": [<position dicts>],
          "computed": {
            "nominal_budget_usd": total_amount_quote * portfolio_allocation,
            "worst_case_usd":     nominal × max_active_executors_by_level,
            "committed_now_usd":  sum(amount * breakeven_price) over positions,
            "active_executors":   number of positions,
          },
        }

    The matching of config to performance is robust to the same key drift
    that bit ``capital_state.py`` (probes `_config_name` then falls back).
    """
    async with with_client(server_name) as client:
        # Configs of the bot
        cfgs = await client.controllers.get_bot_controller_configs(bot_name)
        cfg = next(
            (c for c in (cfgs or []) if isinstance(c, dict) and c.get("_config_name") == config_name),
            None,
        )
        if cfg is None:
            return {
                "bot_name": bot_name,
                "config_name": config_name,
                "error": "config not found",
                "available_configs": [
                    c.get("_config_name") for c in (cfgs or []) if isinstance(c, dict)
                ],
            }

        # Performance snapshot for the bot
        bots_status = await client.bot_orchestration.get_active_bots_status()
        bot_data = (bots_status.get("data", {}) if isinstance(bots_status, dict) else {}).get(bot_name) or {}
        perf_by_id = bot_data.get("performance", {}) if isinstance(bot_data, dict) else {}

        perf = None
        for k in (cfg.get("_config_name"), cfg.get("id"), cfg.get("controller_id")):
            if k and k in perf_by_id:
                entry = perf_by_id[k]
                if isinstance(entry, dict):
                    perf = entry.get("performance", entry)
                break

        positions_summary = (perf.get("positions_summary") if isinstance(perf, dict) else None) or []

        # Computed fields (mirrors capital_state's logic)
        total_amount_quote = float(cfg.get("total_amount_quote", 0) or 0)
        portfolio_allocation = float(cfg.get("portfolio_allocation", 0) or 0)
        max_executors = int(cfg.get("max_active_executors_by_level", 1) or 1)
        nominal_budget_usd = total_amount_quote * portfolio_allocation
        worst_case_usd = nominal_budget_usd * max_executors

        committed_now_usd = 0.0
        for p in positions_summary:
            if not isinstance(p, dict):
                continue
            amount = float(p.get("amount", 0) or 0)
            breakeven = float(p.get("breakeven_price", 0) or 0)
            notional = amount * breakeven
            committed_now_usd += notional if notional > 0 else float(p.get("current_value", 0) or 0)

        return {
            "bot_name": bot_name,
            "config_name": config_name,
            "config": cfg,
            "performance": perf,
            "positions_summary": positions_summary,
            "computed": {
                "nominal_budget_usd": nominal_budget_usd,
                "worst_case_usd": worst_case_usd,
                "committed_now_usd": committed_now_usd,
                "active_executors": len(positions_summary),
                "utilization_now": (
                    committed_now_usd / nominal_budget_usd if nominal_budget_usd > 0 else 0.0
                ),
            },
        }


# ---------------------------------------------------------------------------
# Wallet / portfolio
# ---------------------------------------------------------------------------


async def get_wallet_state(
    server_name: str,
    account_name: str | None = None,
) -> dict[str, Any]:
    """Wallet balances for one account (or all if ``account_name`` is None).

    Returns the raw shape from ``client.portfolio.get_state()`` —
    ``{account: {connector: [{token, units, value, ...}, ...]}}``.

    Use ``flatten_balances`` (below) to aggregate by token.
    """
    async with with_client(server_name) as client:
        accounts = [account_name] if account_name else []
        try:
            raw = await client.portfolio.get_state(
                account_names=accounts,
                connector_names=[],
                refresh=True,
            )
        except Exception as e:
            return {"error": str(e), "raw": None}
        return raw if isinstance(raw, dict) else {"raw": raw}


def flatten_balances(raw: dict) -> dict[str, dict[str, float]]:
    """Aggregate ``portfolio.get_state`` output by token.

    Returns ``{token: {balance: total_units, value_usd: total_value}}``.
    """
    out: dict[str, dict[str, float]] = {}
    for _account, by_connector in (raw or {}).items():
        if not isinstance(by_connector, dict):
            continue
        for _connector, holdings in by_connector.items():
            if not isinstance(holdings, list):
                continue
            for h in holdings:
                if not isinstance(h, dict):
                    continue
                token = h.get("token") or h.get("symbol")
                if not token:
                    continue
                slot = out.setdefault(token, {"balance": 0.0, "value_usd": 0.0})
                slot["balance"] += float(h.get("units", h.get("amount", 0)) or 0)
                slot["value_usd"] += float(h.get("value", 0) or 0)
    return out


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------


async def get_prices(
    server_name: str,
    connector_name: str,
    trading_pairs: list[str],
) -> dict[str, float]:
    """Current mid-prices for a list of trading pairs on one connector.

    Returns ``{pair: price}``. Pairs the API doesn't know are omitted.
    """
    async with with_client(server_name) as client:
        try:
            raw = await client.market_data.get_prices(
                connector_name=connector_name,
                trading_pairs=",".join(trading_pairs),
            )
        except Exception as e:
            logger.warning("get_prices failed: %s", e)
            return {}
        prices = raw.get("prices", {}) if isinstance(raw, dict) else {}
        return {k: float(v) for k, v in prices.items() if v is not None}


# ---------------------------------------------------------------------------
# Convenience: pretty-print a controller for human consumption
# ---------------------------------------------------------------------------


def format_controller_summary(state: dict[str, Any]) -> str:
    """One-shot human-readable summary of ``get_controller_state`` output."""
    if "error" in state:
        return f"ERROR — {state['error']}\nAvailable: {state.get('available_configs', [])}"

    cfg = state["config"]
    comp = state["computed"]
    lines = [
        f"# {state['config_name']}",
        f"  bot:        {state['bot_name']}",
        f"  pair:       {cfg.get('trading_pair')} on {cfg.get('connector_name')}",
        f"  controller: {cfg.get('controller_name')} ({cfg.get('controller_type')})",
        "",
        "  CONFIG",
        f"    total_amount_quote:            {cfg.get('total_amount_quote')}",
        f"    portfolio_allocation:          {cfg.get('portfolio_allocation')}",
        f"    max_active_executors_by_level: {cfg.get('max_active_executors_by_level')}",
        f"    target/min/max_base_pct:       {cfg.get('target_base_pct')} / {cfg.get('min_base_pct')} / {cfg.get('max_base_pct')}",
        f"    take_profit:                   {cfg.get('take_profit')}",
        f"    buy_spreads:                   {cfg.get('buy_spreads')}",
        f"    sell_spreads:                  {cfg.get('sell_spreads')}",
        "",
        "  COMPUTED",
        f"    nominal_budget_usd: {comp['nominal_budget_usd']:.2f}",
        f"    worst_case_usd:     {comp['worst_case_usd']:.2f}",
        f"    committed_now_usd:  {comp['committed_now_usd']:.2f}",
        f"    utilization_now:    {comp['utilization_now'] * 100:.1f}%",
        f"    active_executors:   {comp['active_executors']}",
        "",
        "  POSITIONS",
    ]
    if not state["positions_summary"]:
        lines.append("    (none)")
    else:
        for p in state["positions_summary"]:
            if not isinstance(p, dict):
                continue
            lines.append(
                f"    {p.get('side','?'):<5} amount={p.get('amount')} "
                f"breakeven={p.get('breakeven_price')} "
                f"unrealized_pnl={p.get('unrealized_pnl_quote')}"
            )
    return "\n".join(lines)
