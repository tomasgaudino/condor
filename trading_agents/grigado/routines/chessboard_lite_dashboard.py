"""Executive dashboard for active chessboard_lite grid controllers.

Reciclado de `chessboard_grid_dashboard`, adaptado al controller `chessboard_lite`:
detecta qué controllers `chessboard_lite` están corriendo, dibuja su rango activo
[min_price, max_price] como zonas sombreadas sobre un candlestick, marca el precio
actual y produce un resumen ejecutivo (bots, grillas, rangos, liquidez, volumen, PnL).

Diferencias clave vs el chessboard original:
  - Rango: `min_price` / `max_price` (en vez de `border_a` / `border_b`).
  - Niveles: `n_levels` (en vez de `n_grids`).
  - Inventario: cada grilla divide `total_amount_quote` 50/50 y transiciona lineal
    100% base en `min_price` (abajo) → 100% quote en `max_price` (arriba)
    (= techo 1.0 / piso 0.0 del modelo de inventario reciclado).
  - Banda dura de inventario: `inv_floor_pct` / `inv_ceiling_pct` (anti-desbalance).
"""

import io
import logging
import re
from collections import deque
from datetime import datetime, timezone

import pandas as pd
from pydantic import BaseModel, Field
from telegram.ext import ContextTypes

from config_manager import get_client
from routines.base import RoutineResult

logger = logging.getLogger(__name__)

CATEGORY = "Analysis"

# Distinct base hues for grid shading (R, G, B). Fills are drawn translucent so
# that overlapping ranges naturally stack into a denser tone.
_GRID_COLORS = [
    (56, 189, 248),   # sky
    (251, 146, 60),   # orange
    (52, 211, 153),   # emerald
    (244, 114, 182),  # pink
    (167, 139, 250),  # violet
    (250, 204, 21),   # amber
    (96, 165, 250),   # blue
    (248, 113, 113),  # red
]


class Config(BaseModel):
    """Dashboard ejecutivo de grillas chessboard_lite activas (rangos + inventario)."""

    controller_filter: str = Field(
        default="chessboard_lite",
        description="Solo controllers cuyo nombre/id contenga este texto",
    )
    interval: str = Field(default="1h", description="Intervalo de las velas")
    days: int = Field(default=7, description="Días de historial de velas a mostrar")
    fill_opacity: float = Field(
        default=0.12,
        description="Opacidad base del sombreado de cada grilla (overlap = más densa)",
    )
    detail_interval: str = Field(
        default="5m",
        description="Intervalo de velas para el detalle por grilla",
    )
    detail_max_days: int = Field(
        default=3,
        description="Tope de días de historial (5m) en el detalle por grilla",
    )
    detail_max_grids: int = Field(
        default=12,
        description="Máximo de grillas a graficar en el detalle (evita reportes enormes)",
    )
    maker_rebate_pct: float = Field(
        default=0.015,
        description="Rebate maker por leg, en % del notional (ej 0.015 = 0,015%). Se estima sobre TODOS los fills (LIMIT_MAKER).",
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _f(value, default=0.0) -> float:
    """Best-effort float coercion (configs sometimes return strings)."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _fmt(n: float, dec: int = 0) -> str:
    """Thousands-separated number formatting."""
    return f"{n:,.{dec}f}"


def _parse_candles(result) -> pd.DataFrame:
    """Normalize candle responses into a DataFrame with ts/open/high/low/close/volume."""
    records = result
    if isinstance(result, dict):
        records = result.get("data") or result.get("candles") or []
    if not records:
        return pd.DataFrame()

    rows = []
    for r in records:
        if isinstance(r, dict):
            ts = r.get("timestamp", r.get("time", r.get("t")))
            rows.append(
                {
                    "ts": ts,
                    "open": _f(r.get("open", r.get("o"))),
                    "high": _f(r.get("high", r.get("h"))),
                    "low": _f(r.get("low", r.get("l"))),
                    "close": _f(r.get("close", r.get("c"))),
                    "volume": _f(r.get("volume", r.get("v"))),
                }
            )
        elif isinstance(r, (list, tuple)) and len(r) >= 6:
            rows.append(
                {
                    "ts": r[0],
                    "open": _f(r[1]),
                    "high": _f(r[2]),
                    "low": _f(r[3]),
                    "close": _f(r[4]),
                    "volume": _f(r[5]),
                }
            )

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    # Timestamps may be seconds or milliseconds.
    ts = pd.to_numeric(df["ts"], errors="coerce")
    if ts.notna().all():
        unit = "ms" if ts.max() > 1e12 else "s"
        df["dt"] = pd.to_datetime(ts, unit=unit, utc=True)
    else:
        df["dt"] = pd.to_datetime(df["ts"], utc=True, errors="coerce")
    df = df.dropna(subset=["dt"]).sort_values("dt").reset_index(drop=True)
    return df


def _deploy_ts(bot_name: str, runs_map: dict) -> pd.Timestamp | None:
    """Deploy timestamp for a bot: prefer the runs API, fallback to the name suffix.

    Bot names embed the deploy time as `...-YYYYMMDD-HHMMSS` (UTC).
    """
    iso = runs_map.get(bot_name)
    if iso:
        ts = pd.to_datetime(iso, utc=True, errors="coerce")
        if pd.notna(ts):
            return ts
    m = re.search(r"-(\d{8})-(\d{6})$", bot_name)
    if m:
        ts = pd.to_datetime(m.group(1) + m.group(2), format="%Y%m%d%H%M%S",
                            utc=True, errors="coerce")
        if pd.notna(ts):
            return ts
    return None


async def _collect_grids(client, controller_filter: str) -> tuple[list[dict], list[dict]]:
    """Return (grids, bots) for active chessboard_lite controllers.

    Each grid merges its saved config (range, liquidity, banda de inventario) con
    la performance en vivo (pnl, volumen) del bot, más el timestamp de deploy.
    """
    status = await client.bot_orchestration.get_active_bots_status()
    data = status.get("data", status) if isinstance(status, dict) else {}
    # The bot name is the dict KEY (the value dict has no "bot_name" field).
    bot_items = list(data.items()) if isinstance(data, dict) else []

    # Deploy timestamps: bot_name -> deployed_at ISO string.
    runs_map: dict[str, str] = {}
    try:
        runs = await client.bot_orchestration.get_bot_runs()
        rdata = runs.get("data", runs) if isinstance(runs, dict) else runs
        if isinstance(rdata, list):
            for r in rdata:
                if isinstance(r, dict) and r.get("bot_name"):
                    runs_map[r["bot_name"]] = r.get("deployed_at") or r.get("created_at")
        elif isinstance(rdata, dict):
            for bn, info in rdata.items():
                if isinstance(info, dict):
                    runs_map[bn] = info.get("deployed_at") or info.get("created_at")
                elif isinstance(info, str):
                    runs_map[bn] = info
    except Exception:
        logger.debug("get_bot_runs unavailable; falling back to bot-name suffix")

    grids: list[dict] = []
    bots: list[dict] = []
    flt = controller_filter.lower()

    for bot_name, bot_data in bot_items:
        if not isinstance(bot_data, dict):
            continue
        performance = bot_data.get("performance", {})
        if not isinstance(performance, dict):
            continue

        # Pre-fetch controller configs for this bot, keyed by id and name.
        cfg_map: dict[str, dict] = {}
        try:
            configs = await client.controllers.get_bot_controller_configs(bot_name)
            for cfg in configs or []:
                if not isinstance(cfg, dict):
                    continue
                cid = cfg.get("id") or cfg.get("controller_id", "")
                cname = cfg.get("controller_name", "")
                if cid:
                    cfg_map[cid] = cfg
                if cname:
                    cfg_map.setdefault(cname, cfg)
        except Exception:
            logger.debug("No controller configs for bot %s", bot_name)

        bot_grid_count = 0
        for ctrl_name, ctrl_info in performance.items():
            if not isinstance(ctrl_info, dict):
                continue
            cfg = cfg_map.get(ctrl_name, {})
            cname = cfg.get("controller_name", "") or ctrl_name
            cid = cfg.get("id") or cfg.get("controller_id", ctrl_name)

            # Keep only chessboard_lite-style controllers.
            if flt not in cname.lower() and flt not in str(cid).lower() and flt not in ctrl_name.lower():
                continue

            # Rango: min_price/max_price (lite); fallback a border_a/border_b por las dudas.
            lo_raw = _f(cfg.get("min_price")) or _f(cfg.get("border_a"))
            hi_raw = _f(cfg.get("max_price")) or _f(cfg.get("border_b"))
            if lo_raw <= 0 and hi_raw <= 0:
                # No range info -> can't draw it, skip.
                continue
            low, high = sorted((lo_raw, hi_raw))

            live = ctrl_info.get("performance", ctrl_info)
            if not isinstance(live, dict):
                live = {}
            realized = _f(live.get("realized_pnl_quote"))
            unrealized = _f(live.get("unrealized_pnl_quote"))
            volume = _f(live.get("volume_traded"))

            grids.append(
                {
                    "id": str(cid),
                    "bot_name": bot_name,
                    "connector": cfg.get("connector_name", live.get("connector", "")),
                    "trading_pair": cfg.get("trading_pair", live.get("trading_pair", "")),
                    "low": low,
                    "high": high,
                    "n_levels": int(_f(cfg.get("n_levels"), 0)),
                    "liquidity": _f(cfg.get("total_amount_quote")),
                    # Inventario lite: 50/50, lineal. techo=100% base en min (low),
                    # piso=0% base en max (high). Sin techo/target_pct_btc del viejo.
                    "techo": 1.0,
                    "piso": 0.0,
                    # Banda dura anti-desbalance (% de capital en base).
                    "inv_floor": _f(cfg.get("inv_floor_pct"), 10.0),
                    "inv_ceiling": _f(cfg.get("inv_ceiling_pct"), 90.0),
                    "realized": realized,
                    "unrealized": unrealized,
                    "pnl": realized + unrealized,
                    "volume": volume,
                    "deployed_at": _deploy_ts(bot_name, runs_map),
                }
            )
            bot_grid_count += 1

        if bot_grid_count:
            bots.append({"bot_name": bot_name, "grids": bot_grid_count})

    return grids, bots


# --------------------------------------------------------------------------- #
# Telemetría del controller (custom_info publicado por chessboard_lite >= fix
# 2026-07-02 y persistido por la API en controller_performance_snapshots).
# --------------------------------------------------------------------------- #
def _parse_ci(raw) -> dict:
    """custom_info puede venir como dict o como JSON string (columna text)."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            import json
            return json.loads(raw)
        except Exception:
            return {}
    return {}


async def _fetch_telemetry(client, grids: list[dict]) -> dict[str, dict]:
    """{controller_id: {"latest": custom_info, "hist": DataFrame}} para las grillas
    que publican telemetría. Bots pre-fix → latest vacío (degradación limpia)."""
    tele: dict[str, dict] = {}
    latest_map: dict[str, dict] = {}
    try:
        resp = await client.bot_orchestration.get_latest_controller_performance()
        for rec in (resp.get("data", resp) or []):
            if isinstance(rec, dict) and rec.get("controller_id"):
                latest_map[rec["controller_id"]] = _parse_ci(rec.get("custom_info"))
    except Exception as e:
        logger.warning(f"telemetría: latest falló: {e}")

    for g in grids:
        cid = g["id"]
        latest = latest_map.get(cid, {})
        hist = pd.DataFrame()
        if latest:  # solo pedir historia si el controller publica telemetría
            try:
                h = await client.bot_orchestration.get_controller_performance_history(
                    controller_id=cid, interval="5m", limit=300)
                rows = []
                for rec in (h.get("data", h) or []):
                    ci = _parse_ci(rec.get("custom_info"))
                    if not ci:
                        continue
                    row = {"ts": pd.to_datetime(rec.get("timestamp"), utc=True),
                           "inv_pct": ci.get("inv_pct")}
                    for side in ("long", "short"):
                        sd = ci.get(side) or {}
                        row[f"taker_{side}"] = sd.get("taker_volume_quote", 0) or 0
                        row[f"relaunch_{side}"] = sd.get("relaunches", 0) or 0
                    rows.append(row)
                if rows:
                    hist = pd.DataFrame(rows).sort_values("ts")
            except Exception as e:
                logger.debug(f"telemetría: history {cid} falló: {e}")
        tele[cid] = {"latest": latest, "hist": hist}
    return tele


def _telemetry_alerts(g: dict, latest: dict) -> list[str]:
    """Alertas accionables por grilla, derivadas del custom_info más reciente."""
    alerts: list[str] = []
    cid = g["id"]
    taker_tot = sum(float((latest.get(s) or {}).get("taker_volume_quote", 0) or 0)
                    for s in ("long", "short"))
    if taker_tot > 0:
        alerts.append(f"🚨 `{cid}`: volumen TAKER {taker_tot:,.0f} — con LIMIT_MAKER debe ser 0 "
                      f"(¿controller viejo deployado?)")
    inv = latest.get("inv_pct")
    band = latest.get("inv_band") or [g.get("inv_floor", 10), g.get("inv_ceiling", 90)]
    if inv is not None and band and len(band) == 2:
        lo, hi = float(band[0]), float(band[1])
        if inv <= lo or inv >= hi:
            alerts.append(f"⚠ `{cid}`: inventario {inv:.0f}% FUERA de banda [{lo:.0f},{hi:.0f}] — "
                          f"un lado bloqueado")
        elif inv <= lo + 5 or inv >= hi - 5:
            alerts.append(f"⚠ `{cid}`: inventario {inv:.0f}% pegado al borde de banda "
                          f"[{lo:.0f},{hi:.0f}]")
    for side in ("long", "short"):
        sd = latest.get(side) or {}
        st = sd.get("state")
        if st == "WAITING_FUNDS":
            alerts.append(f"⚠ `{cid}` {side.upper()}: SIN FONDOS (retry en {sd.get('retry_in_s', '?')}s) "
                          f"— fondear la wallet")
        elif st == "BLOCKED_BAND":
            alerts.append(f"ℹ `{cid}` {side.upper()}: bloqueado por banda de inventario")
        nlr = sd.get("n_levels_real")
        if nlr and g.get("n_levels") and int(nlr) != int(g["n_levels"]):
            alerts.append(f"⚠ `{cid}` {side.upper()}: executor creó {nlr} niveles, config pide "
                          f"{g['n_levels']} — incoherencia lab↔executor")
    return alerts


def _tele_side_str(latest: dict) -> str:
    """'L:ACTIVE·S:WAITING_REENTRY' compacto para tablas/texto."""
    if not latest:
        return "—"
    parts = []
    for side, tag in (("long", "L"), ("short", "S")):
        st = (latest.get(side) or {}).get("state", "?")
        parts.append(f"{tag}:{st}")
    return " · ".join(parts)


def _build_telemetry_figure(cid: str, hist: pd.DataFrame, band: list, quote: str):
    """Series de tiempo: inventario scopeado vs banda + canario taker."""
    if hist.empty:
        return None
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.62, 0.38],
                        vertical_spacing=0.06,
                        subplot_titles=("Inventario scopeado (%)", "Canario taker (quote)"))
    for ann in fig.layout.annotations:
        ann.font = dict(size=12, color="#c9d1d9")

    fig.add_trace(go.Scatter(x=hist["ts"], y=hist["inv_pct"], mode="lines",
                             name="inv %", line=dict(color="#f59e0b", width=2)), row=1, col=1)
    if band and len(band) == 2:
        for v, lbl in ((band[0], "piso"), (band[1], "techo")):
            fig.add_hline(y=float(v), line=dict(color="#fbbf24", width=1, dash="dashdot"),
                          annotation_text=f"banda {lbl} {float(v):.0f}%",
                          annotation_font=dict(size=10, color="#fbbf24"), row=1, col=1)

    taker = hist.get("taker_long", 0) + hist.get("taker_short", 0)
    fig.add_trace(go.Scatter(x=hist["ts"], y=taker, mode="lines", name="taker vol",
                             line=dict(color="#ef4444", width=2),
                             fill="tozeroy", fillcolor="rgba(239,68,68,0.25)"), row=2, col=1)

    fig.update_layout(template="plotly_dark", paper_bgcolor="#0e1117", plot_bgcolor="#161b22",
                      title=f"Telemetría · {cid}", height=420, showlegend=False,
                      margin=dict(l=60, r=30, t=70, b=40))
    fig.update_yaxes(range=[0, 100], gridcolor="#21262d", row=1, col=1)
    fig.update_yaxes(gridcolor="#21262d", row=2, col=1)
    return fig


