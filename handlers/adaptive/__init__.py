"""Adaptive Strategy Framework — Telegram propose-mode handler.

Routes ``adaptive:*`` inline-button callbacks. The agent loop (Phase 5.7)
generates proposals and calls :func:`send_proposal` to ship them out;
this module owns the round-trip from "human clicks Apply" to "the MCP
tool wrote the new value to the running bot".

Three callback actions (PROPOSE_MODE_SPEC §3.2):

- ``apply``:  ``adaptive:apply:<patch_id>:<candidate_id>``
- ``reject``: ``adaptive:reject:<patch_id>``
- ``snooze``: ``adaptive:snooze:<patch_id>:<duration>``  (``1h`` for MVP)

All state lives under ``trading_agents/<agent>/state/``:
- ``audit_log.jsonl`` — one line per proposal, fields filled in-place
  as the lifecycle advances.
- ``last_changes.json`` — keyed cooldown lookups for future ticks.

The handler **owns the side effects** (audit log update, last_changes
upsert, MCP tool call, message edit). It never raises — every error
path is shown to the human in the message edit.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path
from typing import Any

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import CallbackQueryHandler, ContextTypes

from condor.trading_agent.adaptive import audit as audit_io
from condor.trading_agent.adaptive import invariants as inv_loader
from condor.trading_agent.adaptive import state_io

from . import messages as msg

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Agent discovery — find the agent dir that owns a given patch_id
# ---------------------------------------------------------------------------


_AGENTS_ROOT = Path(__file__).resolve().parent.parent.parent / "trading_agents"


def _candidate_agent_dirs() -> list[Path]:
    """Return all agent dirs that look like adaptive-framework agents.

    Heuristic: directory under ``trading_agents/`` with an
    ``invariants.yaml`` file. The adaptive framework requires it; legacy
    agents never have one.
    """
    if not _AGENTS_ROOT.exists():
        return []
    return [
        d for d in sorted(_AGENTS_ROOT.iterdir())
        if d.is_dir() and (d / "invariants.yaml").exists()
    ]


def _resolve_agent_for_patch(patch_id: str) -> tuple[Path, dict[str, Any]] | None:
    """Find (agent_dir, entry) that owns this patch_id, scanning the
    audit_log.jsonl of every adaptive agent. Returns ``None`` if not found.

    O(N agentes * M entries). With 1-2 adaptive agents in practice this
    is trivially cheap. If it ever becomes a bottleneck, an index
    keyed by patch_id would solve it without changing the API.
    """
    for agent_dir in _candidate_agent_dirs():
        entry = audit_io.find_audit_entry(agent_dir, patch_id)
        if entry is not None:
            return agent_dir, entry
    return None


# ---------------------------------------------------------------------------
# Public API used by the agent loop (Phase 5.7)
# ---------------------------------------------------------------------------


async def send_proposal(
    bot,
    agent_dir: Path,
    entry: dict[str, Any],
    *,
    chat_id: int,
) -> dict[str, Any]:
    """Append a new proposal to ``audit_log`` and send the Telegram
    message.

    Returns the entry as it sits on disk after the send (with
    ``telegram_message_id`` and ``human_verdict='pending'`` populated).
    """
    # Defaults the spec considers part of "fresh proposal" state.
    entry.setdefault("human_verdict", "pending")
    entry.setdefault("verdict_at", None)
    entry.setdefault("verdict_chose_candidate", None)
    entry.setdefault("verdict_by", None)
    entry.setdefault("applied", False)
    entry.setdefault("applied_at", None)
    entry.setdefault("apply_result", None)
    entry.setdefault("apply_error", None)
    entry.setdefault("outcome_30min", None)
    entry.setdefault("ts", state_io.utc_iso())

    audit_io.append_audit_entry(agent_dir, entry)

    text = msg.build_propose_text(entry)
    keyboard = msg.build_propose_keyboard(entry)

    sent = await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=keyboard,
    )

    audit_io.update_audit_entry(agent_dir, entry["patch_id"], {
        "telegram_chat_id": chat_id,
        "telegram_message_id": sent.message_id,
    })
    entry["telegram_chat_id"] = chat_id
    entry["telegram_message_id"] = sent.message_id
    return entry


# ---------------------------------------------------------------------------
# Callback handler — registered with pattern="^adaptive:"
# ---------------------------------------------------------------------------


async def adaptive_callback_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Route ``adaptive:<action>:<patch_id>[:<args>]`` callbacks.

    Always answers the callback query immediately (Telegram requires
    an ack within ~30s) and edits the original message to reflect the
    final state. Errors are surfaced in the edited message — never
    raised.
    """
    query = update.callback_query
    if query is None:
        return
    await query.answer()

    data = query.data or ""
    parts = data.split(":", 4)
    if len(parts) < 3 or parts[0] != msg.NAMESPACE:
        # Defensive: the pattern guard should prevent this, but
        # belt-and-braces — the only safe move is to silently drop.
        logger.warning("adaptive_callback: unexpected data: %r", data)
        return

    action = parts[1]
    patch_id = parts[2]
    args = parts[3:]

    resolved = _resolve_agent_for_patch(patch_id)
    if resolved is None:
        try:
            await query.edit_message_text(
                "❌ Propuesta no encontrada \\(¿expirada o archivada?\\)\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        except Exception:  # editing may fail (e.g. message gone), ignore
            pass
        return

    agent_dir, entry = resolved

    # Double-click / already-resolved guard
    if entry.get("human_verdict") not in (None, "pending"):
        try:
            await query.edit_message_text(
                msg.build_already_resolved_text(entry),
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        except Exception:
            pass
        return

    user_id = update.effective_user.id if update.effective_user else None

    if action == "apply":
        if not args:
            await _safe_edit(query, "❌ Falta candidate\\_id en el callback\\.")
            return
        await _handle_apply(query, context, agent_dir, entry, candidate_id=args[0], by=user_id)
    elif action == "reject":
        await _handle_reject(query, agent_dir, entry, by=user_id)
    elif action == "snooze":
        duration = args[0] if args else "1h"
        await _handle_snooze(query, agent_dir, entry, duration=duration, by=user_id)
    else:
        await _safe_edit(query, f"❌ Acción desconocida: {action}")


# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------


async def _handle_apply(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    agent_dir: Path,
    entry: dict[str, Any],
    *,
    candidate_id: str,
    by: int | None,
) -> None:
    """Apply the chosen candidate via the MCP tool, then update state."""

    chosen = next(
        (c for c in (entry.get("expanded_candidates") or [])
         if c.get("id") == candidate_id),
        None,
    )
    if chosen is None:
        await _safe_edit(query, f"❌ Candidato `{candidate_id}` no encontrado\\.")
        return

    now = state_io.utc_now()
    audit_io.update_audit_entry(agent_dir, entry["patch_id"], {
        "human_verdict": "approved",
        "verdict_at": state_io.utc_iso(now),
        "verdict_chose_candidate": candidate_id,
        "verdict_by": by,
    })

    proposal = entry.get("llm_proposal", {}) or {}
    field = proposal.get("dimension")
    bot_name = entry.get("bot_name")
    controller_id_full = entry.get("controller_id", "")
    # MCP tool expects the controller_id as the YAML config name (the part
    # after the ``::`` in the canonical id).
    if "::" in controller_id_full:
        controller_id_short = controller_id_full.split("::", 1)[1]
    else:
        controller_id_short = controller_id_full

    new_value = chosen.get("value")
    old_value = entry.get("old_value")

    # Lazy-import so we don't pay the cost on module import (and so the
    # MCP server can stay an optional dep when running tests).
    from mcp_servers.hummingbot_api.tools.adaptive_agent import (
        update_controller_config,
    )

    client = await _get_hb_client(context)
    if client is None:
        audit_io.update_audit_entry(agent_dir, entry["patch_id"], {
            "applied": False,
            "apply_error": "could not obtain Hummingbot client",
        })
        await _safe_edit(query, msg.build_failed_apply_text(
            entry, error="could not obtain Hummingbot client"
        ))
        return

    try:
        api_result = await update_controller_config(
            client=client,
            bot_name=bot_name,
            controller_id=controller_id_short,
            field=field,
            value=new_value,
            expected_old_value=old_value,
        )
    except Exception as e:  # bug, not domain error — domain returns dict
        logger.exception("adaptive_callback: apply raised")
        audit_io.update_audit_entry(agent_dir, entry["patch_id"], {
            "applied": False,
            "apply_error": str(e),
        })
        await _safe_edit(query, msg.build_failed_apply_text(entry, error=str(e)))
        return

    if not api_result.get("success"):
        # Domain error — surface it in the message + audit log.
        audit_io.update_audit_entry(agent_dir, entry["patch_id"], {
            "applied": False,
            "apply_error": api_result,
        })
        await _safe_edit(query, msg.build_failed_apply_text(entry, error=api_result))
        return

    audit_io.update_audit_entry(agent_dir, entry["patch_id"], {
        "applied": True,
        "applied_at": state_io.utc_iso(now),
        "apply_result": api_result,
    })
    audit_io.upsert_last_change(
        agent_dir,
        controller_id=controller_id_full,
        field=field,
        old_value=old_value,
        new_value=new_value,
        patch_id=entry["patch_id"],
        ts=now,
        marker="applied",
    )

    cooldown_end = _cooldown_deadline_after(agent_dir, controller_id_full, field)

    await _safe_edit(query, msg.build_applied_text(
        entry,
        candidate_id=candidate_id,
        new_value=new_value,
        apply_result=api_result,
        cooldown_until=cooldown_end,
    ))


async def _handle_reject(
    query,
    agent_dir: Path,
    entry: dict[str, Any],
    *,
    by: int | None,
) -> None:
    now = state_io.utc_now()
    audit_io.update_audit_entry(agent_dir, entry["patch_id"], {
        "human_verdict": "rejected",
        "verdict_at": state_io.utc_iso(now),
        "verdict_by": by,
    })

    proposal = entry.get("llm_proposal", {}) or {}
    field = proposal.get("dimension")
    controller_id_full = entry.get("controller_id", "")
    old_value = entry.get("old_value")

    # Reject also writes last_changes so cooldown kicks in.
    audit_io.upsert_last_change(
        agent_dir,
        controller_id=controller_id_full,
        field=field,
        old_value=old_value,
        new_value=old_value,
        patch_id=entry["patch_id"],
        ts=now,
        marker="rejected",
    )

    cooldown_min = _per_field_cooldown_minutes(agent_dir)
    await _safe_edit(query, msg.build_rejected_text(entry, cooldown_minutes=cooldown_min))


async def _handle_snooze(
    query,
    agent_dir: Path,
    entry: dict[str, Any],
    *,
    duration: str,
    by: int | None,
) -> None:
    delta = _parse_duration(duration)
    if delta is None:
        await _safe_edit(query, f"❌ Duración inválida: `{duration}`\\.")
        return

    now = state_io.utc_now()
    snoozed_until = now + delta

    audit_io.update_audit_entry(agent_dir, entry["patch_id"], {
        "human_verdict": "snoozed",
        "verdict_at": state_io.utc_iso(now),
        "snoozed_until_ts": state_io.utc_iso(snoozed_until),
        "verdict_by": by,
    })

    proposal = entry.get("llm_proposal", {}) or {}
    field = proposal.get("dimension")
    controller_id_full = entry.get("controller_id", "")
    old_value = entry.get("old_value")

    audit_io.upsert_last_change(
        agent_dir,
        controller_id=controller_id_full,
        field=field,
        old_value=old_value,
        new_value=old_value,
        patch_id=entry["patch_id"],
        ts=now,
        marker="snoozed",
        cooldown_until=snoozed_until,
    )

    await _safe_edit(query, msg.build_snoozed_text(
        entry, snoozed_until_iso=state_io.utc_iso(snoozed_until)
    ))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_duration(s: str) -> timedelta | None:
    """``"1h"`` → ``timedelta(hours=1)``. None on bad input. Accepts
    m, h, d for MVP."""
    if not s or len(s) < 2:
        return None
    unit = s[-1].lower()
    try:
        n = float(s[:-1])
    except ValueError:
        return None
    if n < 0:
        return None
    if unit == "m":
        return timedelta(minutes=n)
    if unit == "h":
        return timedelta(hours=n)
    if unit == "d":
        return timedelta(days=n)
    return None


def _per_field_cooldown_minutes(agent_dir: Path) -> float | None:
    """Load invariants.yaml and return per_controller_per_field_minutes."""
    try:
        inv = inv_loader.load_invariants(agent_dir / "invariants.yaml")
        return inv.cooldowns.get("per_controller_per_field_minutes")
    except Exception as e:
        logger.warning("adaptive_callback: invariants load failed: %s", e)
        return None


def _cooldown_deadline_after(
    agent_dir: Path, controller_id: str, field: str
) -> str | None:
    try:
        inv = inv_loader.load_invariants(agent_dir / "invariants.yaml")
    except Exception:
        return None
    end = audit_io.compute_cooldown_end(
        agent_dir, controller_id, field,
        per_controller_per_field_minutes=inv.cooldowns["per_controller_per_field_minutes"],
        per_controller_minutes=inv.cooldowns["per_controller_minutes"],
        global_minutes=inv.cooldowns["global_minutes"],
    )
    return state_io.utc_iso(end) if end else None


async def _get_hb_client(context: ContextTypes.DEFAULT_TYPE):
    """Resolve a Hummingbot API client from the Telegram context.

    Uses Condor's ``config_manager.get_client`` keyed on chat_id, same
    as the rest of the handlers. Returns ``None`` if no client is
    available.
    """
    try:
        from config_manager import get_client
    except ImportError as e:
        logger.error("adaptive_callback: get_client unavailable: %s", e)
        return None

    chat_id = getattr(context, "_chat_id", None) or 0
    try:
        return await get_client(chat_id, context=context)
    except Exception as e:
        logger.exception("adaptive_callback: client error: %s", e)
        return None


async def _safe_edit(query, text: str) -> None:
    """Edit the message, swallowing any rendering / API error."""
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        logger.warning("adaptive_callback: edit_message_text failed: %s", e)


# ---------------------------------------------------------------------------
# Registration helper
# ---------------------------------------------------------------------------


def get_callback_handler() -> CallbackQueryHandler:
    """Return the CallbackQueryHandler ready to register in main.py."""
    return CallbackQueryHandler(adaptive_callback_handler, pattern="^adaptive:")
