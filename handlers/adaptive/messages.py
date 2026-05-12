"""Telegram message builder for `adaptive` proposal notifications.

Pure functions only — given an audit entry (the canonical dict described
in MEMORY_SPEC §F6) plus invariants metadata, return ``(text, keyboard)``
ready to ``bot.send_message(...)``.

Format follows ``.planning/strategy-framework/PROPOSE_MODE_SPEC.md`` §3.1.

Two things the agent builder needs to know:

1. **MarkdownV2 escaping**: Telegram is strict about backslashes for
   ``_ * [ ] ( ) ~ ` > # + - = | { } . !``. We use
   ``utils.telegram_formatters.escape_markdown_v2`` for any user-data
   text (controller_id, reasoning, etc.) and embed the formatted
   structure ourselves.

2. **Callback data length**: Telegram caps each ``callback_data`` to 64
   bytes. Pattern ``adaptive:<action>:<patch_id>:<candidate_id>`` —
   patch_id is ``p_<utc20chars>_<3digits>`` = 27 chars, plus the rest,
   well under the limit even for the longest action ("snooze:1h" = 9).
"""

from __future__ import annotations

from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from utils.telegram_formatters import escape_markdown_v2


# ---------------------------------------------------------------------------
# Callback data — single source of truth for all adaptive:* patterns
# ---------------------------------------------------------------------------

NAMESPACE = "adaptive"


def cb_apply(patch_id: str, candidate_id: str) -> str:
    return f"{NAMESPACE}:apply:{patch_id}:{candidate_id}"


def cb_reject(patch_id: str) -> str:
    return f"{NAMESPACE}:reject:{patch_id}"


def cb_snooze(patch_id: str, duration: str) -> str:
    return f"{NAMESPACE}:snooze:{patch_id}:{duration}"


# ---------------------------------------------------------------------------
# Text body (MarkdownV2)
# ---------------------------------------------------------------------------


def build_propose_text(entry: dict[str, Any]) -> str:
    """Format the proposal text. Returns a MarkdownV2-safe string.

    Layout follows PROPOSE_MODE_SPEC.md §3.1 (the long-form variant).
    A handful of fields are optional in case the agent didn't populate
    them (regime_observed, suboptimal_minutes, etc.) — we fall back
    to ``"n/a"`` so the message still renders.
    """

    def esc(s: Any) -> str:
        return escape_markdown_v2(str(s)) if s is not None else "n/a"

    proposal = entry.get("llm_proposal", {}) or {}
    bot_name = entry.get("bot_name", "?")
    controller_id = entry.get("controller_id", "?")
    regime = entry.get("regime_observed", "n/a")
    suboptimal_min = entry.get("suboptimal_minutes")
    dimension = proposal.get("dimension", "?")
    direction = proposal.get("direction", "?")
    magnitude = proposal.get("magnitude_qualitative", "?")
    reasoning = proposal.get("reasoning", "")
    caveats = proposal.get("caveats") or []
    candidates = entry.get("expanded_candidates") or []
    backtests = entry.get("backtests") or {}
    selected = entry.get("selected")
    selected_value = entry.get("selected_value")
    old_value = entry.get("old_value")

    lines: list[str] = []

    # Header
    lines.append(f"🤖 *ADAPTIVE PROPOSAL* — {esc(bot_name)}")
    lines.append("")
    lines.append(f"Controller: `{esc(controller_id)}`")
    if suboptimal_min is not None:
        lines.append(
            f"Régimen: {esc(regime)} \\(subóptimo: {esc(suboptimal_min)} min\\)"
        )
    else:
        lines.append(f"Régimen: {esc(regime)}")
    lines.append("")

    # LLM analysis
    lines.append("📊 *Análisis del LLM:*")
    lines.append(f"`{esc(dimension)}` · `{esc(direction)}` · `{esc(magnitude)}`")
    lines.append("")
    if reasoning:
        # Blockquote each line of reasoning
        for r_line in str(reasoning).strip().splitlines():
            lines.append(f">{esc(r_line)}")
        lines.append("")
    if caveats:
        lines.append("⚠️ " + esc(" · ".join(str(c) for c in caveats)))
        lines.append("")

    # Backtests table
    if candidates and backtests:
        lines.append("📈 *Backtests del período subóptimo:*")
        lines.append("")

        baseline = backtests.get("baseline") or {}
        b_pnl = baseline.get("net_pnl_quote")
        b_vol = baseline.get("volume")
        b_score = baseline.get("score")
        lines.append(
            f"`Baseline    " + _fmt_summary(b_pnl, b_vol, b_score) + "`"
        )

        for i, cand in enumerate(candidates, start=1):
            cid = cand.get("id", f"c{i}")
            cand_bt = backtests.get(f"candidate{i}") or backtests.get(cid) or {}
            pnl = cand_bt.get("net_pnl_quote")
            vol = cand_bt.get("volume")
            score = cand_bt.get("score")
            delta_rel = cand.get("delta_relative", "")
            marker = " ★" if cid == selected else "  "
            lines.append(
                f"`{cid}: {esc(delta_rel):<8s}" + _fmt_summary(pnl, vol, score) + f"{marker}`"
            )
        lines.append("")

    # Resolution
    if selected_value is not None:
        lines.append(
            f"Ganador: `{esc(dimension)}` {esc(old_value)} → {esc(selected_value)}"
        )

    return "\n".join(lines)