def _grid_base_fraction(price: float, low: float, high: float,
                        techo: float, piso: float) -> float:
    """Fraction of a grid's capital held in BASE at a given price.

    Below the range -> fully loaded (techo, =100% base for these grids).
    Above the range -> fully unloaded (piso, =0% base = 100% quote).
    Inside -> linear transition between techo (at low) and piso (at high).
    """
    if high <= low:
        return techo
    if price <= low:
        return techo
    if price >= high:
        return piso
    frac = (price - low) / (high - low)
    return techo - (techo - piso) * frac


def _inventory_profile(grids: list[dict], y_lo: float, y_hi: float, n_bins: int = 40):
    """Aggregate base/quote inventory (in quote units) across price bins."""
    import numpy as np

    edges = np.linspace(y_lo, y_hi, n_bins + 1)
    centers = (edges[:-1] + edges[1:]) / 2
    base_vals, quote_vals = [], []
    for P in centers:
        bv = qv = 0.0
        for g in grids:
            fb = _grid_base_fraction(P, g["low"], g["high"], g["techo"], g["piso"])
            bv += g["liquidity"] * fb
            qv += g["liquidity"] * (1.0 - fb)
        base_vals.append(bv)
        quote_vals.append(qv)
    step = float(edges[1] - edges[0]) if len(edges) > 1 else 1.0
    return centers, base_vals, quote_vals, step


def _volume_profile(df: pd.DataFrame, y_lo: float, y_hi: float, n_bins: int = 40):
    """Aggregate traded volume across price bins (classic volume-by-price).

    Each candle's volume is assigned to the bin holding its typical price
    (H+L+C)/3. Returns (bin_centers, volume_per_bin, bin_step, poc_index),
    where poc_index marks the Point of Control (highest-volume bin).
    """
    import numpy as np

    edges = np.linspace(y_lo, y_hi, n_bins + 1)
    centers = (edges[:-1] + edges[1:]) / 2
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    idx = np.clip(np.digitize(typical, edges) - 1, 0, n_bins - 1)
    vol = np.zeros(n_bins)
    for i, v in zip(idx, df["volume"]):
        vol[i] += float(v)
    step = float(edges[1] - edges[0]) if len(edges) > 1 else 1.0
    poc = int(np.argmax(vol)) if vol.any() else -1
    return centers, vol, step, poc


def _aggregate_pct_base(price: float, grids: list[dict]) -> float:
    """Aggregate %base across grids at a price (0-100)."""
    liq = sum(g["liquidity"] for g in grids)
    if liq <= 0:
        return 0.0
    base = sum(
        g["liquidity"] * _grid_base_fraction(price, g["low"], g["high"], g["techo"], g["piso"])
        for g in grids
    )
    return base / liq * 100.0


