"""Executive dashboard for active chessboard grid controllers.

Detects which chessboard controllers are running, draws their active grid
ranges as shaded zones over a 1h candlestick chart (overlapping ranges get
visually denser), marks the current price with a dotted line, and produces an
executive summary: how many bots, how many grids, in which ranges, how much
liquidity, volume and PnL.
"""

import io
import logging
import re
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
    """Dashboard ejecutivo de grillas chessboard activas (rangos sombreados + resumen)."""

    controller_filter: str = Field(
        default="chessboard",
        description="Solo controllers cuyo nombre/id contenga este texto",
    )
    interval: str = Field(default="1h", description="Intervalo de las velas")
    days: int = Field(default=7, description="Días de historial de velas a mostrar")
    fill_opacity: float = Field(
        default=0.12,
        description="Opacidad base del sombreado de cada grilla (overlap = más densa)",
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
    """Return (grids, bots) for active chessboard controllers.

    Each grid merges its saved config (range, liquidity) with live performance
    (pnl, volume) from the bot status, plus the bot's deploy timestamp.
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

            # Keep only chessboard-style controllers.
            if flt not in cname.lower() and flt not in str(cid).lower() and flt not in ctrl_name.lower():
                continue

            border_a = _f(cfg.get("border_a"))
            border_b = _f(cfg.get("border_b"))
            if border_a <= 0 and border_b <= 0:
                # No range info -> can't draw it, skip.
                continue
            low, high = sorted((border_a, border_b))

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
                    "n_grids": int(_f(cfg.get("n_grids"), 0)),
                    "liquidity": _f(cfg.get("total_amount_quote")),
                    "base_assigned": _f(cfg.get("base_assigned")),
                    "quote_assigned": _f(cfg.get("quote_assigned")),
                    # Inventory curve: %base = techo at A (low price, loaded),
                    # piso at B (high price, unloaded). For these grids techo=1, piso=0.
                    "techo": _f(cfg.get("techo_pct_btc"), 1.0),
                    "piso": _f(cfg.get("target_pct_btc"), 0.0),
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
            hovertemplate=("P=%{y:,.0f}<br>Volumen: %{x:,.0f} "
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
            hovertemplate=("P=%{y:,.0f}<br>Base: %{x:,.0f} " + quote
                           + " (%{customdata[0]:.0f}%)<extra></extra>"),
        ),
        row=1, col=3,
    )
    fig.add_trace(
        go.Bar(
            y=centers, x=quote_vals, orientation="h", name=f"Quote ({quote})",
            marker_color="rgba(45,212,191,0.85)", width=step * 0.92,
            customdata=(100 - pct_base).reshape(-1, 1),
            hovertemplate=("P=%{y:,.0f}<br>Quote: %{x:,.0f} " + quote
                           + " (%{customdata[0]:.0f}%)<extra></extra>"),
        ),
        row=1, col=3,
    )

    # Current price dotted line across the three panels.
    fig.add_hline(y=current_price, line=dict(color="#facc15", width=1.5, dash="dash"),
                  row=1, col=1)
    fig.add_hline(
        y=current_price, line=dict(color="#facc15", width=1.5, dash="dash"),
        annotation_text=f"  precio actual {_fmt(current_price)}",
        annotation_position="top left", annotation_font_color="#facc15",
        row=1, col=2,
    )
    fig.add_hline(y=current_price, line=dict(color="#facc15", width=1.5, dash="dash"),
                  row=1, col=3)

    fig.update_layout(
        template="plotly_dark",
        title=f"Grillas chessboard activas · {pair}",
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

    # --- Aggregates --------------------------------------------------------- #
    n_bots = len({g["bot_name"] for g in grids})
    n_grids = len(grids)
    total_liq = sum(g["liquidity"] for g in grids)
    total_vol = sum(g["volume"] for g in grids)
    total_pnl = sum(g["pnl"] for g in grids)
    total_levels = sum(g["n_grids"] for g in grids)
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
        f"*Dashboard chessboard · {pair}*",
        "",
        f"🤖 *{n_bots}* bot(s) · 🧩 *{n_grids}* grilla(s) · {total_levels} niveles internos",
        f"💧 Liquidez asignada: *{_fmt(total_liq)} {quote}*",
        f"📊 Volumen operado: *{_fmt(total_vol)} {quote}*",
        f"{pnl_sign} PnL global: *{_fmt(total_pnl, 2)} {quote}*",
        f"🎯 Precio actual *{_fmt(current_price)}* — {pos_txt}",
        f"🧮 Inventario proyectado: *{pct_base_now:.0f}% {base_asset} / {100 - pct_base_now:.0f}% {quote}*",
        "",
        "*Rangos activos:*",
    ]
    for g in sorted(chart_grids, key=lambda x: x["low"]):
        mark = "▶" if g in in_range else " "
        width_pct = (g["high"] - g["low"]) / g["low"] * 100 if g["low"] else 0
        dep = g.get("deployed_at")
        dep_str = f" · 🚀 {dep.strftime('%d-%b %H:%M')} UTC" if dep is not None else ""
        lines.append(
            f"{mark} `{g['id']}` {_fmt(g['low'])}–{_fmt(g['high'])} "
            f"(±{width_pct:.1f}%) · liq {_fmt(g['liquidity'])} · "
            f"vol {_fmt(g['volume'])} · pnl {_fmt(g['pnl'], 1)}{dep_str}"
        )
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
        f"*Grillas chessboard · {pair}*",
        f"🤖 {n_bots} bots · 🧩 {n_grids} grillas · 💧 {_fmt(total_liq)} {quote}",
        f"📊 vol {_fmt(total_vol)} · {pnl_sign} pnl {_fmt(total_pnl, 2)} {quote}",
        f"🎯 {_fmt(current_price)} — {pos_txt}",
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
            "Rango": f"{_fmt(g['low'])}–{_fmt(g['high'])}",
            "Niveles": g["n_grids"],
            "Liquidez": _fmt(g["liquidity"]),
            "Volumen": _fmt(g["volume"]),
            "PnL": _fmt(g["pnl"], 2),
            "En rango": "✅" if (g["low"] <= current_price <= g["high"]) else "—",
        }
        for g in sorted(grids, key=lambda x: (x["trading_pair"], x["low"]))
    ]
    table_columns = ["Grilla", "Par", "Rango", "Niveles", "Liquidez", "Volumen", "PnL", "En rango"]

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
        {"type": "kpi", "label": "Precio actual", "value": _fmt(current_price)},
        {
            "type": "kpi",
            "label": "Inventario @ precio",
            "value": f"{pct_base_now:.0f}% {base_asset}",
            "delta": f"{100 - pct_base_now:.0f}% {quote}",
        },
    ]

    # --- HTML report (interactive) ----------------------------------------- #
    try:
        from condor.reports import ReportBuilder

        builder = ReportBuilder("Dashboard Grillas Chessboard")
        builder.source("routine", "chessboard_grid_dashboard").tags(["chessboard", "grids", pair])
        builder.manual_order()
        builder.kpi("Bots", str(n_bots))
        builder.kpi("Grillas", str(n_grids))
        builder.kpi(f"Liquidez ({quote})", _fmt(total_liq))
        builder.kpi(f"Volumen ({quote})", _fmt(total_vol))
        builder.kpi(f"PnL global ({quote})", _fmt(total_pnl, 2),
                    delta=f"{total_pnl:+,.2f}", trend="up" if total_pnl >= 0 else "down")
        builder.kpi("Precio actual", _fmt(current_price))
        builder.markdown(
            f"## Resumen ejecutivo\n"
            f"**{n_bots}** bots operando **{n_grids}** grillas chessboard "
            f"({total_levels} niveles internos) sobre **{pair}**.\n\n"
            f"El precio actual **{_fmt(current_price)}** está **{pos_txt}**. "
            f"Liquidez total asignada **{_fmt(total_liq)} {quote}**, "
            f"volumen acumulado **{_fmt(total_vol)} {quote}**, "
            f"PnL global **{_fmt(total_pnl, 2)} {quote}**.\n\n"
            f"El panel **izquierdo** muestra el *volumen por precio* (volume profile) de la "
            f"ventana: cuánto se operó en cada nivel; la barra ámbar es el **POC** "
            f"(Point of Control, el precio de mayor volumen).\n\n"
            f"**Inventario proyectado a precio actual: {pct_base_now:.0f}% {base_asset} / "
            f"{100 - pct_base_now:.0f}% {quote}.** El panel derecho del gráfico muestra, "
            f"para cada nivel de precio, cómo quedaría compuesto el capital de las grillas: "
            f"barras apiladas con el valor en {base_asset} (naranja) y en {quote} (teal). "
            f"Debajo de cada rango la grilla está cargada en {base_asset}; por encima, "
            f"descargada a {quote}; adentro transiciona linealmente "
            f"(según `techo_pct_btc`/`target_pct_btc` de cada controller)."
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

    return RoutineResult(
        text=text,
        table_data=table_data,
        table_columns=table_columns,
        chart_image=chart_image,
        sections=sections,
    )