def _fmt_summary(pnl: Any, vol: Any, score: Any) -> str:
    """Format a single backtest row's numbers. Kept simple — Telegram
    monospace handles spacing."""
    def _num(x, fmt):
        return fmt.format(x) if isinstance(x, (int, float)) else "  n/a"

    pnl_s = _num(pnl, "{:+.2f}")
    vol_s = _num(vol, "{:>8.0f}")
    score_s = _num(score, "{:.2f}")
    return f"PnL {pnl_s} Vol {vol_s} Score {score_s}"


# ---------------------------------------------------------------------------
# Inline keyboard
# ---------------------------------------------------------------------------


def build_propose_keyboard(entry: dict[str, Any]) -> InlineKeyboardMarkup:
    """Two-row keyboard:

    - Row 1: ✅ Apply <winner> + alternative-apply buttons.
    - Row 2: ❌ Reject + ⏸ Snooze 1h.

    The "winner" is determined by ``entry['selected']`` (the patch
    contract guarantees this is set by the time the proposal reaches
    propose mode).
    """
    patch_id = entry["patch_id"]
    candidates = entry.get("expanded_candidates") or []
    selected = entry.get("selected")

    # Re-order: winner first, others preserve original order
    winner = next((c for c in candidates if c.get("id") == selected), None)
    others = [c for c in candidates if c.get("id") != selected]
    ordered = ([winner] if winner else []) + others

    row1: list[InlineKeyboardButton] = []
    for i, c in enumerate(ordered):
        cid = c.get("id", f"c{i}")
        label = f"✅ Apply {cid}" if i == 0 and winner else cid
        row1.append(InlineKeyboardButton(label, callback_data=cb_apply(patch_id, cid)))

    # Telegram limits ~8 buttons per row in practice. We won't exceed 3
    # candidates in MVP (PATCH_CONTRACT_SPEC). Belt-and-braces:
    row1 = row1[:8]

    row2 = [
        InlineKeyboardButton("❌ Reject", callback_data=cb_reject(patch_id)),
        InlineKeyboardButton("⏸ Snooze 1h", callback_data=cb_snooze(patch_id, "1h")),
    ]

    return InlineKeyboardMarkup([row1, row2])


# ---------------------------------------------------------------------------
# Verdict messages — what we edit the message to AFTER the human clicks
# ---------------------------------------------------------------------------


def build_applied_text(
    entry: dict[str, Any],
    *,
    candidate_id: str,
    new_value: Any,
    apply_result: dict[str, Any],
    cooldown_until: str | None = None,
) -> str:
    proposal = entry.get("llm_proposal", {}) or {}
    field = proposal.get("dimension", "?")
    old = entry.get("old_value")

    def esc(s):
        return escape_markdown_v2(str(s)) if s is not None else "n/a"

    lines = [
        f"✅ *Aplicado*: `{esc(field)}` {esc(old)} → {esc(new_value)}",
        "",
        f"Candidato elegido: `{esc(candidate_id)}`",
    ]
    caveats = apply_result.get("caveats") or []
    if caveats:
        lines.append("")
        for c in caveats:
            lines.append(f"⚠️ {esc(c)}")
    if cooldown_until:
        lines.append("")
        lines.append(f"Cooldown hasta {esc(cooldown_until)}\\.")
    lines.append("")
    lines.append("_Outcome será medido en 30 min\\._")
    return "\n".join(lines)


def build_rejected_text(entry: dict[str, Any], *, cooldown_minutes: float | None) -> str:
    proposal = entry.get("llm_proposal", {}) or {}
    field = proposal.get("dimension", "?")
    cid = entry.get("controller_id", "?")

    def esc(s):
        return escape_markdown_v2(str(s))

    out = [
        "❌ *Propuesta rechazada\\.*",
        "",
    ]
    if cooldown_minutes is not None:
        out.append(
            f"Cooldown de {esc(int(cooldown_minutes))} min para `{esc(field)}` en `{esc(cid)}`\\."
        )
    return "\n".join(out)


def build_snoozed_text(entry: dict[str, Any], *, snoozed_until_iso: str) -> str:
    def esc(s):
        return escape_markdown_v2(str(s))

    return (
        f"⏸ *Pospuesto* hasta {esc(snoozed_until_iso)}\n\n"
        f"_El agente puede volver a proponer después si el régimen persiste\\._"
    )


def build_failed_apply_text(entry: dict[str, Any], *, error: dict[str, Any] | str) -> str:
    """Apply succeeded validation but the MCP tool failed at the API."""
    if isinstance(error, dict):
        msg = error.get("message") or str(error.get("error_code") or error)
    else:
        msg = str(error)

    def esc(s):
        return escape_markdown_v2(str(s))

    return (
        "❌ *Apply falló al escribir en el bot\\.*\n\n"
        f"`{esc(msg)}`\n\n"
        "_El verdict quedó como `approved` pero el cambio NO se aplicó\\._"
    )


def build_already_resolved_text(entry: dict[str, Any]) -> str:
    verdict = entry.get("human_verdict", "?")
    verdict_at = entry.get("verdict_at", "n/a")

    def esc(s):
        return escape_markdown_v2(str(s))

    return (
        f"⚠️ Esta propuesta ya fue `{esc(verdict)}` a las {esc(verdict_at)}\\."
    )