def _build_figure(df: pd.DataFrame, grids: list[dict], current_price: float,
                  pair: str, fill_opacity: float, quote: str):
    """Volume-by-price (left) + candlestick & grid-ranges (center) + inventory (right)."""
    import numpy as np
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    base_asset = pair.split("-")[0] if "-" in pair else "base"

    # 3 panels sharing the price (y) axis: volume profile | candles | inventory.
    fig = make_subplots(
        rows=1, cols=3, shared_yaxes=True,
        column_widths=[0.16, 0.68, 0.16], horizontal_spacing=0.012,
        subplot_titles=("Volumen por precio", "", "Inventario por precio"),
    )

    fig.add_trace(
        go.Candlestick(
            x=df["dt"],
            open=df["open"], high=df["high"], low=df["low"], close=df["close"],
            name=pair, showlegend=False,
            increasing_line_color="#26a69a", decreasing_line_color="#ef5350",
            increasing_fillcolor="#26a69a", decreasing_fillcolor="#ef5350",
            line_width=1, whiskerwidth=0.4,
        ),
        row=1, col=2,
    )

    x0, x1 = df["dt"].iloc[0], df["dt"].iloc[-1]
    for i, g in enumerate(grids):
        r, gg, b = _GRID_COLORS[i % len(_GRID_COLORS)]
        # Shading starts at the bot's deploy time (clamped to the visible window).
        dep = g.get("deployed_at")
        gx0 = x0
        dep_in_window = dep is not None and x0 < dep < x1
        if dep is not None and dep > x0:
            gx0 = min(dep, x1)
        # Translucent fill -> overlapping ranges stack into a denser tone.
        fig.add_shape(
            type="rect", x0=gx0, x1=x1, y0=g["low"], y1=g["high"],
            fillcolor=f"rgba({r},{gg},{b},{fill_opacity})",
            line_width=0, layer="below", row=1, col=2,
        )
        for edge in (g["low"], g["high"]):
            fig.add_shape(
                type="line", x0=gx0, x1=x1, y0=edge, y1=edge,
                line=dict(color=f"rgba({r},{gg},{b},0.85)", width=1, dash="dot"),
                layer="below", row=1, col=2,
            )
        # Deploy marker: vertical line + label where the grid went live.
        if dep_in_window:
            fig.add_shape(
                type="line", x0=gx0, x1=gx0, y0=g["low"], y1=g["high"],
                line=dict(color=f"rgba({r},{gg},{b},0.9)", width=1.2, dash="dot"),
                layer="below", row=1, col=2,
            )
            fig.add_annotation(
                x=gx0, y=g["high"], text=f"🚀 {dep.strftime('%d-%b %H:%M')} UTC",
                showarrow=False, xanchor="left", yanchor="bottom",
                font=dict(color=f"rgba({r},{gg},{b},1)", size=9), row=1, col=2,
            )
        # Always-visible range bracket at the right edge: guarantees a grid's
        # full [low, high] range is legible even when it just launched and its
        # filled area is still a thin sliver.
        xb = x1 - (x1 - x0) * 0.004
        fig.add_shape(
            type="line", x0=xb, x1=xb, y0=g["low"], y1=g["high"],
            line=dict(color=f"rgba({r},{gg},{b},1)", width=4),
            layer="above", row=1, col=2,
        )
        fig.add_annotation(
            x=x1, y=(g["low"] + g["high"]) / 2, text=f" {g['id']}",
            showarrow=False, xanchor="left",
            font=dict(color=f"rgba({r},{gg},{b},1)", size=10), row=1, col=2,
        )

    # Shared price (y) range across all three panels.
    y_lo = min(df["low"].min(), min(g["low"] for g in grids))
    y_hi = max(df["high"].max(), max(g["high"] for g in grids))

    # --- Volume-by-price panel (left): horizontal bars, mirroring inventory -- #
    v_centers, v_vol, v_step, v_poc = _volume_profile(df, y_lo, y_hi)
    bar_colors = ["rgba(124,196,255,0.55)"] * len(v_vol)
    if v_poc >= 0:
        bar_colors[v_poc] = "rgba(250,204,21,0.95)"  # POC highlighted amber
    fig.add_trace(
        go.Bar(
            y=v_centers, x=v_vol, orientation="h", name=f"Volumen ({base_asset})",
            marker_color=bar_colors, width=v_step * 0.92, showlegend=False,
            hovertemplate=("P=%{y:,.2f}<br>Volumen: %{x:,.0f} "
                           + base_asset + "<extra></extra>"),
        ),
        row=1, col=1,
    )
    # Point-of-Control marker line.
    if v_poc >= 0 and v_vol.any():
        fig.add_annotation(
            x=v_vol[v_poc], y=v_centers[v_poc], text="POC ",
            showarrow=False, xanchor="right", yanchor="middle",
            font=dict(color="#facc15", size=9), row=1, col=1,
        )

    # --- Inventory panel (right): horizontal stacked bars per price bin ----- #
    centers, base_vals, quote_vals, step = _inventory_profile(grids, y_lo, y_hi)
    totals = [bv + qv for bv, qv in zip(base_vals, quote_vals)]
    pct_base = np.array([(bv / t * 100 if t else 0) for bv, t in zip(base_vals, totals)])

    fig.add_trace(
        go.Bar(
            y=centers, x=base_vals, orientation="h", name=f"Base ({base_asset})",
            marker_color="rgba(251,146,60,0.85)", width=step * 0.92,
            customdata=pct_base.reshape(-1, 1),
            hovertemplate=("P=%{y:,.2f}<br>Base: %{x:,.0f} " + quote
                           + " (%{customdata[0]:.0f}%)<extra></extra>"),
        ),
        row=1, col=3,
    )
    fig.add_trace(
        go.Bar(
            y=centers, x=quote_vals, orientation="h", name=f"Quote ({quote})",
            marker_color="rgba(45,212,191,0.85)", width=step * 0.92,
            customdata=(100 - pct_base).reshape(-1, 1),
            hovertemplate=("P=%{y:,.2f}<br>Quote: %{x:,.0f} " + quote
                           + " (%{customdata[0]:.0f}%)<extra></extra>"),
        ),
        row=1, col=3,
    )

    # Current price dotted line across the three panels.
    fig.add_hline(y=current_price, line=dict(color="#facc15", width=1.5, dash="dash"),
                  row=1, col=1)
    fig.add_hline(
        y=current_price, line=dict(color="#facc15", width=1.5, dash="dash"),
        annotation_text=f"  precio actual {_fmt(current_price, 2)}",
        annotation_position="top left", annotation_font_color="#facc15",
        row=1, col=2,
    )
    fig.add_hline(y=current_price, line=dict(color="#facc15", width=1.5, dash="dash"),
                  row=1, col=3)

    fig.update_layout(
        template="plotly_dark",
        title=f"Grillas chessboard_lite activas · {pair}",
        height=640, margin=dict(l=60, r=30, t=70, b=60),
        xaxis_rangeslider_visible=False,
        barmode="stack", bargap=0.04,
        hovermode="closest",
        legend=dict(orientation="h", yanchor="top", y=-0.12, xanchor="center", x=0.5),
    )
    # Candle panel (col=2 -> xaxis2): kill the rangeslider that candlesticks add.
    fig.update_xaxes(rangeslider_visible=False, row=1, col=2)
    # Left panel: reverse x so volume bars grow toward the candles (mirror look).
    fig.update_xaxes(title_text=f"Vol ({base_asset})", autorange="reversed", row=1, col=1)
    fig.update_yaxes(title_text="Precio", row=1, col=1)
    fig.update_xaxes(title_text=f"Capital ({quote})", row=1, col=3)
    return fig


# --------------------------------------------------------------------------- #
# Per-grid detail (5m candles + volume profile + side)
# --------------------------------------------------------------------------- #
def _exec_side(ex: dict) -> str | None:
    """Normalize a grid_executor's side to 'BUY' or 'SELL'.

    Looks in custom_info, then top-level, then config. Side is sometimes a
    string ('BUY'/'SELL') and sometimes numeric (1=BUY, 2=SELL).
    """
    cfg = ex.get("config", {}) or {}
    ci = ex.get("custom_info", {}) or {}
    raw = (ci.get("side") or ci.get("grid_side")
           or ex.get("side") or cfg.get("side"))
    if raw is None:
        return None
    s = str(raw).upper()
    if "BUY" in s or s == "1":
        return "BUY"
    if "SELL" in s or s == "2":
        return "SELL"
    return None


def _exec_ts(ex: dict) -> pd.Timestamp | None:
    """Best-effort created timestamp (epoch seconds) of an executor."""
    cfg = ex.get("config", {}) or {}
    raw = ex.get("timestamp") or cfg.get("timestamp") or ex.get("created_at")
    if raw is None:
        return None
    val = pd.to_numeric(raw, errors="coerce")
    if pd.notna(val):
        unit = "ms" if val > 1e12 else "s"
        return pd.to_datetime(val, unit=unit, utc=True)
    return pd.to_datetime(raw, utc=True, errors="coerce")


def _exec_vol(ex: dict) -> float:
    for key in ("filled_amount_quote", "volume_traded", "total_volume"):
        v = ex.get(key)
        if v:
            return _f(v)
    return 0.0


def _exec_pnl(ex: dict) -> float:
    for key in ("net_pnl_quote", "realized_pnl_quote", "pnl_quote", "net_pnl"):
        v = ex.get(key)
        if v is not None and v != 0:
            return _f(v)
    return 0.0


async def _grid_executors(client, grid: dict) -> list[dict]:
    """Grid-executors attributable to a grid: same pair, born after deploy,
    price range overlapping the grid's [low, high].

    Executors don't persist a controller_id we can match, so we attribute by
    (connector, trading_pair) + deploy window + range overlap. Imperfect if two
    controllers share a pair+range, but correct for the usual one-grid case.
    """
    from condor.fetchers.executors import extract_executors_list

    try:
        result = await client.executors.search_executors(
            connector_names=[grid["connector"]],
            trading_pairs=[grid["trading_pair"]],
            executor_types=["grid_executor"],
            limit=500,
        )
    except Exception as e:
        logger.debug("search_executors failed for %s: %s", grid["id"], e)
        return []

    dep = grid.get("deployed_at")
    out = []
    for ex in extract_executors_list(result):
        if not isinstance(ex, dict):
            continue
        ts = _exec_ts(ex)
        if dep is not None and ts is not None and ts < dep:
            continue  # belongs to an earlier run
        cfg = ex.get("config", {}) or {}
        sp, ep = _f(cfg.get("start_price")), _f(cfg.get("end_price"))
        lo, hi = sorted((sp, ep)) if (sp or ep) else (0.0, 0.0)
        # Range-overlap check (skip when we have no price info).
        if hi > 0 and (hi < grid["low"] or lo > grid["high"]):
            continue
        out.append({
            "id": ex.get("id", ""),
            "side": _exec_side(ex),
            "low": lo, "high": hi,
            "volume": _exec_vol(ex),
            "pnl": _exec_pnl(ex),
            "status": str(ex.get("status", "")),
            "close_type": str(ex.get("close_type", "")),
            "created": ts,
        })
    return out


