"""Controller Performance — per-controller PnL/volume/diagnostics over multiple windows.

Third MVP routine of the Adaptive Strategy Framework. Snapshots each
active controller's performance, persists a minimal time-series of
core metrics, and derives velocities + a diagnostic that tells the
agent whether the controller is in a *subóptimo* period (D9 trigger).

Spec:
    `.planning/strategy-framework/CONTROLLER_PERFORMANCE_SPEC.md`

Output contract:
    `.planning/strategy-framework/AGENT_VS_USER_VIEW.md` §controller_performance.

Multi-controller by design (mirrors `market_regime` multi-pair):
- `controller_ids=[]` → autodetect all active controllers.
- Explicit list → filter to those.
- Aggregated user view (KPIs + summary table + per-controller sub-sections).
- Per-controller agent sections under `title="agent:<canonical_id>"`,
  plus a cross-controller `agent:summary`.

Persistence (state_io JSONL, 30d rotation):
    state/controller_performance/<canonical_id_sanitized>.jsonl
    {"ts":"...","r_pnl":...,"u_pnl":...,"vol":...,"pos_closed":...}

Deviations from spec (validated against brigado, 2026-05-12):
- `total_positions` does not exist on the snapshot. Derived as
  `sum(close_type_counts) + len(positions_summary)`.
- `accuracy` does not exist. `null` in MVP.
- `close_type_counts` arrives with enum-string keys
  (`"CloseType.TAKE_PROFIT"`). Stripped to `take_profit`.
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from telegram.ext import ContextTypes

from condor.trading_agent.adaptive import state_io
from config_manager import get_client
from routines.base import RoutineResult

logger = logging.getLogger(__name__)

CATEGORY = "Adaptive Framework"


# ---------------------------------------------------------------------------
# Paths / persistence
# ---------------------------------------------------------------------------

# Lives in repo root `state/`. Will migrate to
# `trading_agents/<agent>/state/` once Phase 5.5 lands.
STATE_DIR = Path("state") / "controller_performance"
HISTORY_RETENTION_DAYS = 30


def _history_path(canonical_id: str) -> Path:
    return STATE_DIR / f"{state_io.canonical_id_to_filename(canonical_id)}.jsonl"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class Config(BaseModel):
    """Performance metrics per controller, multi-window comparison."""

    controller_ids: list[str] = Field(
        default=[],
        description=(
            "Canonical controller IDs (`{bot_name}::{config_name}`) to "
            "process. Empty = autodetect all active controllers."
        ),
    )
    bot_names: list[str] = Field(
        default=[],
        description="Optional filter by bot name before autodetect.",
    )

    # Ventanas (default del spec)
    windows_hours: list[float] = Field(default=[1.0, 6.0, 24.0])

    # Cómputos opcionales
    compute_market_share: bool = Field(default=True)

    # Thresholds del disparador subóptimo
    pnl_flat_threshold: float = Field(default=0.001)
    volume_drop_ratio: float = Field(default=0.5)
    stuck_minutes_threshold: float = Field(default=60.0)
    suboptimal_min_persistence_minutes: float = Field(default=30.0)

    # Persistencia: si False, no escribe historia (modo "read-only").
    # Útil para inspección sin polucionar el state dir.
    persist_snapshot: bool = Field(default=True)


# ---------------------------------------------------------------------------
# Helpers — close_type normalization, snapshot extraction
# ---------------------------------------------------------------------------


def _strip_close_type(key: str) -> str:
    """`CloseType.TAKE_PROFIT` → `take_profit`. Keeps unknown keys as-is."""
    if not isinstance(key, str):
        return str(key)
    if key.startswith("CloseType."):
        return key[len("CloseType."):].lower()
    return key.lower()


def _normalize_close_type_counts(raw: Any) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    return {_strip_close_type(k): int(v) for k, v in raw.items()}


def _dominant_close_type(counts: dict[str, int]) -> str | None:
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: kv[1])[0]


def _extract_snapshot(perf: dict[str, Any]) -> dict[str, Any]:
    """Pull the numeric fields needed for windowing + diagnostics.

    Input: the per-controller `performance` dict as returned by
    `bot_orchestration.get_active_bots_status`.
    """
    counts = _normalize_close_type_counts(perf.get("close_type_counts"))
    positions_open = len(perf.get("positions_summary") or [])
    r_pnl = float(perf.get("realized_pnl_quote", 0) or 0)
    u_pnl = float(perf.get("unrealized_pnl_quote", 0) or 0)
    vol = float(perf.get("volume_traded", 0) or 0)
    pos_closed = sum(counts.values())

    return {
        "realized_pnl_quote": r_pnl,
        "unrealized_pnl_quote": u_pnl,
        "net_pnl_quote": r_pnl + u_pnl,
        "volume_traded": vol,
        "positions_open": positions_open,
        "positions_closed": pos_closed,
        "close_type_counts": counts,
        "close_types_dominant": _dominant_close_type(counts),
    }


def _persist_snapshot(canonical_id: str, ts: datetime, snap: dict) -> None:
    """Append the minimal time-series row + truncate >30d. Best-effort."""
    path = _history_path(canonical_id)
    entry = {
        "ts": state_io.parse_ts.__self__ if False else ts.strftime("%Y-%m-%dT%H:%M:%SZ"),  # noqa: F841
        "r_pnl": snap["realized_pnl_quote"],
        "u_pnl": snap["unrealized_pnl_quote"],
        "vol": snap["volume_traded"],
        "pos_closed": snap["positions_closed"],
    }
    # The `ts` above is intentionally constructed manually so we don't
    # rely on state_io.utc_now() inside the routine (we already have
    # `ts` from the caller — consistent across all controllers in one
    # Run).
    entry["ts"] = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        state_io.append_jsonl(path, entry)
        cutoff = ts - timedelta(days=HISTORY_RETENTION_DAYS)
        state_io.truncate_jsonl_older_than(path, cutoff)
    except Exception as e:
        logger.warning("controller_performance: persist snapshot failed for %s: %s", canonical_id, e)


# ---------------------------------------------------------------------------
# Window computation
# ---------------------------------------------------------------------------


def _find_snapshot_before(history: list[dict], cutoff: datetime) -> dict | None:
    """Return the closest history entry with ts <= cutoff, or None.

    History is expected to be ascending by ts (state_io.append_jsonl
    appends in order). We walk backwards.
    """
    for entry in reversed(history):
        try:
            ts = state_io.parse_ts(entry["ts"])
        except Exception:
            continue
        if ts <= cutoff:
            return entry
    return None


def _compute_window(
    current_snap: dict,
    history: list[dict],
    now: datetime,
    window_hours: float,
) -> dict[str, Any]:
    """Velocities for a single window. Returns the per-window dict.

    `available=False` when there isn't enough history to compare. Counts
    samples between window_start and now for transparency.
    """
    cutoff = now - timedelta(hours=window_hours)
    target = _find_snapshot_before(history, cutoff)
    if target is None:
        return {
            "available": False,
            "samples": 0,
            "realized_pnl_quote": current_snap["realized_pnl_quote"],
            "unrealized_pnl_quote": current_snap["unrealized_pnl_quote"],
            "net_pnl_quote": current_snap["net_pnl_quote"],
            "volume_traded": current_snap["volume_traded"],
            "positions_closed": current_snap["positions_closed"],
            "pnl_velocity": None,
            "volume_velocity": None,
            "fills_per_hour": None,
        }

    try:
        target_ts = state_io.parse_ts(target["ts"])
    except Exception:
        return {"available": False, "samples": 0}

    elapsed_h = (now - target_ts).total_seconds() / 3600.0
    if elapsed_h <= 0:
        return {"available": False, "samples": 0}

    delta_r = current_snap["realized_pnl_quote"] - float(target.get("r_pnl", 0) or 0)
    delta_u = current_snap["unrealized_pnl_quote"] - float(target.get("u_pnl", 0) or 0)
    delta_vol = current_snap["volume_traded"] - float(target.get("vol", 0) or 0)
    delta_pos = current_snap["positions_closed"] - int(target.get("pos_closed", 0) or 0)

    # Count samples in window
    samples = 0
    for e in history:
        try:
            t = state_io.parse_ts(e["ts"])
        except Exception:
            continue
        if t >= target_ts:
            samples += 1

    return {
        "available": True,
        "samples": samples,
        "elapsed_hours": elapsed_h,
        "realized_pnl_quote": current_snap["realized_pnl_quote"],
        "unrealized_pnl_quote": current_snap["unrealized_pnl_quote"],
        "net_pnl_quote": current_snap["net_pnl_quote"],
        "volume_traded": current_snap["volume_traded"],
        "positions_closed": current_snap["positions_closed"],
        "pnl_velocity": (delta_r + delta_u) / elapsed_h,
        "volume_velocity": delta_vol / elapsed_h,
        "fills_per_hour": delta_pos / elapsed_h,
    }


# ---------------------------------------------------------------------------
# Cold-start (F3)
# ---------------------------------------------------------------------------


def _warmup_status(history: list[dict], now: datetime) -> tuple[float, str, str]:
    """Returns (effective_window_hours, status, effective_window_key).

    Mirrors the cold-start handling in CONTROLLER_PERFORMANCE_SPEC.md §F3
    with one tweak: we map the effective window to one of the
    requested windows (the largest one that fits inside the history age)
    so the consumer can look it up by key.
    """
    if len(history) < 3:
        return 0.0, "cold_start", "n/a"
    try:
        oldest_ts = state_io.parse_ts(history[0]["ts"])
    except Exception:
        return 0.0, "cold_start", "n/a"
    age_h = (now - oldest_ts).total_seconds() / 3600.0
    if age_h < 0.5:
        return 0.0, "cold_start", "n/a"
    if age_h < 4.0:
        return age_h, "adaptive", _window_key(age_h)
    return 4.0, "normal", "last_4h"


def _window_key(hours: float) -> str:
    return f"last_{int(round(hours))}h" if hours >= 1 else f"last_{hours:.1f}h"


# ---------------------------------------------------------------------------
# Time since last fill
# ---------------------------------------------------------------------------


def _time_since_last_fill_minutes(
    current_pos_closed: int, history: list[dict], now: datetime
) -> float | None:
    """Minutes since `positions_closed` last increased.

    Walks backwards through history; the first entry where pos_closed
    is strictly less than current marks the moment the latest fill
    happened (between that entry and the next one). Returns None if
    history is empty or pos_closed never moved.
    """
    if not history:
        return None
    for entry in reversed(history):
        prev = int(entry.get("pos_closed", 0) or 0)
        if prev < current_pos_closed:
            try:
                ts = state_io.parse_ts(entry["ts"])
            except Exception:
                return None
            return max(0.0, (now - ts).total_seconds() / 60.0)
    # No movement: take age of oldest entry as lower bound.
    try:
        oldest = state_io.parse_ts(history[0]["ts"])
        return (now - oldest).total_seconds() / 60.0
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Diagnostic
# ---------------------------------------------------------------------------


def _diagnose(
    snap: dict,
    windows: dict[str, dict],
    nominal_budget_usd: float,
    warmup_status: str,
    effective_window_key: str,
    time_since_last_fill_min: float | None,
    cfg: Config,
) -> dict[str, Any]:
    """Build the `diagnostic` block."""
    eff = windows.get(effective_window_key) or {}
    pnl_v = eff.get("pnl_velocity")
    vol_v_1h = (windows.get("last_1h") or {}).get("volume_velocity")
    vol_v_24h = (windows.get("last_24h") or {}).get("volume_velocity")

    # is_pnl_flat — uses |pnl_velocity_norm| < threshold (per spec).
    pnl_v_norm = (pnl_v / nominal_budget_usd) if (pnl_v is not None and nominal_budget_usd > 0) else None
    is_pnl_flat = pnl_v_norm is not None and abs(pnl_v_norm) < cfg.pnl_flat_threshold

    # is_volume_dropping — short window < ratio × long window.
    is_volume_dropping = (
        vol_v_1h is not None and vol_v_24h is not None
        and vol_v_24h > 0
        and vol_v_1h < cfg.volume_drop_ratio * vol_v_24h
    )

    # is_stuck — no fills for stuck_minutes_threshold AND vol_velocity_1h very low.
    is_stuck = (
        time_since_last_fill_min is not None
        and time_since_last_fill_min > cfg.stuck_minutes_threshold
        and (vol_v_1h is None or (vol_v_24h is not None and vol_v_24h > 0 and vol_v_1h < 0.1 * vol_v_24h))
    )

    # `suboptimal_now` requires NOT cold_start AND pnl <= 0 AND vol dropping AND
    # persistence (which lives outside the routine, so we expose
    # the individual flags + a simpler 'suboptimal_signals_now').
    suboptimal_signals_now = warmup_status != "cold_start" and (
        (pnl_v is not None and pnl_v <= 0) and is_volume_dropping
    )

    # Persistence: trivial in this MVP — we expose 0 unless the agent
    # tracks transitions externally. Future: read regime_history-like
    # state for suboptimal periods. Placeholder.
    suboptimal_period_minutes = 0

    return {
        "is_pnl_flat": bool(is_pnl_flat),
        "is_volume_dropping": bool(is_volume_dropping),
        "is_stuck": bool(is_stuck),
        "suboptimal_signals_now": bool(suboptimal_signals_now),
        "suboptimal_now": bool(
            suboptimal_signals_now
            and suboptimal_period_minutes >= cfg.suboptimal_min_persistence_minutes
        ),
        "suboptimal_period_minutes": suboptimal_period_minutes,
        "warmup_status": warmup_status,
        "effective_window_key": effective_window_key,
        "effective_window_hours": _effective_hours(effective_window_key, windows),
        "close_types_dominant": snap.get("close_types_dominant"),
        "time_since_last_fill_minutes": time_since_last_fill_min,
        "pnl_velocity_normalized": pnl_v_norm,
    }


def _effective_hours(key: str, windows: dict) -> float | None:
    w = windows.get(key)
    if isinstance(w, dict) and "elapsed_hours" in w:
        return float(w["elapsed_hours"])
    return None


# ---------------------------------------------------------------------------
# Per-controller pipeline
# ---------------------------------------------------------------------------


def _compute_nominal_budget(cfg_raw: dict) -> float:
    tot = float(cfg_raw.get("total_amount_quote", 0) or 0)
    alloc = float(cfg_raw.get("portfolio_allocation", 0) or 0)
    return tot * alloc


async def _maybe_fetch_market_share(
    client, connector: str, pair: str, controller_vol_24h: float
) -> dict[str, Any]:
    """Fetch 24x 1h candles, sum quote_asset_volume, compute market_share.

    Returns {} on failure — market_cross is optional.
    """
    try:
        raw = await client.market_data.get_candles(
            connector_name=connector, trading_pair=pair, interval="1h", max_records=24,
        )
    except Exception as e:
        logger.warning(
            "controller_performance: get_candles failed for %s %s: %s",
            connector, pair, e,
        )
        return {}
    if not isinstance(raw, list) or not raw:
        return {}
    exchange_vol = sum(float(c.get("quote_asset_volume", 0) or 0) for c in raw)
    share = (controller_vol_24h / exchange_vol) if exchange_vol > 0 else None
    return {
        "exchange_volume_24h_quote": exchange_vol,
        "controller_volume_24h_quote": controller_vol_24h,
        "market_share_24h": share,
    }


def _build_controller_payload(
    *,
    canonical_id: str,
    bot_name: str,
    config_name: str,
    cfg_raw: dict,
    perf: dict,
    history: list[dict],
    now: datetime,
    cfg: Config,
    market_cross: dict,
) -> dict[str, Any]:
    snap = _extract_snapshot(perf)

    windows: dict[str, dict] = {}
    for hours in cfg.windows_hours:
        key = _window_key(hours)
        windows[key] = _compute_window(snap, history, now, hours)

    effective_h, warmup, eff_key = _warmup_status(history, now)
    # If `last_4h` was requested as effective but not in cfg.windows_hours,
    # compute it on the fly so diagnostic doesn't reference a missing window.
    if eff_key != "n/a" and eff_key not in windows:
        windows[eff_key] = _compute_window(snap, history, now, effective_h)

    time_since_fill = _time_since_last_fill_minutes(snap["positions_closed"], history, now)

    diag = _diagnose(
        snap=snap, windows=windows,
        nominal_budget_usd=_compute_nominal_budget(cfg_raw),
        warmup_status=warmup, effective_window_key=eff_key,
        time_since_last_fill_min=time_since_fill, cfg=cfg,
    )

    return {
        "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "controller_id": canonical_id,
        "bot_name": bot_name,
        "config_name": config_name,
        "connector_name": cfg_raw.get("connector_name", ""),
        "trading_pair": cfg_raw.get("trading_pair", ""),
        "windows": windows,
        "snapshot": {k: v for k, v in snap.items() if k != "close_type_counts"},
        "close_type_counts": snap["close_type_counts"],
        "market_cross": market_cross or {},
        "diagnostic": diag,
    }


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------


def _compose_aggregate(payloads_by_id: dict[str, dict]) -> dict[str, Any]:
    """Cross-controller aggregate: counts + worst + sorted list."""
    counts = {"suboptimal_now": 0, "stuck": 0, "cold_start": 0, "pnl_flat": 0}
    worst = {"controller_id": None, "pnl_velocity_normalized": 0.0}
    total_pnl_24h = 0.0

    for cid, p in payloads_by_id.items():
        if not p:
            continue
        d = p.get("diagnostic", {})
        if d.get("suboptimal_now"):
            counts["suboptimal_now"] += 1
        if d.get("is_stuck"):
            counts["stuck"] += 1
        if d.get("warmup_status") == "cold_start":
            counts["cold_start"] += 1
        if d.get("is_pnl_flat"):
            counts["pnl_flat"] += 1
        # Track worst by normalized pnl velocity (more negative = worse).
        pnl_n = d.get("pnl_velocity_normalized")
        if pnl_n is not None and pnl_n < worst["pnl_velocity_normalized"]:
            worst["controller_id"] = cid
            worst["pnl_velocity_normalized"] = pnl_n
        # 24h pnl velocity → approximate "PnL last 24h" in USD.
        w24 = (p.get("windows", {}) or {}).get("last_24h", {}) or {}
        pnl_v_24h = w24.get("pnl_velocity")
        if pnl_v_24h is not None and w24.get("available"):
            total_pnl_24h += pnl_v_24h * 24.0

    if worst["controller_id"] is None:
        worst = None

    return {
        "ts": (next(iter(payloads_by_id.values()), {}) or {}).get("ts") or _utc_iso(),
        "controllers": sorted(payloads_by_id.keys()),
        "counts": counts,
        "worst": worst,
        "total_pnl_24h_usd": total_pnl_24h,
    }


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Autodetect
# ---------------------------------------------------------------------------


async def _autodetect_controllers(
    client, bot_names_filter: list[str] | None
) -> list[tuple[str, str, dict]]:
    """Return list of (bot_name, config_name, cfg_raw) for all (filtered) active controllers."""
    try:
        bots_raw = await client.bot_orchestration.get_active_bots_status()
    except Exception as e:
        logger.warning("controller_performance: get_active_bots_status failed: %s", e)
        return []
    data = bots_raw.get("data", {}) if isinstance(bots_raw, dict) else {}

    bot_names = list(data.keys())
    if bot_names_filter:
        bot_names = [b for b in bot_names if b in bot_names_filter]

    out: list[tuple[str, str, dict]] = []
    for bot_name in bot_names:
        try:
            cfgs = await client.controllers.get_bot_controller_configs(bot_name)
        except Exception as e:
            logger.warning("controller_performance: configs %s failed: %s", bot_name, e)
            continue
        for cfg in cfgs or []:
            if not isinstance(cfg, dict):
                continue
            cname = cfg.get("_config_name") or cfg.get("id")
            if not cname:
                continue
            out.append((bot_name, cname, cfg))
    return out


def _resolve_perf(perfs_map: dict, cfg_raw: dict) -> dict | None:
    """Same robust resolver as capital_state — try multiple keys then fall back."""
    for key in (
        cfg_raw.get("_config_name"),
        cfg_raw.get("id"),
        cfg_raw.get("controller_id"),
        cfg_raw.get("controller_name"),
    ):
        if key and key in perfs_map:
            entry = perfs_map[key]
            if isinstance(entry, dict):
                return entry.get("performance", entry)
    # Fallback: match by (connector, pair).
    target = (cfg_raw.get("connector_name", ""), cfg_raw.get("trading_pair", ""))
    for entry in perfs_map.values():
        perf = entry.get("performance", entry) if isinstance(entry, dict) else entry
        if not isinstance(perf, dict):
            continue
        for pos in perf.get("positions_summary", []) or []:
            if (pos.get("connector_name", ""), pos.get("trading_pair", "")) == target:
                return perf
    return None


# ---------------------------------------------------------------------------
# Agent views
# ---------------------------------------------------------------------------


def _build_agent_payload(p: dict) -> dict:
    """Minimal per-controller dict for the LLM."""
    d = p.get("diagnostic", {})
    snap = p.get("snapshot", {})
    mc = p.get("market_cross", {}) or {}

    def _window(key):
        w = (p.get("windows", {}) or {}).get(key) or {}
        if not w.get("available"):
            return {"available": False, "samples": w.get("samples", 0)}
        return {
            "available": True,
            "samples": w.get("samples", 0),
            "pnl_velocity": _r(w.get("pnl_velocity"), 6),
            "volume_velocity": _r(w.get("volume_velocity"), 4),
            "fills_per_hour": _r(w.get("fills_per_hour"), 4),
        }

    return {
        "ts": p.get("ts"),
        "controller_id": p.get("controller_id"),
        "trading_pair": p.get("trading_pair"),
        "windows": {k: _window(k) for k in (p.get("windows") or {}).keys()},
        "snapshot": {
            "realized_pnl_quote": _r(snap.get("realized_pnl_quote"), 4),
            "unrealized_pnl_quote": _r(snap.get("unrealized_pnl_quote"), 4),
            "net_pnl_quote": _r(snap.get("net_pnl_quote"), 4),
            "volume_traded": _r(snap.get("volume_traded"), 2),
            "positions_open": snap.get("positions_open"),
            "positions_closed": snap.get("positions_closed"),
        },
        "market_cross": {
            "market_share_24h": _r(mc.get("market_share_24h"), 6),
        } if mc else {},
        "diagnostic": {
            "is_pnl_flat": d.get("is_pnl_flat"),
            "is_volume_dropping": d.get("is_volume_dropping"),
            "is_stuck": d.get("is_stuck"),
            "suboptimal_now": d.get("suboptimal_now"),
            "suboptimal_period_minutes": d.get("suboptimal_period_minutes"),
            "warmup_status": d.get("warmup_status"),
            "effective_window_key": d.get("effective_window_key"),
            "effective_window_hours": _r(d.get("effective_window_hours"), 2),
            "close_types_dominant": d.get("close_types_dominant"),
            "time_since_last_fill_minutes": _r(d.get("time_since_last_fill_minutes"), 1),
            "pnl_velocity_normalized": _r(d.get("pnl_velocity_normalized"), 6),
        },
    }


def _build_agent_summary(aggregate: dict, payloads_by_id: dict) -> dict:
    return {
        "ts": aggregate["ts"],
        "controllers": aggregate["controllers"],
        "counts": aggregate["counts"],
        "worst": aggregate["worst"],
        "total_pnl_24h_usd": _r(aggregate["total_pnl_24h_usd"], 2),
    }


def _r(v, ndigits):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    try:
        return round(float(v), ndigits)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# User view — text, KPIs, table
# ---------------------------------------------------------------------------


def _render_compact_summary(aggregate: dict) -> str:
    """1-line ASCII summary for Telegram (≤ 220 chars, no backticks)."""
    if not aggregate["controllers"]:
        return "controller_performance: no controllers."
    total = len(aggregate["controllers"])
    c = aggregate["counts"]
    worst = aggregate.get("worst")
    worst_str = ""
    if worst and worst.get("controller_id"):
        cid = worst["controller_id"]
        # Show only the config_name part to keep it short
        short = cid.split("::", 1)[-1]
        worst_str = f" · worst: {short} {worst.get('pnl_velocity_normalized', 0):.4f}/h"
    parts = [
        f"{total} ctrl",
        f"{c['suboptimal_now']} subopt",
        f"{c['stuck']} stuck?",
        f"{c['cold_start']} cold",
        f"PnL24h ${aggregate['total_pnl_24h_usd']:.2f}",
    ]
    return " · ".join(parts) + worst_str


def _build_kpi_sections(aggregate: dict) -> list[dict]:
    total = len(aggregate["controllers"])
    c = aggregate["counts"]
    pnl_usd = aggregate["total_pnl_24h_usd"]
    return [
        {"type": "kpi", "label": "Controllers", "value": str(total), "delta": None, "trend": "neutral"},
        {"type": "kpi", "label": "Suboptimal",
         "value": str(c["suboptimal_now"]),
         "delta": f"{c['suboptimal_now'] * 100 // total}% of total" if total else None,
         "trend": "down" if c["suboptimal_now"] else "neutral"},
        {"type": "kpi", "label": "Stuck",
         "value": str(c["stuck"]),
         "delta": f"{c['stuck'] * 100 // total}% of total" if total else None,
         "trend": "down" if c["stuck"] else "neutral"},
        {"type": "kpi", "label": "PnL 24h",
         "value": f"${pnl_usd:,.2f}",
         "delta": f"{c['cold_start']} cold-start" if c["cold_start"] else None,
         "trend": "up" if pnl_usd > 0 else ("down" if pnl_usd < 0 else "neutral")},
    ]


def _build_summary_table(payloads_by_id: dict) -> tuple[list[str], list[dict]]:
    cols = [
        "controller", "pair", "pnl_24h_usd", "vol_24h_usd", "pos_closed",
        "fills_per_h_1h", "last_fill_min", "flags",
    ]
    rows = []
    for cid, p in payloads_by_id.items():
        if not p:
            continue
        d = p.get("diagnostic", {})
        w24 = (p.get("windows", {}) or {}).get("last_24h", {}) or {}
        w1 = (p.get("windows", {}) or {}).get("last_1h", {}) or {}
        snap = p.get("snapshot", {})

        pnl_24h = (w24.get("pnl_velocity") or 0) * 24.0 if w24.get("available") else None
        vol_24h_v = w24.get("volume_velocity")
        flags = []
        if d.get("warmup_status") == "cold_start": flags.append("❄️ cold")
        if d.get("is_stuck"): flags.append("🔒 stuck")
        if d.get("suboptimal_now"): flags.append("🟡 subopt")
        if d.get("is_pnl_flat"): flags.append("💤 flat")
        if d.get("is_volume_dropping"): flags.append("📉 vol↓")
        flag_str = " ".join(flags) if flags else "—"

        rows.append({
            "controller": cid.split("::", 1)[-1],
            "pair": p.get("trading_pair", "?"),
            "pnl_24h_usd": f"${pnl_24h:,.2f}" if pnl_24h is not None else "—",
            "vol_24h_usd": f"${vol_24h_v * 24:,.0f}" if vol_24h_v is not None else "—",
            "pos_closed": snap.get("positions_closed", 0),
            "fills_per_h_1h": f"{w1.get('fills_per_hour'):.2f}" if w1.get("available") else "—",
            "last_fill_min": (
                f"{d.get('time_since_last_fill_minutes'):.1f}"
                if d.get("time_since_last_fill_minutes") is not None else "—"
            ),
            "flags": flag_str,
        })
    # Sort: suboptimal/stuck first, then by PnL ascending (worst first).
    def _rank(r):
        has_problem = ("🔒" in r["flags"]) or ("🟡" in r["flags"]) or ("📉" in r["flags"])
        try:
            pnl = float(r["pnl_24h_usd"].replace("$", "").replace(",", "")) if r["pnl_24h_usd"] != "—" else 0
        except Exception:
            pnl = 0
        return (0 if has_problem else 1, pnl)
    rows.sort(key=_rank)
    return cols, rows


def _build_windows_table(p: dict) -> tuple[list[str], list[dict]]:
    cols = ["window", "available", "samples", "r_pnl", "u_pnl", "net_pnl", "vol", "vel_pnl", "vel_vol", "fills/h"]
    rows = []
    for k, w in (p.get("windows") or {}).items():
        if not isinstance(w, dict):
            continue
        rows.append({
            "window": k,
            "available": "✓" if w.get("available") else "—",
            "samples": w.get("samples", 0),
            "r_pnl": f"{w.get('realized_pnl_quote', 0):.2f}",
            "u_pnl": f"{w.get('unrealized_pnl_quote', 0):.2f}",
            "net_pnl": f"{w.get('net_pnl_quote', 0):.2f}",
            "vol": f"{w.get('volume_traded', 0):.0f}",
            "vel_pnl": f"{w.get('pnl_velocity'):.4f}" if w.get('pnl_velocity') is not None else "—",
            "vel_vol": f"{w.get('volume_velocity'):.0f}" if w.get('volume_velocity') is not None else "—",
            "fills/h": f"{w.get('fills_per_hour'):.2f}" if w.get('fills_per_hour') is not None else "—",
        })
    return cols, rows


# ---------------------------------------------------------------------------
# Narrative + glossary
# ---------------------------------------------------------------------------


def _build_aggregate_narrative(aggregate: dict, payloads_by_id: dict) -> str:
    total = len(aggregate["controllers"])
    if total == 0:
        return "Sin controllers activos."
    c = aggregate["counts"]
    pnl = aggregate["total_pnl_24h_usd"]
    parts = [
        f"De **{total} controllers** activos: "
        f"{c['suboptimal_now']} en zona subóptima · "
        f"{c['stuck']} con sospecha de estar atrapados · "
        f"{c['cold_start']} en cold-start (sin historia suficiente)."
    ]
    if pnl != 0:
        parts.append(f"PnL agregado 24h estimado: **${pnl:,.2f}**.")
    worst = aggregate.get("worst")
    if worst and worst.get("controller_id"):
        short = worst["controller_id"].split("::", 1)[-1]
        parts.append(
            f"El más crítico (por velocidad de PnL normalizada): **{short}** "
            f"({worst['pnl_velocity_normalized']:.4f}/h)."
        )
    return " ".join(parts)


def _build_controller_narrative(p: dict) -> str:
    d = p.get("diagnostic", {})
    pair = p.get("trading_pair", "?")
    warmup = d.get("warmup_status", "?")
    dom = d.get("close_types_dominant") or "n/a"
    parts = [
        f"**{pair}** · warmup {warmup} · close dominante: **{dom}**."
    ]
    if warmup == "cold_start":
        parts.append("Sin historia suficiente para diagnóstico (no se proponen acciones).")
        return " ".join(parts)
    flags = []
    if d.get("is_pnl_flat"): flags.append("PnL flat")
    if d.get("is_volume_dropping"): flags.append("volumen cayendo")
    if d.get("is_stuck"): flags.append("posiblemente atrapado")
    if flags:
        parts.append(f"Señales: {', '.join(flags)}.")
    last_fill = d.get("time_since_last_fill_minutes")
    if last_fill is not None:
        parts.append(f"Último fill detectado hace {last_fill:.1f} min.")
    return " ".join(parts)


_GLOSSARY = """## Glosario

