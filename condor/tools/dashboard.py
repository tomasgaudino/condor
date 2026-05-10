"""Adaptive Strategy Framework — CLI dashboard.

Usage:
    python -m condor.tools.dashboard [--server local] [--account master_account]

Connects to a Condor server, runs the `capital_state` routine, and prints
a one-screen summary of capital across controllers and wallet. Read-only.
No Telegram, no agent state — just the live snapshot.

This is the MVP-of-MVP dashboard: only the CAPITAL section is rendered.
The full dashboard (PERFORMANCE / AGENT ACTIVITY / MARKET REGIME / TECH
HEALTH) lands when those KPIs are wired up.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Any

from routines.capital_state import Config as CapitalStateConfig
from routines.capital_state import _bar, _fmt_pct, _fmt_usd

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


HEADER = "═" * 65
SECTION = "─" * 65


def render_dashboard(payload: dict[str, Any]) -> str:
    """Build the textual dashboard from a capital_state payload."""
    lines: list[str] = []
    lines.append(HEADER)
    lines.append(" ADAPTIVE STRATEGY FRAMEWORK · DASHBOARD (capital-only MVP)")
    lines.append(HEADER)
    lines.append("")
    lines.append(f"  Snapshot: {payload['ts']}")
    lines.append(f"  Account:  {payload['account']}")
    lines.append("")

    # CAPITAL section
    lines.append("┌─ CAPITAL ─────────────────────────────────────────────────────")
    summary = payload["summary"]

    if summary["total_controllers"] == 0:
        lines.append("│  (no controllers in scope)")
        lines.append("└" + SECTION)
        return "\n".join(lines)

    lines.append(
        f"│  Controllers: {summary['total_controllers']}    "
        f"Nominal: {_fmt_usd(summary['total_nominal_budget_usd'])}    "
        f"Committed: {_fmt_usd(summary['total_committed_now_usd'])}"
    )
    lines.append(
        f"│  Worst-case: {_fmt_usd(summary['total_worst_case_usd'])}    "
        f"Wallet total: {_fmt_usd(summary['wallet_total_value_usd'])}"
    )
    lines.append("│")

    # Wallet headroom
    wallet = payload["global"]["wallet"]
    if wallet:
        lines.append("│  WALLET HEADROOM")
        for asset, w in sorted(wallet.items(), key=lambda kv: -kv[1]["value_usd"]):
            used_pct = (
                w["used_now_usd"] / w["value_usd"] if w["value_usd"] > 0 else 0.0
            )
            lines.append(
                f"│    {asset:<6} {_bar(used_pct)} {_fmt_pct(used_pct):>4} used   "
                f"{_fmt_usd(w['headroom_usd']):>10} free"
            )
        lines.append("│")

    # Per-controller
    lines.append("│  CONTROLLERS  (utilization vs nominal)")
    for c in payload["controllers"]:
        cap = c["capital"]
        cfg = c["config"]
        util = cap["utilization_now"]
        warn = " ⚠" if util > 1.0 else "  "
        lines.append(
            f"│    {c['config_name']:<28.28} {_bar(util)} {_fmt_pct(util):>4}{warn} "
            f"{cfg['max_active_executors_by_level']:>2} lvl · "
            f"alloc {_fmt_pct(cfg['portfolio_allocation'])}"
        )

    # Oversub alerts
    alerts = payload["global"]["oversub_alerts"]
    if alerts:
        lines.append("│")
        lines.append("│  ⚠️  OVERSUBSCRIPTION")
        for a in alerts:
            lines.append(
                f"│    {a['asset']} ratio {a['ratio']:.2f}× "
                f"({a['severity']}) — {len(a['controllers'])} ctrl(s)"
            )

    lines.append("└" + SECTION)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Routine invocation
# ---------------------------------------------------------------------------


async def _run_capital_state(
    server_name: str,
    account_name: str | None,
    bot_names: list[str],
    controller_filter: str,
) -> dict[str, Any]:
    """Run the capital_state routine and return the payload dict.

    Uses the same WebRoutineContext that the web/api uses, so we don't
    need a Telegram bot to invoke it.
    """
    # Imports kept local so this module is importable without telegram libs
    # being needed at import time (we only need them at run time).
    from condor.routine_store import WebRoutineContext
    from routines.capital_state import run as capital_state_run

    context = WebRoutineContext(server_name=server_name)
    config = CapitalStateConfig(
        account_name=account_name,
        bot_names=bot_names,
        controller_filter=controller_filter,
        target_chat_id=None,  # never send to Telegram from the CLI
    )

    # The routine returns a RoutineResult whose `sections[0]` ("payload")
    # carries the full structured payload. Fall back to text-extraction
    # for older string-returning shapes.
    result = await capital_state_run(config, context)
    if hasattr(result, "sections") and result.sections:
        for sec in result.sections:
            if sec.get("title") == "payload":
                return sec["data"]
    raw = result.text if hasattr(result, "text") else str(result)
    return _extract_payload(raw)


def _extract_payload(raw: str) -> dict[str, Any]:
    """Pull the JSON block out of the routine's text output."""
    import json

    marker = "```json\n"
    idx = raw.find(marker)
    if idx < 0:
        # No JSON in the output — likely an error message
        return {
            "ts": "",
            "account": "?",
            "global": {"wallet": {}, "oversub_alerts": []},
            "controllers": [],
            "summary": {
                "total_controllers": 0,
                "total_nominal_budget_usd": 0.0,
                "total_committed_now_usd": 0.0,
                "total_worst_case_usd": 0.0,
                "wallet_total_value_usd": 0.0,
                "any_oversub": False,
                "max_oversub_ratio": 0.0,
            },
            "_error": raw,
        }

    end = raw.rfind("```")
    json_str = raw[idx + len(marker) : end].strip()
    return json.loads(json_str)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="condor.tools.dashboard",
        description="Adaptive framework dashboard (capital-only MVP).",
    )
    parser.add_argument(
        "--server",
        default="local",
        help="Condor server name (default: local)",
    )
    parser.add_argument(
        "--account",
        default=None,
        help="Hummingbot account name (default: all accounts in the server)",
    )
    parser.add_argument(
        "--bot",
        action="append",
        dest="bots",
        default=[],
        help="Filter to specific bot (repeatable). Default: all active bots.",
    )
    parser.add_argument(
        "--controller-filter",
        default="pmm_mister",
        help="Filter by controller_name (default: pmm_mister, empty = all)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable info-level logging",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        payload = asyncio.run(
            _run_capital_state(
                server_name=args.server,
                account_name=args.account,
                bot_names=args.bots,
                controller_filter=args.controller_filter,
            )
        )
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if "_error" in payload:
        print(f"ERROR: {payload['_error']}", file=sys.stderr)
        return 2

    print(render_dashboard(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