def _grid_side_label(execs: list[dict]) -> tuple[str, str]:
    """(label, emoji) for a grid based on its executors' net side by volume.

    SELL volume → SHORT, BUY volume → LONG. Returns ('s/d', '⚪') when there's
    no executor data yet (e.g. freshly relaunched, no closed cycles).
    """
    if not execs:
        return "s/d", "⚪"
    sell = sum(e["volume"] for e in execs if e["side"] == "SELL")
    buy = sum(e["volume"] for e in execs if e["side"] == "BUY")
    if sell == 0 and buy == 0:
        # Fall back to count if volume is all zero.
        sell = sum(1 for e in execs if e["side"] == "SELL")
        buy = sum(1 for e in execs if e["side"] == "BUY")
    if sell == 0 and buy == 0:
        return "s/d", "⚪"
    ratio = max(sell, buy) / (sell + buy)
    if ratio < 0.6:
        return "MIXTO", "🔵"
    return ("SHORT", "🔴") if sell >= buy else ("LONG", "🟢")


def _find_trades(obj):
    """Recursively locate the 'trades' list inside the bot-history payload."""
    if isinstance(obj, dict):
        if isinstance(obj.get("trades"), list):
            return obj["trades"]
        for v in obj.values():
            r = _find_trades(v)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_trades(v)
            if r is not None:
                return r
    return None


async def _bot_history_trades(client, bot_name: str) -> list[dict]:
    """Live fills of a running bot, read from its docker container via MQTT.

    The orders/trades DB does NOT carry a running bot's activity (it only syncs
    on archival), and the performance endpoint exposes no executor breakdown.
    `get_bot_history` is the only live source — but its client wrapper passes
    `verbose` as a bool the HTTP layer rejects, so we call `_get` directly with
    string params. Returns [{price, base, qvol, side, symbol, ts(ms), fee_quote}].
    """
    try:
        h = await client.bot_orchestration._get(
            f"/bot-orchestration/{bot_name}/history",
            params={"days": 0, "verbose": "false", "timeout": 30},
        )
    except Exception as e:
        logger.debug("get_bot_history failed for %s: %s", bot_name, e)
        return []
    raw = _find_trades(h) or []
    out: list[dict] = []
    for t in raw:
        if not isinstance(t, dict):
            continue
        price = _f(t.get("price"))
        qty = _f(t.get("quantity"))
        side = str(t.get("trade_type", "")).upper()
        if price <= 0 or qty <= 0 or side not in ("BUY", "SELL"):
            continue
        # Fee in quote: flat fees come in base (BUY) or quote (SELL); convert.
        fee_q = 0.0
        tf = (t.get("raw_json") or {}).get("trade_fee") or {}
        for ff in (tf.get("flat_fees") or []):
            tok = str(ff.get("token", ""))
            amt = _f(ff.get("amount"))
            if tok == t.get("quote_asset"):
                fee_q += amt
            elif tok == t.get("base_asset"):
                fee_q += amt * price
        out.append({
            "price": price, "base": qty, "qvol": price * qty, "side": side,
            "symbol": t.get("symbol", ""), "ts": int(_f(t.get("trade_timestamp"))),
            "fee_quote": fee_q,
        })
    return out


def _fifo_match(fills: list[dict]) -> dict:
    """Price-aware FIFO over the bot's own fills (this run only).

    A SELL closes the oldest OPEN buy priced below it (take-profit of the LONG
    grid); a BUY closes the oldest OPEN sell priced above it (take-profit of the
    SHORT grid). Anything that can't close at a profit OPENS inventory. Mutates
    each fill with `matched`/`open` (base) and returns realized spread (fees
    deducted) plus the break-evens of the open inventory:
      BE buy  = wavg price of open buys  (LONG inventory exposure)
      BE sell = wavg price of open sells (SHORT inventory exposure)
      BE gen  = wavg cost of the NET open inventory (open buys - open sells)

    Fills carry neither a grid tag nor an order id, so grid membership can't be
    read directly; this reconstructs round-trips from the grid's TP economics.
    """
    sl = sorted(fills, key=lambda f: f["ts"])
    for f in sl:
        f["matched"] = 0.0
    open_longs: deque = deque()   # open BUY lots:  {price, base, f}
    open_shorts: deque = deque()  # open SELL lots: {price, base, f}
    realized = 0.0
    fees = 0.0
    # Per-round-trip attribution: which grid earned it and at which level (the
    # OPEN side's price = the grid level that originated the trade).
    matches: list[dict] = []
    for f in sl:
        fees += f.get("fee_quote", 0.0)
        amt, price = f["base"], f["price"]
        if f["side"] == "BUY":
            while amt > 1e-12 and open_shorts and open_shorts[0]["price"] > price:
                lot = open_shorts[0]
                m = min(amt, lot["base"])
                realized += (lot["price"] - price) * m
                matches.append({"side": "SHORT", "level": lot["price"],
                                "spread": (lot["price"] - price) * m})
                f["matched"] += m
                lot["f"]["matched"] += m
                lot["base"] -= m
                amt -= m
                if lot["base"] <= 1e-12:
                    open_shorts.popleft()
            if amt > 1e-12:
                open_longs.append({"price": price, "base": amt, "f": f})
        else:  # SELL
            while amt > 1e-12 and open_longs and open_longs[0]["price"] < price:
                lot = open_longs[0]
                m = min(amt, lot["base"])
                realized += (price - lot["price"]) * m
                matches.append({"side": "LONG", "level": lot["price"],
                                "spread": (price - lot["price"]) * m})
                f["matched"] += m
                lot["f"]["matched"] += m
                lot["base"] -= m
                amt -= m
                if lot["base"] <= 1e-12:
                    open_longs.popleft()
            if amt > 1e-12:
                open_shorts.append({"price": price, "base": amt, "f": f})
    for f in sl:
        f["open"] = max(0.0, f["base"] - f["matched"])
    ob_base = sum(l["base"] for l in open_longs)
    ob_cost = sum(l["base"] * l["price"] for l in open_longs)
    os_base = sum(l["base"] for l in open_shorts)
    os_cost = sum(l["base"] * l["price"] for l in open_shorts)
    net_base = ob_base - os_base
    # BE general = price where the open book's unrealized PnL = 0
    # = (ob_cost - os_cost) / net_base. When the book is ~market-neutral
    # (|net| small vs gross), that slope ~0 and the BE flies to a meaningless
    # value, so only report it when the net is a real fraction of the gross.
    gross = ob_base + os_base
    be_general = None
    if gross > 1e-12 and abs(net_base) > 0.15 * gross:
        be_general = (ob_cost - os_cost) / net_base
    return {
        "realized": realized - fees, "fees": fees,
        "be_buy": (ob_cost / ob_base) if ob_base > 1e-12 else None,
        "be_sell": (os_cost / os_base) if os_base > 1e-12 else None,
        "be_general": be_general,
        "open_long_base": ob_base, "open_short_base": os_base,
        "matched_quote": sum(f["matched"] * f["price"] for f in sl),
        "open_quote": sum(f["open"] * f["price"] for f in sl),
        "t0": sl[0]["ts"] if sl else None,
        "matches": matches,
        "open_lots": ([{"side": "BUY", "price": l["price"], "base": l["base"]} for l in open_longs]
                      + [{"side": "SELL", "price": l["price"], "base": l["base"]} for l in open_shorts]),
    }


# Paleta accesible (daltonismo rojo-verde): LONG/BUY = azul, SHORT/SELL = ámbar.
# El estado se codifica por textura/opacidad (sólido = matcheado, claro = abierto),
# nunca solo por matiz.
_TEAL_M = "rgba(55,138,221,0.9)"    # LONG/BUY matched (solid, azul)
_TEAL_O = "rgba(55,138,221,0.32)"   # LONG/BUY open (translucent, azul)
_RED_M = "rgba(239,159,39,0.9)"     # SHORT/SELL matched (solid, ámbar)
_RED_O = "rgba(239,159,39,0.32)"    # SHORT/SELL open (translucent, ámbar)
_BLUE = "#378ADD"                   # LONG (accesible)
_AMBER = "#EF9F27"                  # SHORT (accesible)
_PINK = "#D4537E"                   # rebates (tercer color, distinguible de ambos)
_GREEN_TOT = "#2EA043"              # totalizador ingreso
_RED_TOT = "#E24B4A"                # totalizador IL