- **pnl_velocity** — derivada temporal del net PnL del controller. USD/hora.
- **pnl_velocity_normalized** — pnl_velocity / nominal_budget. Ratio comparable entre controllers.
- **volume_velocity** — derivada temporal del volume traded. USD/hora.
- **fills_per_hour** — derivada del contador de posiciones cerradas. Aproximación a tasa de fills.
- **warmup_status** — `cold_start` (<30 min de historia), `adaptive` (30 min – 4h), `normal` (≥4h).
- **suboptimal_now** — true cuando PnL ≤ 0 + volumen cayendo + persistencia ≥ 30 min + no cold-start.
- **is_stuck** — sin fills por > stuck_minutes_threshold (default 60) Y velocidad de volumen 1h < 10% de la 24h.
- **close_types_dominant** — qué razón de cierre predomina (take_profit / early_stop / position_hold / ...).
- **market_share_24h** — volumen del controller / volumen del par en el exchange últimas 24h.
"""


# ---------------------------------------------------------------------------
# Report (HTML, persisted) — L3, with manual_order — L9
# ---------------------------------------------------------------------------


def _save_report(
    aggregate: dict, payloads_by_id: dict
) -> str | None:
    try:
        from condor.reports import ReportBuilder
    except Exception as e:
        logger.warning("ReportBuilder import failed: %s", e)
        return None

    controllers = aggregate["controllers"]
    if not controllers:
        return None

    builder = ReportBuilder(f"Controller Performance — {len(controllers)} ctrl — {aggregate['ts']}")
    builder.source("routine", "controller_performance").tags(["adaptive_framework", "performance"])
    # L9: never reorder. Tables must stay next to their headers.
    builder.manual_order()

    for k in _build_kpi_sections(aggregate):
        builder.kpi(k["label"], k["value"], delta=k.get("delta"), trend=k.get("trend", "neutral"))

    builder.markdown("## Resumen\n\n" + _build_aggregate_narrative(aggregate, payloads_by_id))

    cols, rows = _build_summary_table(payloads_by_id)
    if rows:
        builder.markdown("## Resumen por controller")
        builder.table(rows, columns=cols)

    # Per-controller sub-sections — header + narrative + windows table.
    # Each block is one builder.markdown(...) so the unit can't be split.
    for cid in controllers:
        p = payloads_by_id.get(cid)
        if not p:
            continue
        narr = _build_controller_narrative(p)
        ms = (p.get("market_cross") or {}).get("market_share_24h")
        ms_line = f"\n\n**Market share 24h**: {ms*100:.4f}%" if ms is not None else ""
        builder.markdown(
            f"## {cid}\n\n{narr}{ms_line}"
        )
        cols_w, rows_w = _build_windows_table(p)
        if rows_w:
            # Prefix each row with the controller id, so even if the
            # renderer reorders things again (L9 safety net), each row
            # is self-identifying.
            rows_w = [{"controller": cid.split('::', 1)[-1], **r} for r in rows_w]
            cols_w = ["controller"] + cols_w
            builder.table(rows_w, columns=cols_w)

    builder.markdown(_GLOSSARY)

    return builder.save()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


async def run(config: Config, context: ContextTypes.DEFAULT_TYPE) -> RoutineResult:
    chat_id = context._chat_id if hasattr(context, "_chat_id") else None
    client = await get_client(chat_id or 0, context=context)
    if not client:
        return RoutineResult(text="Could not connect to API server.")

    # Discover target controllers.
    discovered = await _autodetect_controllers(client, config.bot_names or None)
    if config.controller_ids:
        wanted = set(config.controller_ids)
        discovered = [
            (b, c, raw) for (b, c, raw) in discovered
            if f"{b}::{c}" in wanted
        ]
    if not discovered:
        return RoutineResult(text="No matching active controllers.")

    # Fetch active bots status once (shared across all controllers).
    try:
        bots_raw = await client.bot_orchestration.get_active_bots_status()
    except Exception as e:
        logger.error("controller_performance: bots status failed: %s", e)
        return RoutineResult(text=f"API error: {e}")
    bots_data = bots_raw.get("data", {}) if isinstance(bots_raw, dict) else {}

    now = datetime.now(timezone.utc)

    # Process each controller; cap concurrency on market_share fetches with
    # a semaphore (we'd otherwise hit the API with N concurrent get_candles).
    sem = asyncio.Semaphore(8)

    async def _process(bot_name: str, config_name: str, cfg_raw: dict) -> tuple[str, dict | None]:
        canonical_id = f"{bot_name}::{config_name}"
        bot_data = bots_data.get(bot_name) or {}
        perfs_map = bot_data.get("performance", {}) if isinstance(bot_data, dict) else {}
        perf = _resolve_perf(perfs_map, cfg_raw)
        if perf is None:
            logger.info("controller_performance: no perf for %s", canonical_id)
            return canonical_id, None

        # Load history (before persisting current — we don't want the new
        # snapshot to skew the velocity window).
        history = state_io.read_jsonl_all(_history_path(canonical_id))

        market_cross: dict = {}
        if config.compute_market_share:
            snap_now = _extract_snapshot(perf)
            async with sem:
                market_cross = await _maybe_fetch_market_share(
                    client,
                    cfg_raw.get("connector_name", "") or "",
                    cfg_raw.get("trading_pair", "") or "",
                    snap_now["volume_traded"],
                )

        payload = _build_controller_payload(
            canonical_id=canonical_id,
            bot_name=bot_name,
            config_name=config_name,
            cfg_raw=cfg_raw,
            perf=perf,
            history=history,
            now=now,
            cfg=config,
            market_cross=market_cross,
        )

        # Persist *after* computing windows.
        if config.persist_snapshot:
            _persist_snapshot(canonical_id, now, _extract_snapshot(perf))

        return canonical_id, payload

    results = await asyncio.gather(
        *(_process(b, c, raw) for b, c, raw in discovered),
        return_exceptions=True,
    )

    payloads_by_id: dict[str, dict] = {}
    for entry in results:
        if isinstance(entry, Exception):
            logger.error("controller_performance: pipeline error: %s", entry)
            continue
        cid, payload = entry
        if payload:
            payloads_by_id[cid] = payload

    if not payloads_by_id:
        return RoutineResult(text="No controllers produced a payload.")

    aggregate = _compose_aggregate(payloads_by_id)

    # Persist HTML report — L3.
    try:
        _save_report(aggregate, payloads_by_id)
    except Exception as e:
        logger.error("controller_performance: failed to save report: %s", e)

    text = _render_compact_summary(aggregate)
    kpi_sections = _build_kpi_sections(aggregate)
    data_sections: list[dict] = [
        {"type": "data", "title": "payload",
         "data": {"ts": aggregate["ts"], "aggregate": aggregate, "payloads": payloads_by_id}},
        {"type": "data", "title": "agent:summary", "data": _build_agent_summary(aggregate, payloads_by_id)},
    ]
    for cid in aggregate["controllers"]:
        data_sections.append({
            "type": "data",
            "title": f"agent:{cid}",
            "data": _build_agent_payload(payloads_by_id[cid]),
        })

    table_columns, table_rows = _build_summary_table(payloads_by_id)

    return RoutineResult(
        text=text,
        sections=kpi_sections + data_sections,
        table_data=table_rows,
        table_columns=table_columns,
    )
