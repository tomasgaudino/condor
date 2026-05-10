"""Capital State — capital model across controllers and wallet.

First MVP routine of the Adaptive Strategy Framework. Reports capital
dynamics in three planes (see `.planning/strategy-framework/CAPITAL_DYNAMICS.md`):

1. **Controller-local**: nominal_budget, committed_now, worst_case,
   utilization_now / vs worst.
2. **Inter-controller (network)**: per-asset demand vs wallet supply,
   oversub_alerts when demand > 0.95 supply.
3. **Cross-pair**: documented qualitatively only (effects of trading
   between controllers sharing assets).

Returns a structured JSON in `text` (compact) and a human-readable
markdown report. Per-controller history (utilization percentiles
p50/p95/max) is NOT persisted in MVP — `history.available` is False.
That comes in a follow-up commit.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from pydantic import BaseModel, Field
from telegram.ext import ContextTypes

from config_manager import get_client
from routines.base import RoutineResult

logger = logging.getLogger(__name__)

CATEGORY = "Adaptive Framework"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class Config(BaseModel):
    """Capital state across controllers and wallet."""

    account_name: str | None = Field(
        default=None,
        description="Account to inspect (default: master_account from balances)",
    )
    bot_names: list[str] = Field(
        default=[],
        description="Filter to specific bots (empty = all active)",
    )
    controller_filter: str = Field(
        default="pmm_mister",
        description="Filter by controller_name (default: pmm_mister, empty = all)",
    )
    target_chat_id: int | None = Field(
        default=None,
        description="Send compact monitor view to this chat ID",
    )
    safety_margin: float = Field(
        default=0.95,
        description="Oversub ratio threshold for warn severity (above = warn)",
    )


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _fmt_usd(n: float) -> str:
    if abs(n) >= 1_000_000:
        return f"${n / 1_000_000:.1f}M"
    if abs(n) >= 1_000:
        return f"${n / 1_000:.1f}k"
    return f"${n:.2f}"


def _fmt_pct(ratio: float) -> str:
    return f"{ratio * 100:.0f}%"


def _bar(ratio: float, width: int = 10) -> str:
    """ASCII bar showing utilization. ratio clamped to [0, 1] for display only."""
    clamped = max(0.0, min(1.0, ratio))
    filled = round(clamped * width)
    return "▓" * filled + "░" * (width - filled)


def _severity(ratio: float) -> str:
    if ratio < 0.8:
        return "ok"
    if ratio < 1.0:
        return "info"
    if ratio < 1.5:
        return "warn"
    return "crit"


# ---------------------------------------------------------------------------
# Asset parsing — split a trading_pair into base/quote
# ---------------------------------------------------------------------------


def _parse_pair(pair: str) -> tuple[str, str]:
    """Split 'BTC-USDT' or 'BTC/USDT' into ('BTC', 'USDT'). Empty fallback."""
    if "-" in pair:
        b, q = pair.split("-", 1)
        return b, q
    if "/" in pair:
        b, q = pair.split("/", 1)
        return b, q
    return pair, ""


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------


async def _get_active_bots(client) -> dict[str, Any]:
    data = await client.bot_orchestration.get_active_bots_status()
    if isinstance(data, dict) and "data" in data:
        return data["data"]
    return data if isinstance(data, dict) else {}


async def _get_bot_configs(client, bot_name: str) -> list[dict]:
    try:
        raw = await client.controllers.get_bot_controller_configs(bot_name)
        return raw if isinstance(raw, list) else []
    except Exception as e:
        logger.warning("Could not fetch configs for %s: %s", bot_name, e)
        return []


async def _get_balances(client, account_names: list[str], connector_names: list[str]) -> dict:
    try:
        raw = await client.portfolio.get_state(
            account_names=account_names,
            connector_names=connector_names,
            refresh=True,
        )
    except Exception as e:
        logger.warning("Could not fetch balances: %s", e)
        return {}
    return raw if isinstance(raw, dict) else {}


# ---------------------------------------------------------------------------
# Core computation
# ---------------------------------------------------------------------------


def _compute_controller_block(
    bot_name: str,
    config_name: str,
    cfg: dict,
    perf: dict | None,
) -> dict[str, Any]:
    """Build the per-controller section of the output."""
    trading_pair = cfg.get("trading_pair", "?")
    base_asset, quote_asset = _parse_pair(trading_pair)
    connector_name = cfg.get("connector_name", "?")

    total_amount_quote = float(cfg.get("total_amount_quote", 0) or 0)
    portfolio_allocation = float(cfg.get("portfolio_allocation", 0) or 0)
    max_executors = int(cfg.get("max_active_executors_by_level", 1) or 1)

    # Number of buy/sell levels (comma-separated spreads)
    buy_levels = len(_split_csv(cfg.get("buy_spreads", "")))
    sell_levels = len(_split_csv(cfg.get("sell_spreads", "")))

    nominal_budget_usd = total_amount_quote * portfolio_allocation
    worst_case_usd = nominal_budget_usd * max_executors

    # Committed now: from positions_summary in performance snapshot
    committed_now_usd = 0.0
    active_executors_count = 0
    if perf is not None:
        positions_summary = perf.get("positions_summary") or []
        for p in positions_summary:
            if isinstance(p, dict):
                committed_now_usd += float(p.get("current_value", 0) or 0)
        active_executors_count = len(positions_summary)

    utilization_now = (
        committed_now_usd / nominal_budget_usd if nominal_budget_usd > 0 else 0.0
    )
    utilization_vs_worst = (
        committed_now_usd / worst_case_usd if worst_case_usd > 0 else 0.0
    )

    # Inventory snapshot (target / range)
    target_base_pct = float(cfg.get("target_base_pct", 0.5) or 0.5)
    min_base_pct = float(cfg.get("min_base_pct", 0) or 0)
    max_base_pct = float(cfg.get("max_base_pct", 1) or 1)
    current_base_pct = (
        committed_now_usd / total_amount_quote if total_amount_quote > 0 else 0.0
    )
    in_range = min_base_pct <= current_base_pct <= max_base_pct
    drift_from_target = current_base_pct - target_base_pct

    canonical_id = f"{bot_name}::{config_name}"

    return {
        "id": canonical_id,
        "bot_name": bot_name,
        "config_name": config_name,
        "trading_pair": trading_pair,
        "base_asset": base_asset,
        "quote_asset": quote_asset,
        "connector_name": connector_name,
        "config": {
            "total_amount_quote": total_amount_quote,
            "portfolio_allocation": portfolio_allocation,
            "max_active_executors_by_level": max_executors,
            "buy_levels": buy_levels,
            "sell_levels": sell_levels,
        },
        "capital": {
            "nominal_budget_usd": nominal_budget_usd,
            "committed_now_usd": committed_now_usd,
            "worst_case_usd": worst_case_usd,
            "utilization_now": utilization_now,
            "utilization_vs_worst": utilization_vs_worst,
            "active_executors_count": active_executors_count,
        },
        "inventory": {
            "current_base_pct": current_base_pct,
            "target_base_pct": target_base_pct,
            "min_base_pct": min_base_pct,
            "max_base_pct": max_base_pct,
            "in_range": in_range,
            "drift_from_target": drift_from_target,
        },
        "history": {
            "lookback_hours": 24,
            "available": False,
            "samples": 0,
        },
    }


def _split_csv(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return []


def _flatten_balances(balances_raw: dict) -> dict[str, dict[str, float]]:
    """Aggregate balances by token across accounts/connectors.

    Input shape: {account: {connector: [{token, units, value, ...}, ...]}}
    Output shape: {token: {balance: total_units, value_usd: total_value}}
    """
    out: dict[str, dict[str, float]] = {}
    for _account, by_connector in (balances_raw or {}).items():
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
                units = float(h.get("units", h.get("amount", 0)) or 0)
                value = float(h.get("value", 0) or 0)
                slot = out.setdefault(token, {"balance": 0.0, "value_usd": 0.0})
                slot["balance"] += units
                slot["value_usd"] += value
    return out


def _compute_global_block(
    controllers: list[dict],
    wallet_by_token: dict[str, dict[str, float]],
    safety_margin: float,
) -> dict[str, Any]:
    """Per-asset demand vs supply, oversub_alerts."""
    # 1. Map asset → list[controller_id] using it (base or quote)
    controllers_by_asset: dict[str, list[str]] = {}
    for c in controllers:
        for asset in (c["base_asset"], c["quote_asset"]):
            if not asset:
                continue
            controllers_by_asset.setdefault(asset, []).append(c["id"])

    # 2. Per-asset demand: sum of (worst_case_usd × 0.5) for each leg
    #    (MVP simplification: 50/50 split base/quote — see CAPITAL_DYNAMICS.md caveat).
    demand_by_asset: dict[str, float] = {}
    used_now_by_asset: dict[str, float] = {}
    for c in controllers:
        worst = c["capital"]["worst_case_usd"]
        committed = c["capital"]["committed_now_usd"]
        for asset in (c["base_asset"], c["quote_asset"]):
            if not asset:
                continue
            demand_by_asset[asset] = demand_by_asset.get(asset, 0.0) + worst * 0.5
            used_now_by_asset[asset] = used_now_by_asset.get(asset, 0.0) + committed * 0.5

    # 3. Build wallet block + oversub alerts
    wallet: dict[str, dict[str, Any]] = {}
    oversub_alerts: list[dict[str, Any]] = []
    for asset, slot in wallet_by_token.items():
        supply_usd = slot["value_usd"]
        demand_usd = demand_by_asset.get(asset, 0.0)
        used_now_usd = used_now_by_asset.get(asset, 0.0)
        headroom_usd = supply_usd - used_now_usd
        headroom_pct = headroom_usd / supply_usd if supply_usd > 0 else 0.0
        ratio = demand_usd / supply_usd if supply_usd > 0 else float("inf")
        sev = _severity(ratio)

        wallet[asset] = {
            "balance": slot["balance"],
            "value_usd": supply_usd,
            "used_now_usd": used_now_usd,
            "headroom_usd": headroom_usd,
            "headroom_pct": headroom_pct,
            "controllers_using": controllers_by_asset.get(asset, []),
        }

        if sev in ("warn", "crit") and ratio >= safety_margin:
            oversub_alerts.append(
                {
                    "asset": asset,
                    "demand_potential_usd": demand_usd,
                    "supply_usd": supply_usd,
                    "ratio": ratio,
                    "severity": sev,
                    "controllers": controllers_by_asset.get(asset, []),
                }
            )

    return {"wallet": wallet, "oversub_alerts": oversub_alerts}


def _compute_summary(controllers: list[dict], global_block: dict, wallet_total: float) -> dict:
    return {
        "total_controllers": len(controllers),
        "total_nominal_budget_usd": sum(c["capital"]["nominal_budget_usd"] for c in controllers),
        "total_committed_now_usd": sum(c["capital"]["committed_now_usd"] for c in controllers),
        "total_worst_case_usd": sum(c["capital"]["worst_case_usd"] for c in controllers),
        "wallet_total_value_usd": wallet_total,
        "any_oversub": len(global_block["oversub_alerts"]) > 0,
        "max_oversub_ratio": max(
            (a["ratio"] for a in global_block["oversub_alerts"]),
            default=0.0,
        ),
    }


# ---------------------------------------------------------------------------
# Compact monitor render (Telegram-friendly)
# ---------------------------------------------------------------------------


def _render_compact_summary(payload: dict[str, Any]) -> str:
    """Short ASCII summary for the Telegram preview (must fit in ~220 chars).

    Designed to be safe inside a triple-backtick code fence: no backticks,
    no markdown chars that need escaping. Useful even when controllers/wallet
    are empty (won't dump just headers like the full monitor render does).
    """
    summary = payload.get("summary", {})
    wallet = payload.get("global", {}).get("wallet", {})
    alerts = payload.get("global", {}).get("oversub_alerts", [])
    controllers = payload.get("controllers", [])

    lines: list[str] = []
    lines.append(
        f"Controllers: {summary.get('total_controllers', 0)} | "
        f"Alerts: {len(alerts)}"
    )
    lines.append(
        f"Committed: {_fmt_usd(summary.get('total_committed_now_usd', 0.0))} / "
        f"Nominal: {_fmt_usd(summary.get('total_nominal_budget_usd', 0.0))} / "
        f"Worst: {_fmt_usd(summary.get('total_worst_case_usd', 0.0))}"
    )
    lines.append(
        f"Wallet total: {_fmt_usd(summary.get('wallet_total_value_usd', 0.0))} "
        f"({len(wallet)} assets)"
    )
    if alerts:
        worst = max(alerts, key=lambda a: a.get("ratio", 0))
        lines.append(
            f"WORST: {worst.get('asset','?')} {worst.get('ratio',0):.2f}x "
            f"({worst.get('severity','?')})"
        )
    elif not controllers:
        lines.append("(no controllers matched filter)")
    return "\n".join(lines)


def _render_monitor(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("📊 *PORTFOLIO UTILIZATION*")
    lines.append(f"_{payload['ts']}_")
    lines.append("")

    # Wallet headroom
    lines.append("*WALLET HEADROOM*")
    wallet = payload["global"]["wallet"]
    if not wallet:
        lines.append("  _no balances available_")
    for asset, w in sorted(wallet.items(), key=lambda kv: -kv[1]["value_usd"]):
        used_pct = (w["used_now_usd"] / w["value_usd"]) if w["value_usd"] > 0 else 0.0
        lines.append(
            f"  {asset:<6} {_bar(used_pct)} {_fmt_pct(used_pct)} used   "
            f"{_fmt_usd(w['headroom_usd'])} free"
        )
    lines.append("")

    # Controllers
    lines.append("*CONTROLLERS*  (utilization vs nominal)")
    for c in payload["controllers"]:
        cap = c["capital"]
        cfg = c["config"]
        util = cap["utilization_now"]
        bar = _bar(util)
        warn = " ⚠️" if util > 1.0 else ""
        lines.append(
            f"  {c['config_name']:<24} {bar} {_fmt_pct(util)}{warn} · "
            f"alloc {_fmt_pct(cfg['portfolio_allocation'])} × "
            f"{cfg['max_active_executors_by_level']} lvl"
        )

    # Oversub
    alerts = payload["global"]["oversub_alerts"]
    if alerts:
        lines.append("")
        lines.append("⚠️ *OVERSUBSCRIPTION*")
        for a in alerts:
            lines.append(
                f"  {a['asset']} ratio {a['ratio']:.2f}× "
                f"({a['severity']}) — {len(a['controllers'])} ctrl(s)"
            )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def run(config: Config, context: ContextTypes.DEFAULT_TYPE) -> str:
    chat_id = context._chat_id if hasattr(context, "_chat_id") else None
    send_to = config.target_chat_id or chat_id

    client = await get_client(chat_id or send_to or 0, context=context)
    if not client:
        return "Could not connect to API server"

    bots_data = await _get_active_bots(client)
    if not bots_data:
        return "No active bots found."

    if config.bot_names:
        bots_data = {k: v for k, v in bots_data.items() if k in config.bot_names}

    # Fetch all configs in parallel
    bot_names = list(bots_data.keys())
    configs_per_bot = await asyncio.gather(
        *(_get_bot_configs(client, b) for b in bot_names)
    )

    # Build per-controller blocks (filter by controller_name)
    controllers: list[dict] = []
    connector_names_seen: set[str] = set()
    for bot_name, cfgs in zip(bot_names, configs_per_bot):
        bot_data = bots_data.get(bot_name) or {}
        perf_by_id = bot_data.get("performance", {}) if isinstance(bot_data, dict) else {}

        for cfg in cfgs:
            if not isinstance(cfg, dict):
                continue
            ctrl_name = cfg.get("controller_name", "")
            if config.controller_filter and ctrl_name != config.controller_filter:
                continue

            config_name = cfg.get("_config_name") or cfg.get("id", "")
            if not config_name:
                continue

            # Match performance by controller_id key (MQTT side may use id)
            perf = perf_by_id.get(config_name)
            if isinstance(perf, dict):
                # perf may be wrapped: {"performance": {...}}
                perf = perf.get("performance", perf)

            connector = cfg.get("connector_name", "")
            if connector:
                connector_names_seen.add(connector)

            controllers.append(_compute_controller_block(bot_name, config_name, cfg, perf))

    # Fetch balances (account_names defaulted to all if not specified)
    account_names = [config.account_name] if config.account_name else []
    balances_raw = await _get_balances(client, account_names, list(connector_names_seen))
    wallet_by_token = _flatten_balances(balances_raw)

    # Global block
    global_block = _compute_global_block(controllers, wallet_by_token, config.safety_margin)

    wallet_total = sum(w["value_usd"] for w in wallet_by_token.values())
    summary = _compute_summary(controllers, global_block, wallet_total)

    # Final payload (matches schema in CAPITAL_DYNAMICS.md)
    payload: dict[str, Any] = {
        "ts": _utc_iso(),
        "account": config.account_name or "all",
        "global": global_block,
        "controllers": controllers,
        "summary": summary,
    }

    # Send compact monitor to target chat if requested (separate channel —
    # full monitor render goes here without conflicting with the routine
    # handler's own truncated preview).
    if config.target_chat_id and context.bot is not None:
        text = _render_monitor(payload)
        try:
            await context.bot.send_message(
                chat_id=config.target_chat_id,
                text=text,
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.error("Failed to send monitor: %s", e)

    # Build the routine result.
    #   - `text` is ONLY the compact summary: short enough to render
    #     cleanly in the Telegram detail-view's 250-char truncation, and
    #     free of triple-backticks that would break the surrounding code
    #     fence inserted by the routines handler.
    #   - `sections` carries the structured payload for the web dashboard
    #     and for programmatic consumers (e.g. condor/tools/dashboard.py),
    #     which read `result.sections[0]["data"]` for the full payload
    #     instead of regex-extracting JSON from text.
    text = _render_compact_summary(payload)

    sections = [
        {"title": "payload", "data": payload},
        {"title": "summary", "data": payload.get("summary", {})},
        {"title": "wallet", "data": payload.get("global", {}).get("wallet", {})},
        {"title": "oversub_alerts",
         "data": payload.get("global", {}).get("oversub_alerts", [])},
        {"title": "controllers", "data": payload.get("controllers", [])},
    ]

    return RoutineResult(text=text, sections=sections)


def _utc_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