def _build_grid_detail_figure(df: pd.DataFrame, grid: dict, side_label: str,
                              side_emoji: str, current_price: float,
                              fills: list[dict], fifo: dict, n_bins: int = 60):
    """Break-even-with-resolution monitor for one grid (spec: chessboard_lite).

    4 panels, all on this run's fills (t >= t0 = first fill):
      [L]  mirror volume-by-price: SELL left / BUY right, each bar split into
           matcheado (solid) + abierto (translucent) from the FIFO of §1.
      [TOP] OHLC + range shading from t0 + 4 hlines: precio actual, BE buy,
            BE sell, BE general (a BE line is omitted if that side has no open
            inventory).
      [B]  LONG (BUY) volume over time, matcheado/abierto.
      [C]  SHORT (SELL) volume over time, matcheado/abierto.
    """
    import numpy as np
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    pair = grid["trading_pair"]
    quote = pair.split("-")[-1] if "-" in pair else "quote"

    fig = make_subplots(
        rows=3, cols=2, shared_xaxes=True, shared_yaxes=True,
        column_widths=[0.20, 0.80], row_heights=[0.62, 0.19, 0.19],
        horizontal_spacing=0.012, vertical_spacing=0.035,
        specs=[[{}, {}], [None, {}], [None, {}]],
        subplot_titles=("Vol × precio (matcheado/abierto)", "", "", ""),
    )

    # t0 = first fill of this run; window starts there.
    t0 = fifo.get("t0")
    t0_dt = pd.to_datetime(t0, unit="ms", utc=True) if t0 else df["dt"].iloc[0]
    x0, x1 = df["dt"].iloc[0], df["dt"].iloc[-1]
    gx0 = max(t0_dt, x0)

    # --- TOP: OHLC + range shading from t0 + BE lines --------------------- #
    fig.add_trace(
        go.Candlestick(
            x=df["dt"], open=df["open"], high=df["high"], low=df["low"],
            close=df["close"], name=pair, showlegend=False,
            increasing_line_color="#26a69a", decreasing_line_color="#ef5350",
            increasing_fillcolor="#26a69a", decreasing_fillcolor="#ef5350",
            line_width=1, whiskerwidth=0.4,
        ),
        row=1, col=2,
    )
    fig.add_shape(type="rect", x0=gx0, x1=x1, y0=grid["low"], y1=grid["high"],
                  fillcolor="rgba(56,189,248,0.08)", line_width=0,
                  layer="below", row=1, col=2)
    for edge in (grid["low"], grid["high"]):
        fig.add_shape(type="line", x0=gx0, x1=x1, y0=edge, y1=edge,
                      line=dict(color="rgba(56,189,248,0.5)", width=1, dash="dot"),
                      layer="below", row=1, col=2)

    be_lines = [
        ("precio actual", current_price, "#facc15", "dash"),
        ("BE buy", fifo.get("be_buy"), "#2dd4bf", "solid"),
        ("BE sell", fifo.get("be_sell"), "#ef4444", "solid"),
        ("BE general", fifo.get("be_general"), "#cbd5e1", "dot"),
    ]
    for label, yv, color, dash in be_lines:
        if yv is None:
            continue  # no inventory on that side -> don't invent a value
        fig.add_hline(
            y=yv, line=dict(color=color, width=1.5, dash=dash),
            annotation_text=f"  {label} {_fmt(yv, 2)}", annotation_position="top left",
            annotation_font_color=color, annotation_font_size=10, row=1, col=2,
        )

    # Shared price (y) range: grid range + every BE line + current price, padded.
    ys = [grid["low"], grid["high"], current_price]
    ys += [v for _, v, _, _ in be_lines if v is not None]
    y_lo, y_hi = min(ys), max(ys)
    pad = (y_hi - y_lo) * 0.05 or y_lo * 0.01
    y_lo, y_hi = y_lo - pad, y_hi + pad

    # --- LEFT: mirror volume-by-price, matched solid / open translucent --- #
    edges = np.linspace(y_lo, y_hi, n_bins + 1)
    centers = (edges[:-1] + edges[1:]) / 2
    step = float(edges[1] - edges[0]) if len(edges) > 1 else 1.0
    buy_m = np.zeros(n_bins); buy_o = np.zeros(n_bins)
    sell_m = np.zeros(n_bins); sell_o = np.zeros(n_bins)
    for f in fills:
        p = f["price"]
        if p < y_lo or p > y_hi:
            continue
        i = int(np.clip(np.digitize([p], edges)[0] - 1, 0, n_bins - 1))
        if f["side"] == "BUY":
            buy_m[i] += f.get("matched", 0.0) * p
            buy_o[i] += f.get("open", 0.0) * p
        else:
            sell_m[i] += f.get("matched", 0.0) * p
            sell_o[i] += f.get("open", 0.0) * p

    def _prof(x, name, color, show):
        return go.Bar(
            y=centers, x=x, orientation="h", name=name, marker_color=color,
            width=step * 0.92, legendgroup=name, showlegend=show,
            customdata=np.abs(x), hovertemplate="P=%{y:,.2f}<br>" + name
            + ": %{customdata:,.0f} " + quote + "<extra></extra>",
        )
    # SELL to the left (negative), BUY to the right (positive); relative mode
    # stacks matched + open on each side.
    fig.add_trace(_prof(-sell_m, "SHORT matcheado", _RED_M, True), row=1, col=1)
    fig.add_trace(_prof(-sell_o, "SHORT abierto", _RED_O, True), row=1, col=1)
    fig.add_trace(_prof(buy_m, "LONG matcheado", _TEAL_M, True), row=1, col=1)
    fig.add_trace(_prof(buy_o, "LONG abierto", _TEAL_O, True), row=1, col=1)
    fig.add_vline(x=0, line=dict(color="rgba(255,255,255,0.25)", width=1), row=1, col=1)

    # --- ROWS B/C: volume over time per side, matched/open ---------------- #
    if len(df) >= 2:
        diffs = df["dt"].diff().dropna().dt.total_seconds()
        bucket_s = float(diffs.median()) if not diffs.empty else 300.0
    else:
        bucket_s = 300.0
    bucket_ms = max(60.0, bucket_s) * 1000.0
    tb: dict = {}  # bucket_ms_start -> [buy_m, buy_o, sell_m, sell_o] in quote
    for f in fills:
        b = int(f["ts"] // bucket_ms) * int(bucket_ms)
        acc = tb.setdefault(b, [0.0, 0.0, 0.0, 0.0])
        p = f["price"]
        if f["side"] == "BUY":
            acc[0] += f.get("matched", 0.0) * p
            acc[1] += f.get("open", 0.0) * p
        else:
            acc[2] += f.get("matched", 0.0) * p
            acc[3] += f.get("open", 0.0) * p
    if tb:
        bks = sorted(tb)
        bx = pd.to_datetime(bks, unit="ms", utc=True)
        bw = bucket_ms * 0.9
        col = [tb[b] for b in bks]
        fig.add_trace(go.Bar(x=bx, y=[c[0] for c in col], width=bw, marker_color=_TEAL_M,
                             legendgroup="LONG matcheado", showlegend=False,
                             hovertemplate="%{x}<br>LONG matcheado %{y:,.0f}<extra></extra>"),
                      row=2, col=2)
        fig.add_trace(go.Bar(x=bx, y=[c[1] for c in col], width=bw, marker_color=_TEAL_O,
                             legendgroup="LONG abierto", showlegend=False,
                             hovertemplate="%{x}<br>LONG abierto %{y:,.0f}<extra></extra>"),
                      row=2, col=2)
        fig.add_trace(go.Bar(x=bx, y=[c[2] for c in col], width=bw, marker_color=_RED_M,
                             legendgroup="SHORT matcheado", showlegend=False,
                             hovertemplate="%{x}<br>SHORT matcheado %{y:,.0f}<extra></extra>"),
                      row=3, col=2)
        fig.add_trace(go.Bar(x=bx, y=[c[3] for c in col], width=bw, marker_color=_RED_O,
                             legendgroup="SHORT abierto", showlegend=False,
                             hovertemplate="%{x}<br>SHORT abierto %{y:,.0f}<extra></extra>"),
                      row=3, col=2)

    fig.update_layout(
        template="plotly_dark",
        title=(f"{grid['id']} · {side_emoji} {side_label} · {pair}  "
               f"({len(fills)} fills · realizado {_fmt(fifo.get('realized', 0), 1)} {quote})"),
        height=720, margin=dict(l=60, r=30, t=64, b=64),
        barmode="relative", bargap=0.06, hovermode="closest",
        legend=dict(orientation="h", yanchor="top", y=-0.06, xanchor="center", x=0.5),
    )
    fig.update_xaxes(rangeslider_visible=False, row=1, col=2)
    fig.update_xaxes(title_text=f"Vol bot ({quote}) · SELL ◄ ► BUY", row=1, col=1)
    fig.update_yaxes(title_text="Precio", range=[y_lo, y_hi], row=1, col=1)
    fig.update_yaxes(range=[y_lo, y_hi], row=1, col=2)
    fig.update_yaxes(title_text=f"BUY ({quote})", row=2, col=2)
    fig.update_yaxes(title_text=f"SELL ({quote})", row=3, col=2)
    return fig


def _build_level_pnl_figure(grid: dict, fifo: dict, current_price: float,
                            quote: str, rebate_pct: float, fills: list[dict]):
    """Ingreso vs IL por nivel de precio (el 'modelo de superficies').

    Identidad contable: PnL vs HOLD = Σ spread capturado + Σ rebates + PnL del abierto.
      - Derecha (sólido): spread por round-trip, apilado por grilla (azul LONG /
        ámbar SHORT) + rebates maker estimados (rosa) sobre TODO el volumen.
      - Izquierda (rayado): PnL no realizado del inventario abierto, por lado
        (la 'IL' de los tramos direccionales). Abierto en ganancia va a la derecha.
      - Fila inferior: totalizadores en la MISMA escala X → comparar superficies
        de un vistazo. Verde = Σ ingresos; rojo = Σ IL abierta.
    Paleta y texturas aptas para daltonismo (color nunca es el único canal).
    """
    import numpy as np
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    matches = fifo.get("matches") or []
    open_lots = fifo.get("open_lots") or []
    if not matches and not open_lots:
        return None

    ref_prices = ([grid["low"], grid["high"]] + [m["level"] for m in matches]
                  + [l["price"] for l in open_lots])
    lo, hi = min(ref_prices) * 0.999, max(ref_prices) * 1.001
    n_bins = int(min(40, max(10, grid.get("n_levels") or 20)))
    edges = np.linspace(lo, hi, n_bins + 1)
    centers = (edges[:-1] + edges[1:]) / 2.0

    def _bin(p: float) -> int:
        return int(np.clip(np.searchsorted(edges, p) - 1, 0, n_bins - 1))

    sp_long = np.zeros(n_bins)
    sp_short = np.zeros(n_bins)
    rebates = np.zeros(n_bins)
    open_long = np.zeros(n_bins)
    open_short = np.zeros(n_bins)
    for m in matches:
        (sp_long if m["side"] == "LONG" else sp_short)[_bin(m["level"])] += m["spread"]
    for f in fills:
        rebates[_bin(f["price"])] += f["qvol"] * rebate_pct / 100.0
    for l in open_lots:
        pnl = ((current_price - l["price"]) * l["base"] if l["side"] == "BUY"
               else (l["price"] - current_price) * l["base"])
        (open_long if l["side"] == "BUY" else open_short)[_bin(l["price"])] += pnl

    income_total = float(sp_long.sum() + sp_short.sum() + rebates.sum()
                         + open_long.clip(min=0).sum() + open_short.clip(min=0).sum())
    il_total = float(-(open_long.clip(max=0).sum() + open_short.clip(max=0).sum()))
    net = income_total - il_total

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.82, 0.18],
                        vertical_spacing=0.05)

    bar_kw = dict(orientation="h", width=(edges[1] - edges[0]) * 0.9)
    fig.add_trace(go.Bar(
        y=centers, x=open_long, name="Abierto LONG (PnL ±)", **bar_kw,
        marker=dict(color="rgba(55,138,221,0.18)",
                    pattern=dict(shape="\\", fgcolor=_BLUE, size=5, solidity=0.5)),
        hovertemplate="P=%{y:,.2f}<br>abierto LONG (+ganando/-perdiendo): %{x:+,.2f} " + quote + "<extra></extra>",
    ), row=1, col=1)
    fig.add_trace(go.Bar(
        y=centers, x=open_short, name="Abierto SHORT (PnL ±)", **bar_kw,
        marker=dict(color="rgba(239,159,39,0.18)",
                    pattern=dict(shape="/", fgcolor=_AMBER, size=5, solidity=0.5)),
        hovertemplate="P=%{y:,.2f}<br>abierto SHORT (+ganando/-perdiendo): %{x:+,.2f} " + quote + "<extra></extra>",
    ), row=1, col=1)
    fig.add_trace(go.Bar(
        y=centers, x=sp_long, name="Spread LONG", marker_color=_BLUE, **bar_kw,
        hovertemplate="P=%{y:,.2f}<br>spread LONG: %{x:,.2f} " + quote + "<extra></extra>",
    ), row=1, col=1)
    fig.add_trace(go.Bar(
        y=centers, x=sp_short, name="Spread SHORT", marker_color=_AMBER, **bar_kw,
        hovertemplate="P=%{y:,.2f}<br>spread SHORT: %{x:,.2f} " + quote + "<extra></extra>",
    ), row=1, col=1)
    fig.add_trace(go.Bar(
        y=centers, x=rebates, name=f"Rebates {rebate_pct}%", marker_color=_PINK, **bar_kw,
        hovertemplate="P=%{y:,.2f}<br>rebates: %{x:,.2f} " + quote + "<extra></extra>",
    ), row=1, col=1)

    fig.add_hline(y=current_price, line=dict(color="#ffffff", width=1, dash="dash"),
                  annotation_text=f"precio {current_price:,.2f}",
                  annotation_font=dict(size=10, color="#ffffff"), row=1, col=1)
    fig.add_vline(x=0, line=dict(color="#484f58", width=1), row=1, col=1)

    # Totalizadores en la misma escala X: comparar superficies de un vistazo.
    fig.add_trace(go.Bar(
        y=["Σ IL abierta"], x=[-il_total], orientation="h", marker_color=_RED_TOT,
        text=[f"−{il_total:,.2f}"], textposition="outside", showlegend=False,
        hovertemplate="IL abierta total: %{x:,.2f} " + quote + "<extra></extra>",
    ), row=2, col=1)
    fig.add_trace(go.Bar(
        y=["Σ ingreso"], x=[income_total], orientation="h", marker_color=_GREEN_TOT,
        text=[f"+{income_total:,.2f}"], textposition="outside", showlegend=False,
        hovertemplate="Ingreso total: %{x:,.2f} " + quote + "<extra></extra>",
    ), row=2, col=1)

    fig.update_layout(
        template="plotly_dark", paper_bgcolor="#0e1117", plot_bgcolor="#161b22",
        barmode="relative", height=560,
        title=(f"Ingreso vs IL por nivel · {grid['id']} · "
               f"neto {net:+,.2f} {quote} vs HOLD"),
        margin=dict(l=80, r=60, t=70, b=40),
        legend=dict(orientation="h", yanchor="bottom", y=-0.14, xanchor="center", x=0.5,
                    font=dict(size=10)),
    )
    fig.update_yaxes(title_text="Precio", gridcolor="#21262d", row=1, col=1)
    fig.update_xaxes(title_text=f"{quote} por nivel · sólido = cobrado · rayado = abierto (der=ganando, izq=perdiendo)",
                     tickformat=",.2s", gridcolor="#21262d", row=2, col=1)
    return fig


async def _collect_grid_details(client, grids: list[dict], config: Config):
    """For each grid (capped), gather its executors, side, and a 5m detail figure.

    Returns a list of dicts: {grid, side_label, side_emoji, execs, fig}.
    Candles + current price are fetched once per (connector, pair) and reused.
    """
    detail_grids = grids[: config.detail_max_grids]
    candle_cache: dict[tuple, pd.DataFrame] = {}
    price_cache: dict[tuple, float] = {}
    hist_cache: dict[str, list[dict]] = {}  # bot_name -> live fills
    out = []

    for g in detail_grids:
        key = (g["connector"], g["trading_pair"])

        if key not in candle_cache:
            # Window: from the grid's deploy (capped) up to now, in 5m candles.
            days = config.detail_max_days
            dep = g.get("deployed_at")
            if dep is not None:
                age = (pd.Timestamp.utcnow() - dep).total_seconds() / 86400
                days = int(max(1, min(config.detail_max_days, age + 1)))
            try:
                raw = await client.market_data.get_candles_last_days(
                    g["connector"], g["trading_pair"], days=days,
                    interval=config.detail_interval,
                )
            except Exception:
                try:
                    raw = await client.market_data.get_candles(
                        g["connector"], g["trading_pair"],
                        interval=config.detail_interval,
                        max_records=days * 288,
                    )
                except Exception as e:
                    logger.debug("5m candles unavailable for %s: %s", key, e)
                    raw = None
            candle_cache[key] = _parse_candles(raw) if raw is not None else pd.DataFrame()

            try:
                prices = await client.market_data.get_prices(g["connector"], g["trading_pair"])
                live = prices.get("prices", {}).get(g["trading_pair"]) if isinstance(prices, dict) else None
                price_cache[key] = _f(live) if live else 0.0
            except Exception:
                price_cache[key] = 0.0

        df = candle_cache[key]
        if df.empty:
            continue
        cur = price_cache.get(key) or df["close"].iloc[-1]

        # Live fills from the bot's container (per-bot, cached), filtered to this pair.
        bn = g["bot_name"]
        if bn not in hist_cache:
            hist_cache[bn] = await _bot_history_trades(client, bn)
        fills = [t for t in hist_cache[bn]
                 if not t["symbol"] or t["symbol"] == g["trading_pair"]]
        # FIFO round-trip matching -> matched/open per fill + break-evens.
        fifo = _fifo_match(fills)

        # Side badge from NET OPEN inventory (what the grid is actually holding).
        ol, osb = fifo["open_long_base"], fifo["open_short_base"]
        tot = ol + osb
        if tot < 1e-9:
            side_label, side_emoji = "NEUTRO", "⚪"
        else:
            r = abs(ol - osb) / tot
            if r < 0.2:
                side_label, side_emoji = "MIXTO", "🔵"
            elif ol > osb:
                side_label, side_emoji = "LONG", "🟢"
            else:
                side_label, side_emoji = "SHORT", "🔴"

        fig = _build_grid_detail_figure(df, g, side_label, side_emoji, cur, fills, fifo)
        fig_levels = _build_level_pnl_figure(g, fifo, cur, g["trading_pair"].split("-")[-1],
                                             config.maker_rebate_pct, fills)
        out.append({
            "grid": g, "side_label": side_label, "side_emoji": side_emoji,
            "fills": fills, "fifo": fifo, "fig": fig, "fig_levels": fig_levels,
        })
    return out


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
async def run(config: Config, context: ContextTypes.DEFAULT_TYPE) -> str:
    client = await get_client(context._chat_id, context=context)
    if not client:
        return "No server available"

    grids, bots = await _collect_grids(client, config.controller_filter)
    if not grids:
        return f"No hay controllers '{config.controller_filter}' activos con rango configurado."

    # Pick the trading pair with the most active grids for the chart.
    pair_counts: dict[tuple, int] = {}
    for g in grids:
        key = (g["connector"], g["trading_pair"])
        pair_counts[key] = pair_counts.get(key, 0) + 1
    (connector, pair), _ = max(pair_counts.items(), key=lambda kv: kv[1])
    chart_grids = [g for g in grids if g["connector"] == connector and g["trading_pair"] == pair]
    other_grids = [g for g in grids if g not in chart_grids]

    # Extend the window so every active grid's deploy timestamp is visible
    # (start a bit before the earliest deploy), capped at 30 days.
    effective_days = config.days
    deploys = [g["deployed_at"] for g in chart_grids if g.get("deployed_at") is not None]
    if deploys:
        days_since = (pd.Timestamp.utcnow() - min(deploys)).total_seconds() / 86400
        effective_days = int(max(config.days, min(30, days_since + 1)))

    # Candles + current price for the charted pair.
    try:
        raw = await client.market_data.get_candles_last_days(
            connector, pair, days=effective_days, interval=config.interval
        )
    except Exception:
        raw = await client.market_data.get_candles(
            connector, pair, interval=config.interval, max_records=effective_days * 24
        )
    df = _parse_candles(raw)
    if df.empty:
        return f"No pude obtener velas para {pair} en {connector}."

    current_price = df["close"].iloc[-1]
    try:
        prices = await client.market_data.get_prices(connector, pair)
        live = prices.get("prices", {}).get(pair) if isinstance(prices, dict) else None
        if live:
            current_price = _f(live, current_price)
    except Exception:
        pass

    # --- Telemetría del controller (custom_info, si el bot la publica) ------- #
    try:
        tele = await _fetch_telemetry(client, grids)
    except Exception as e:
        logger.warning(f"telemetría no disponible: {e}")
        tele = {}
    all_alerts: list[str] = []
    for g in grids:
        all_alerts.extend(_telemetry_alerts(g, (tele.get(g["id"]) or {}).get("latest", {})))

    # --- Aggregates --------------------------------------------------------- #
    n_bots = len({g["bot_name"] for g in grids})
    n_grids = len(grids)
    total_liq = sum(g["liquidity"] for g in grids)
    total_vol = sum(g["volume"] for g in grids)
    total_pnl = sum(g["pnl"] for g in grids)
    total_levels = sum(g["n_levels"] for g in grids)
    in_range = [g for g in chart_grids if g["low"] <= current_price <= g["high"]]
    quote = pair.split("-")[-1] if "-" in pair else "quote"
    base_asset = pair.split("-")[0] if "-" in pair else "base"
    # Projected inventory composition at the current price (aggregate over grids).
    pct_base_now = _aggregate_pct_base(current_price, chart_grids)

    # --- Per-bot aggregation (volume + PnL) --------------------------------- #
    # A bot may run several controllers; aggregate by the bot's clean name
    # (strip the deploy suffix `-YYYYMMDD-HHMMSS`).
    bot_rows: dict[str, dict] = {}
    for g in grids:
        bn = re.sub(r"-\d{8}-\d{6}$", "", g["bot_name"])
        a = bot_rows.setdefault(bn, {"vol": 0.0, "pnl": 0.0, "grids": 0})
        a["vol"] += g["volume"]
        a["pnl"] += g["pnl"]
        a["grids"] += 1
    bot_rows = dict(sorted(bot_rows.items(), key=lambda kv: -kv[1]["vol"]))

    # --- Storytelling text -------------------------------------------------- #
    pnl_sign = "🟢" if total_pnl >= 0 else "🔴"
    pos_txt = (
        f"dentro de {len(in_range)} grilla(s)" if in_range
        else "FUERA de todas las grillas ⚠️"
    )
    lines = [
        f"*Dashboard chessboard_lite · {pair}*",
        "",
        f"🤖 *{n_bots}* bot(s) · 🧩 *{n_grids}* grilla(s) · {total_levels} niveles internos",
        f"💧 Liquidez asignada: *{_fmt(total_liq)} {quote}* (split 50/50 base/quote)",
        f"📊 Volumen operado: *{_fmt(total_vol)} {quote}*",
        f"{pnl_sign} PnL global: *{_fmt(total_pnl, 2)} {quote}*",
        f"🎯 Precio actual *{_fmt(current_price, 2)}* — {pos_txt}",
        f"🧮 Inventario proyectado: *{pct_base_now:.0f}% {base_asset} / {100 - pct_base_now:.0f}% {quote}*",
        "",
        "*Rangos activos:*",
    ]
    for g in sorted(chart_grids, key=lambda x: x["low"]):
        mark = "▶" if g in in_range else " "
        width_pct = (g["high"] - g["low"]) / g["low"] * 100 if g["low"] else 0
        dep = g.get("deployed_at")
        dep_str = f" · 🚀 {dep.strftime('%d-%b %H:%M')} UTC" if dep is not None else ""
        latest_ci = (tele.get(g["id"]) or {}).get("latest", {})
        tele_str = f" · {_tele_side_str(latest_ci)}" if latest_ci else ""
        inv_ci = latest_ci.get("inv_pct")
        inv_str = f" · inv {inv_ci:.0f}%" if inv_ci is not None else ""
        lines.append(
            f"{mark} `{g['id']}` {_fmt(g['low'], 2)}–{_fmt(g['high'], 2)} "
            f"(±{width_pct:.1f}%) · {g['n_levels']} niv · liq {_fmt(g['liquidity'])} · "
            f"banda [{g['inv_floor']:.0f}–{g['inv_ceiling']:.0f}]% · "
            f"vol {_fmt(g['volume'])} · pnl {_fmt(g['pnl'], 1)}{inv_str}{tele_str}{dep_str}"
        )
    if all_alerts:
        lines.append("")
        lines.append("*Alertas de telemetría:*")
        lines.extend(all_alerts)
    if other_grids:
        pairs = ", ".join(sorted({g["trading_pair"] for g in other_grids}))
        lines.append("")
        lines.append(f"_(+{len(other_grids)} grilla(s) en otros pares: {pairs} — no graficadas)_")

    # Per-bot volume + PnL block.
    lines.append("")
    lines.append("*Por bot (volumen · PnL):*")
    for bn, a in bot_rows.items():
        bsign = "🟢" if a["pnl"] >= 0 else "🔴"
        lines.append(
            f"{bsign} `{bn}` · vol {_fmt(a['vol'])} · pnl {_fmt(a['pnl'], 2)} {quote}"
        )
    text = "\n".join(lines)

    # Compact Telegram caption (≤1024 chars): headline + per-bot vol/PnL.
    caption_lines = [
        f"*Chessboard Lite · {pair}*",
        f"🤖 {n_bots} bots · 🧩 {n_grids} grillas · 💧 {_fmt(total_liq)} {quote}",
        f"📊 vol {_fmt(total_vol)} · {pnl_sign} pnl {_fmt(total_pnl, 2)} {quote}",
        f"🎯 {_fmt(current_price, 2)} — {pos_txt}",
        "",
        "*Por bot:*",
    ]
    for bn, a in bot_rows.items():
        bsign = "🟢" if a["pnl"] >= 0 else "🔴"
        caption_lines.append(f"{bsign} {bn}: vol {_fmt(a['vol'])} · pnl {_fmt(a['pnl'], 1)}")
    caption = "\n".join(caption_lines)[:1020]

    # --- Figure ------------------------------------------------------------- #
    fig = _build_figure(df, sorted(chart_grids, key=lambda x: x["low"]),
                        current_price, pair, config.fill_opacity, quote)

    # --- Table -------------------------------------------------------------- #
    table_data = [
        {
            "Grilla": g["id"],
            "Par": g["trading_pair"],
            "Rango": f"{_fmt(g['low'], 2)}–{_fmt(g['high'], 2)}",
            "Niveles": g["n_levels"],
            "Banda inv.": f"{g['inv_floor']:.0f}–{g['inv_ceiling']:.0f}%",
            "Liquidez": _fmt(g["liquidity"]),
            "Volumen": _fmt(g["volume"]),
            "PnL": _fmt(g["pnl"], 2),
            "En rango": "✅" if (g["low"] <= current_price <= g["high"]) else "—",
            "Estado L/S": _tele_side_str((tele.get(g["id"]) or {}).get("latest", {})),
            "Inv %": (f"{(tele.get(g['id']) or {}).get('latest', {}).get('inv_pct'):.0f}"
                      if (tele.get(g["id"]) or {}).get("latest", {}).get("inv_pct") is not None else "—"),
            "Taker": (f"{sum(float(((tele.get(g['id']) or {}).get('latest', {}).get(s) or {}).get('taker_volume_quote', 0) or 0) for s in ('long', 'short')):,.0f}"
                      if (tele.get(g["id"]) or {}).get("latest") else "—"),
        }
        for g in sorted(grids, key=lambda x: (x["trading_pair"], x["low"]))
    ]
    table_columns = ["Grilla", "Par", "Rango", "Niveles", "Banda inv.", "Liquidez", "Volumen", "PnL",
                     "En rango", "Estado L/S", "Inv %", "Taker"]

    # --- KPI cards ---------------------------------------------------------- #
    sections = [
        {"type": "kpi", "label": "Bots activos", "value": str(n_bots)},
        {"type": "kpi", "label": "Grillas activas", "value": str(n_grids)},
        {"type": "kpi", "label": f"Liquidez ({quote})", "value": _fmt(total_liq)},
        {"type": "kpi", "label": f"Volumen ({quote})", "value": _fmt(total_vol)},
        {
            "type": "kpi",
            "label": f"PnL global ({quote})",
            "value": _fmt(total_pnl, 2),
            "delta": f"{total_pnl:+,.2f}",
            "trend": "up" if total_pnl >= 0 else "down",
        },
        {"type": "kpi", "label": "Precio actual", "value": _fmt(current_price, 2)},
        {
            "type": "kpi",
            "label": "Inventario @ precio",
            "value": f"{pct_base_now:.0f}% {base_asset}",
            "delta": f"{100 - pct_base_now:.0f}% {quote}",
        },
    ]

    # --- Per-grid detail (5m + side) --------------------------------------- #
    # All grids, charted pair first, capped inside the helper.
    ordered_grids = sorted(grids, key=lambda x: (x["trading_pair"] != pair, x["low"]))
    try:
        grid_details = await _collect_grid_details(client, ordered_grids, config)
    except Exception as e:
        logger.warning("Per-grid detail failed: %s", e)
        grid_details = []

    # --- HTML report (interactive) ----------------------------------------- #
    try:
        from condor.reports import ReportBuilder

        builder = ReportBuilder("Dashboard Chessboard Lite")
        builder.source("routine", "chessboard_lite_dashboard").tags(["chessboard", "lite", pair])
        builder.manual_order()
        builder.kpi("Bots", str(n_bots))
        builder.kpi("Grillas", str(n_grids))
        builder.kpi(f"Liquidez ({quote})", _fmt(total_liq))
        builder.kpi(f"Volumen ({quote})", _fmt(total_vol))
        builder.kpi(f"PnL global ({quote})", _fmt(total_pnl, 2),
                    delta=f"{total_pnl:+,.2f}", trend="up" if total_pnl >= 0 else "down")
        builder.kpi("Precio actual", _fmt(current_price, 2))
        builder.markdown(
            f"## Resumen ejecutivo\n"
            f"**{n_bots}** bots operando **{n_grids}** grillas chessboard_lite "
            f"({total_levels} niveles internos) sobre **{pair}**.\n\n"
            f"El precio actual **{_fmt(current_price, 2)}** está **{pos_txt}**. "
            f"Liquidez total asignada **{_fmt(total_liq)} {quote}** (cada grilla la "
            f"divide **50/50** en {base_asset}/{quote}), "
            f"volumen acumulado **{_fmt(total_vol)} {quote}**, "
            f"PnL global **{_fmt(total_pnl, 2)} {quote}**.\n\n"
            f"El panel **izquierdo** muestra el *volumen por precio* (volume profile) de la "
            f"ventana: cuánto se operó en cada nivel; la barra ámbar es el **POC** "
            f"(Point of Control, el precio de mayor volumen).\n\n"
            f"**Inventario proyectado a precio actual: {pct_base_now:.0f}% {base_asset} / "
            f"{100 - pct_base_now:.0f}% {quote}.** El panel derecho muestra, para cada nivel "
            f"de precio, cómo quedaría compuesto el capital: barras apiladas con el valor en "
            f"{base_asset} (naranja) y en {quote} (teal). Cada grilla lite carga **100% "
            f"{base_asset} en `min_price`** (abajo) y descarga lineal a **100% {quote} en "
            f"`max_price`** (arriba). La **banda dura** `inv_floor_pct`/`inv_ceiling_pct` "
            f"limita cuánto se desbalancea el inventario (anti-desbalance)."
        )
        builder.plotly(fig)
        builder.table(table_data)
        builder.markdown("## Por bot (volumen · PnL)")
        builder.table([
            {
                "Bot": bn,
                "Grillas": a["grids"],
                f"Volumen ({quote})": _fmt(a["vol"]),
                f"PnL ({quote})": _fmt(a["pnl"], 2),
            }
            for bn, a in bot_rows.items()
        ])

        # --- Telemetría del controller (custom_info) ------------------------ #
        tele_grids = [g for g in grids if (tele.get(g["id"]) or {}).get("latest")]
        if tele_grids or all_alerts:
            alerts_md = ("\n".join(f"- {a}" for a in all_alerts)
                         if all_alerts else "- ✅ sin alertas")
            builder.markdown(
                f"## Telemetría del controller\n"
                f"Publicada por `chessboard_lite` (fix 2026-07-02) vía `get_custom_info` y "
                f"persistida por la API cada ~1s. Grillas con telemetría: "
                f"**{len(tele_grids)}/{len(grids)}** (las demás corren el controller pre-fix).\n\n"
                f"**Alertas:**\n{alerts_md}\n\n"
                f"- *Inventario scopeado vs banda*: la serie debe respirar DENTRO de "
                f"[piso, techo]; pegada a un borde = un lado bloqueado.\n"
                f"- *Canario taker*: con LIMIT_MAKER en ambas patas **debe ser 0 siempre** — "
                f"cualquier valor > 0 es el bug de producción de vuelta (spread + taker fees)."
            )
            for g in tele_grids:
                t = tele[g["id"]]
                latest = t["latest"]
                sides_md = []
                for side in ("long", "short"):
                    sd = latest.get(side) or {}
                    if not sd:
                        continue
                    be = sd.get("break_even")
                    sides_md.append(
                        f"**{side.upper()}**: {sd.get('state', '?')} · niveles reales "
                        f"{sd.get('n_levels_real', '—')} · pos {sd.get('position_quote', 0):,.0f} · "
                        f"pnl {sd.get('realized_pnl_quote', 0):+,.2f} · fees {sd.get('fees_quote', 0):,.2f} · "
                        f"BE {f'{be:,.2f}' if be else '—'} · relanz. {sd.get('relaunches', 0)} · "
                        f"taker {sd.get('taker_volume_quote', 0):,.0f}"
                    )
                builder.markdown(f"### `{g['id']}`\n" + "\n\n".join(sides_md))
                fig_t = _build_telemetry_figure(
                    g["id"], t["hist"], latest.get("inv_band") or
                    [g.get("inv_floor", 10), g.get("inv_ceiling", 90)], quote)
                if fig_t is not None:
                    builder.plotly(fig_t)

        # --- Detalle por grilla: break-even con resolución (spec) ---------- #
        if grid_details:
            builder.markdown(
                f"## Detalle por grilla — break-even con resolución\n"
                f"Sobre los **fills propios del bot de esta corrida** (t ≥ t0 = primer fill, "
                f"leídos en vivo del contenedor vía `get_bot_history`). Un FIFO *price-aware* "
                f"reconstruye los round-trips: un SELL cierra el BUY abierto más viejo por "
                f"debajo (TP del LONG), un BUY cierra el SELL abierto más viejo por encima (TP "
                f"del SHORT); lo que no cierra con ganancia queda **abierto** (inventario "
                f"expuesto).\n\n"
                f"- **Panel izq**: volumen × precio en espejo (SELL ◄ / ► BUY), cada barra "
                f"partida en **matcheado** (sólido = spread realizado) y **abierto** "
                f"(translúcido = riesgo).\n"
                f"- **Panel sup**: OHLC + sombreado del rango desde t0 + líneas **precio "
                f"actual**, **BE buy** (inventario LONG abierto), **BE sell** (SHORT abierto) "
                f"y **BE general** (inventario neto). Si un lado no tiene abierto, no se dibuja "
                f"su BE.\n"
                f"- **Rows B/C**: volumen LONG / SHORT en el tiempo, matcheado vs abierto.\n\n"
                f"Lectura: precio vs cada BE = si ese lado gana sobre lo que tiene abierto; "
                f"matcheado vs abierto = income real vs volumen-vanidad. *Fees descontados del "
                f"matcheado.*"
            )
            for d in grid_details:
                g, fz = d["grid"], d["fifo"]
                be_txt = " · ".join(
                    f"{lbl} {_fmt(v, 2)}" for lbl, v in
                    [("BE buy", fz.get("be_buy")), ("BE sell", fz.get("be_sell")),
                     ("BE gen", fz.get("be_general"))] if v is not None
                ) or "sin inventario abierto"
                builder.markdown(
                    f"### {d['side_emoji']} `{g['id']}` — {d['side_label']} "
                    f"· {g['trading_pair']}\n"
                    f"Rango **{_fmt(g['low'], 2)}–{_fmt(g['high'], 2)}** · {g['n_levels']} niveles · "
                    f"**{len(d['fills'])}** fills · "
                    f"🟢 matcheado **{_fmt(fz.get('matched_quote', 0))}** / "
                    f"🟠 abierto **{_fmt(fz.get('open_quote', 0))}** {quote} · "
                    f"**realizado {_fmt(fz.get('realized', 0), 2)} {quote}** "
                    f"(fees {_fmt(fz.get('fees', 0), 2)}) · {be_txt}."
                )
                builder.plotly(d["fig"])
                if d.get("fig_levels") is not None:
                    builder.markdown(
                        "**Ingreso vs IL por nivel** — la identidad `PnL vs HOLD = "
                        "Σ spread + Σ rebates + PnL del abierto`, con resolución por nivel: "
                        "sólido a la derecha = cobrado (azul LONG / ámbar SHORT / rosa rebates "
                        "estimados); rayado a la izquierda = IL del inventario abierto. Abajo, "
                        "los totalizadores en la misma escala: si la barra verde supera a la "
                        "roja, la grilla se está pagando sola."
                    )
                    builder.plotly(d["fig_levels"])

        await builder.save()
    except Exception as e:
        logger.warning("Report generation failed: %s", e)

    # --- PNG snapshot for Telegram / web ----------------------------------- #
    chart_image = None
    try:
        buf = io.BytesIO()
        fig.write_image(buf, format="png", scale=2, width=1500, height=640)
        chart_image = buf.getvalue()
    except Exception as e:
        logger.debug("PNG export unavailable: %s", e)

    if chart_image:
        try:
            await context.bot.send_photo(
                chat_id=context._chat_id,
                photo=io.BytesIO(chart_image),
                caption=caption,
                parse_mode="Markdown",
            )
        except Exception:
            pass

    # --- Second message: one detail image per active bot/grid -------------- #
    for d in grid_details:
        g, fz = d["grid"], d["fifo"]
        try:
            buf = io.BytesIO()
            d["fig"].write_image(buf, format="png", scale=2, width=1200, height=720)
        except Exception as e:
            logger.debug("Detail PNG export failed for %s: %s", g["id"], e)
            continue
        be_txt = " · ".join(
            f"{lbl} {_fmt(v, 2)}" for lbl, v in
            [("BE buy", fz.get("be_buy")), ("BE sell", fz.get("be_sell")),
             ("BE gen", fz.get("be_general"))] if v is not None
        ) or "sin inventario abierto"
        cap = (
            f"*{d['side_emoji']} {g['id']}* · {d['side_label']} · {g['trading_pair']}\n"
            f"🟢 matcheado {_fmt(fz.get('matched_quote', 0))} / "
            f"🟠 abierto {_fmt(fz.get('open_quote', 0))} {quote} · "
            f"realizado {_fmt(fz.get('realized', 0), 2)} {quote}\n{be_txt}"
        )[:1020]
        try:
            await context.bot.send_photo(
                chat_id=context._chat_id,
                photo=io.BytesIO(buf.getvalue()),
                caption=cap,
                parse_mode="Markdown",
            )
        except Exception:
            pass

    return RoutineResult(
        text=text,
        table_data=table_data,
        table_columns=table_columns,
        chart_image=chart_image,
        sections=sections,
    )
