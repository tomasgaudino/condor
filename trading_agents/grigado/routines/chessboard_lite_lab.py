"""Chessboard Lite Lab — versión de UNA grilla (un LONG + un SHORT) con cuatro
paneles que comparten el eje de precio (Y):

  1) Volumen por precio (izq, reciclado de chessboard_grid_dashboard).
  2) OHLC con soportes/resistencias + precio base (A) y techo (B) de la grilla.
  3) Variación de inventario proyectada (base/quote) de esa grilla, vs precio.
  4) NAV teórico estrategia vs HOLD, vs precio.

COHERENCIA CON EL EXECUTOR (post-auditoría 2026-07-02):
  - La geometría mostrada/emitida sale de `_simulate_executor_levels`, que replica
    la derivación REAL del GridExecutor (grid_executor.py L126-182): min_notional
    del exchange ×1.05, cuantización por min_base_increment, doble cap por
    capital y por step. Lo que ves ES lo que el executor construye.
  - Asume el controller chessboard_lite FIXEADO (TP LIMIT + step derivado de
    n_levels). No deployar sobre el controller viejo.
  - Panel de ECONOMÍA: edge por round-trip neto de fees, cruces/día estimados
    desde velas finas, PnL/día esperado y chequeo f·u > σ²/8.
  - Deploy gateado por: precio en rango + edge > 0 + balances reales (~50/50).

Recicla de chessboard_lab: S/R + velas. De chessboard_grid_dashboard: volume
profile. La curva de inventario/NAV es LOCAL (por nivel, anclada al precio).
"""

CATEGORY = "Analysis"

import importlib.util
import logging
import math
from pathlib import Path
from typing import Literal, Optional

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field, field_validator
from telegram.ext import ContextTypes

from config_manager import get_client
from routines.base import RoutineResult

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Carga dinámica de los módulos hermanos (agent-local: no importables por nombre)
# --------------------------------------------------------------------------- #
def _load_sibling(stem: str):
    path = Path(__file__).resolve().parent / f"{stem}.py"
    spec = importlib.util.spec_from_file_location(f"_lite_helper_{stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_lab = _load_sibling("chessboard_lab")
_dash = _load_sibling("chessboard_grid_dashboard")
_lite_dash = _load_sibling("chessboard_lite_dashboard")  # _collect_grids (bots activos)


class Config(BaseModel):
    """Chessboard Lite Lab — una grilla (LONG+SHORT), 4 paneles sobre el precio."""

    # ── Mercado ──────────────────────────────────────────────────────────────
    trading_pair: str = Field(default="BTC-BRL", description="Par de trading")
    connector_name: str = Field(default="binance", description="Exchange")
    candles_interval: Literal[
        "1m", "3m", "5m", "15m", "30m",
        "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w", "1M"
    ] = Field(default="1d", description="Intervalo de velas para OHLC, S/R y volumen")

    # ── Geometría de la grilla única ─────────────────────────────────────────
    border_a: Optional[float] = Field(default=None, description="Precio BASE (A, borde inferior). Vacío = auto desde soporte.")
    border_b: Optional[float] = Field(default=None, description="Precio TECHO (B, borde superior). Vacío = auto desde resistencia.")
    border_a_sr_idx: int = Field(default=1, description="Índice 1-based del soporte para A (1=más cercano). Ignorado si border_a fijo.")
    border_b_sr_idx: int = Field(default=1, description="Índice 1-based de la resistencia para B (1=más cercana). Ignorado si border_b fijo.")

    # ── Capital ──────────────────────────────────────────────────────────────
    total_amount_quote: float = Field(default=40000.0, description="Capital total (quote). Se parte 50/50 entre grilla LONG y SHORT. ⚠ La WALLET debe estar fondeada ~mitad base / mitad quote — el controller NO convierte.")

    # ── Forma de la grilla (1:1 con ChessboardLiteConfig) ────────────────────
    n_levels: int = Field(default=44, description="Niveles objetivo por grilla. El executor puede capearlos por min_spread/capital/min_notional — la simulación lo muestra.")
    take_profit_pct: float = Field(default=0.2, description="Take profit por nivel, en % (ej 0.2 = 0,2%).")
    min_spread_between_orders: float = Field(default=0.002, description="Spread mínimo entre órdenes (fracción, ej 0.002). Si es mayor al step que piden tus n_levels, CAPA los niveles (la simulación avisa).")
    limit_buffer_pct: float = Field(default=0.5, description="Distancia del stop (limit) más allá de [min,max], en % (ej 0.5 = 0,5%).")
    leverage: int = Field(default=1, description="Apalancamiento (1 = spot).")

    # ── Anti-desbalance (1:1 con ChessboardLiteConfig) ───────────────────────
    inv_floor_pct: float = Field(default=10.0, description="Piso de inventario base, en % del capital.")
    inv_ceiling_pct: float = Field(default=90.0, description="Techo de inventario base, en % del capital.")
    relaunch_margin_pct: float = Field(default=10.0, description="Margen de re-entrada para relanzar, en % del ancho del rango.")
    relaunch_cooldown: int = Field(default=120, description="Segundos a esperar tras cerrar una grilla antes de relanzarla.")

    # ── Economía (fees reales medidos en producción) ─────────────────────────
    fee_maker_pct: float = Field(default=0.045, description="Fee maker por leg, en % (medido ~0.043 en binance BTC-BRL). Ambos legs LIMIT con el controller fixeado.")
    econ_interval: Literal["1m", "5m", "15m", "30m", "1h"] = Field(default="15m", description="Intervalo de velas finas para estimar cruces por nivel.")
    econ_lookback_days: int = Field(default=7, description="Días de velas finas para el estimador de cruces/σ.")

    # ── Sizing interno del lab (alineado al executor) ────────────────────────
    min_order_amount_quote: float = Field(default=5.0, description="min_order_amount_quote del executor (default 5). El min_notional real del exchange se trae de trading-rules y manda el mayor.")

    # ── Detección de S/R ─────────────────────────────────────────────────────
    sr_lookback_days: int = Field(default=90, description="Lookback días para S/R y velas")
    sr_levels_per_side: int = Field(default=5, description="Niveles S/R por lado")

    # ── Emisión del controller config ────────────────────────────────────────
    emit_yml: bool = Field(default=False, description="Si True (tu OK), escribe un .yml del controller con id incremental (no pisa bots/configs corriendo).")

    # ── Deploy en vivo (⚠ FONDOS REALES) ─────────────────────────────────────
    deploy_bot: bool = Field(default=False, description="⚠ Si True: sube el config al server Y levanta un bot en vivo que opera fondos reales. Requiere precio DENTRO del rango + edge>0 + balances suficientes. Default False.")
    deploy_account: str = Field(default="master_account", description="Credentials profile / cuenta para el deploy.")
    max_global_drawdown_quote: Optional[float] = Field(default=None, description="Drawdown global máximo (quote) antes de cortar el bot. Vacío = sin límite.")

    @field_validator("border_a", "border_b", "max_global_drawdown_quote", mode="before")
    @classmethod
    def _empty_str_to_none(cls, v):
        if isinstance(v, str) and v.strip() == "":
            return None
        return v


# --------------------------------------------------------------------------- #
# Simulación FIEL del GridExecutor (grid_executor.py L126-182)
# --------------------------------------------------------------------------- #
async def _fetch_trading_rules(client, connector: str, pair: str) -> dict:
    """Trae min_notional / incrementos del exchange. Fallbacks conservadores si falla."""
    rules = {"min_notional_size": 10.0, "min_base_amount_increment": 1e-5,
             "min_price_increment": 1.0, "source": "fallback"}
    try:
        resp = await client.connectors.get_trading_rules(connector, [pair])
        data = resp.get(pair, resp.get(pair.upper(), resp)) if isinstance(resp, dict) else {}
        if isinstance(data, dict) and data:
            for k in ("min_notional_size", "min_base_amount_increment", "min_price_increment"):
                v = data.get(k)
                if v is not None:
                    rules[k] = float(v)
            rules["source"] = "exchange"
    except Exception as e:
        logger.warning(f"trading-rules falló ({e}); uso fallbacks {rules}")
    return rules


def _simulate_executor_levels(A: float, B: float, capital: float, price: float,
                              n_levels_cfg: int, cfg_min_spread: float,
                              rules: dict, min_order_amount_quote: float) -> dict:
    """Réplica 1:1 de la derivación de niveles del GridExecutor (L126-182),
    INCLUYENDO el step que el controller fixeado le manda:
      min_spread_enviado = max(rango/(n_cfg+0.5), cfg_min_spread)
    Devuelve la geometría REAL que va a operar.
    """
    min_notional = max(min_order_amount_quote, rules["min_notional_size"])
    min_base_increment = rules["min_base_amount_increment"]
    min_notional_margin = min_notional * 1.05
    min_base_amount = max(
        min_notional_margin / price,
        min_base_increment * math.ceil(min_notional / (min_base_increment * price)),
    )
    min_base_amount = math.ceil(min_base_amount / min_base_increment) * min_base_increment
    min_quote_amount = min_base_amount * price

    grid_range = (B - A) / A
    # step que manda el controller fixeado (chessboard_lite._build_grid_config)
    step_sent = max(grid_range / (n_levels_cfg + 0.5), cfg_min_spread)
    min_step = max(step_sent, rules["min_price_increment"] / price)

    max_possible = int(capital / min_quote_amount) if min_quote_amount > 0 else 1
    capped_by = None
    if max_possible == 0:
        n = 1
        quote_per_level = min_quote_amount
        base_per_level = min_base_amount
        capped_by = "capital<min_notional"
    else:
        by_step = int(grid_range / min_step)
        n = max(1, min(max_possible, by_step))
        if by_step < n_levels_cfg:
            capped_by = ("min_spread" if cfg_min_spread >= grid_range / (n_levels_cfg + 0.5)
                         else "price_increment")
        if max_possible < by_step:
            capped_by = "capital/min_notional"
        base_per_level = max(
            min_base_amount,
            math.floor(capital / (price * n) / min_base_increment) * min_base_increment,
        )
        quote_per_level = base_per_level * price
        n = max(1, min(n, int(capital / quote_per_level)))

    if n > 1:
        level_prices = list(np.linspace(A, B, n))
        step_pct = grid_range / (n - 1)
    else:
        level_prices = [(A + B) / 2]
        step_pct = grid_range

    return {
        "n_levels": n, "level_prices": level_prices,
        "order_amount_quote": quote_per_level, "base_per_level": base_per_level,
        "step_pct": step_pct, "capped_by": capped_by,
        "min_quote_amount": min_quote_amount, "rules_source": rules.get("source"),
    }


# --------------------------------------------------------------------------- #
# Proyección de inventario POR NIVEL (única fuente para paneles 3 y 4)
# --------------------------------------------------------------------------- #
def _project_inventory(sim: dict, current_price: float, base0: float,
                       quote0: float, prices: np.ndarray) -> pd.DataFrame:
    """Barrido anclado al precio actual, nivel por nivel (no un escalón único):
      - P < actual: la LONG compró sus niveles en (P, actual] → base sube.
      - P > actual: la SHORT vendió sus niveles en (actual, P] → base baja.
    order_amount por nivel = el del executor simulado. NAV = base·P + quote.
    HOLD = inventario inicial congelado, valorizado a P.
    """
    levels = np.array(sim["level_prices"])
    amt_q = sim["order_amount_quote"]
    rows = []
    for P in prices:
        base, quote = base0, quote0
        if P < current_price:
            crossed = levels[(levels >= P) & (levels < current_price)]
            for L in sorted(crossed, reverse=True):
                if quote >= amt_q:  # la LONG compra con su quote
                    base += amt_q / L
                    quote -= amt_q
        elif P > current_price:
            crossed = levels[(levels > current_price) & (levels <= P)]
            for L in sorted(crossed):
                sell_base = amt_q / L
                if base >= sell_base:  # la SHORT vende su base
                    base -= sell_base
                    quote += amt_q
        nav = base * P + quote
        rows.append({
            "price": P, "base_units": base, "base_val": base * P, "quote_val": quote,
            "pct_base": (base * P / nav * 100.0) if nav > 0 else 0.0,
            "nav_brl": nav, "nav_hold_brl": base0 * P + quote0,
        })
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# Economía: edge por round-trip, cruces/día, PnL esperado, f·u vs σ²/8
# --------------------------------------------------------------------------- #
async def _estimate_economics(client, config: "Config", sim: dict,
                              A: float, B: float, capital: float) -> dict:
    """Estimación desde velas finas. Cruce = vela cuyo [low,high] atraviesa el
    nivel; round-trip ≈ cruces/2 (ida y vuelta). Es una cota gruesa pero honesta."""
    econ = {"ok": False}
    try:
        result = await client.market_data.get_candles_last_days(
            config.connector_name, config.trading_pair.upper(),
            days=config.econ_lookback_days, interval=config.econ_interval,
        )
        records = (result if isinstance(result, list)
                   else result.get("data", result.get("candles", [])) if isinstance(result, dict) else [])
        if not records:
            return econ
        df = pd.DataFrame(records)
        df.columns = [c.lower() for c in df.columns]
        for col in ("open", "high", "low", "close"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["high", "low", "close"])
        if len(df) < 20:
            return econ

        mins_per_bar = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60}[config.econ_interval]
        bars_per_day = 1440.0 / mins_per_bar
        days = len(df) / bars_per_day

        levels = np.array(sim["level_prices"])
        highs, lows = df["high"].to_numpy(), df["low"].to_numpy()
        crossings = sum(int(((lows <= L) & (highs >= L)).sum()) for L in levels)
        roundtrips_day = (crossings / 2.0) / max(days, 1e-9)

        tp = config.take_profit_pct / 100.0
        fee = config.fee_maker_pct / 100.0
        edge_rt = tp - 2.0 * fee  # ambos legs LIMIT (controller fixeado)
        amt = sim["order_amount_quote"]
        pnl_day = roundtrips_day * amt * edge_rt

        rets = np.diff(np.log(df["close"].to_numpy()))
        sigma_daily = float(np.std(rets) * math.sqrt(bars_per_day))
        sigma_annual = sigma_daily * math.sqrt(365.0)

        closes = df["close"].to_numpy()
        time_in_range = float(((closes >= A) & (closes <= B)).mean())

        vol_annual = roundtrips_day * 2.0 * amt * 365.0
        u = vol_annual / capital if capital > 0 else 0.0
        f_eff = edge_rt / 2.0  # edge por unidad de volumen (rt = 2×amt)
        lvr_apr = sigma_annual ** 2 / 8.0
        net_apr = f_eff * u - lvr_apr

        econ.update(ok=True, edge_rt_pct=edge_rt * 100, roundtrips_day=roundtrips_day,
                    pnl_day=pnl_day, sigma_daily_pct=sigma_daily * 100,
                    sigma_annual=sigma_annual, time_in_range_pct=time_in_range * 100,
                    fee_apr=f_eff * u, lvr_apr=lvr_apr, net_apr=net_apr,
                    turnover_u=u, crossings=crossings, days=days)
    except Exception as e:
        logger.warning(f"economía: estimación falló: {e}")
    return econ


# --------------------------------------------------------------------------- #
# Agregador de operaciones — bots activos + la grilla candidata
# --------------------------------------------------------------------------- #
async def _fetch_active_lite_grids(client, config: "Config") -> list[dict]:
    """Bots chessboard_lite ACTIVOS del mismo par/exchange (vía el dashboard,
    misma fuente que el monitoreo → coherencia). Cada uno: rango, capital, pnl vivo."""
    try:
        grids, _bots = await _lite_dash._collect_grids(client, "chessboard_lite")
    except Exception as e:
        logger.warning(f"agregador: _collect_grids falló: {e}")
        return []
    pair = config.trading_pair.upper()
    out = []
    for g in grids:
        if str(g.get("trading_pair", "")).upper() != pair:
            continue
        if g.get("connector") and g["connector"] != config.connector_name:
            continue
        if g.get("low", 0) > 0 and g.get("high", 0) > g.get("low", 0) and g.get("liquidity", 0) > 0:
            out.append(g)
    return out


def _base_units_profile(prices: np.ndarray, specs: list[tuple]) -> np.ndarray:
    """Unidades de base agregadas en cada precio P. Cada grilla lite es un
    RECTÁNGULO de densidad: 100% base en su low → 100% quote en su high (lineal).
    specs: [(low, high, capital_quote), ...]"""
    total = np.zeros_like(prices)
    for low, high, cap in specs:
        frac = np.clip((high - prices) / max(high - low, 1e-9), 0.0, 1.0)
        total += cap * frac / prices  # unidades de base (valor/precio)
    return total


def _pnl_vs_hold(prices: np.ndarray, base_units: np.ndarray, p0: float) -> np.ndarray:
    """PnL mark-to-market vs HOLD en un barrido desde p0. Como el trading en sí es
    NAV-neutro, dNAV = base(p)·dp → PnL_vs_hold(P) = ∫[p0→P] (base(p) − base(p0)) dp.
    Sin fees: es el COSTO DE CONVEXIDAD de la grilla (≤0 en ambos sentidos); lo
    compensan los round-trips (panel de economía). Integración trapezoidal."""
    i0 = int(np.argmin(np.abs(prices - p0)))
    diff = base_units - base_units[i0]
    pnl = np.zeros_like(prices)
    for i in range(i0 + 1, len(prices)):
        pnl[i] = pnl[i - 1] + 0.5 * (diff[i] + diff[i - 1]) * (prices[i] - prices[i - 1])
    for i in range(i0 - 1, -1, -1):
        pnl[i] = pnl[i + 1] - 0.5 * (diff[i] + diff[i + 1]) * (prices[i + 1] - prices[i])
    return pnl


def _range_overlap_pct(A: float, B: float, actives: list[dict]) -> float:
    """% del rango candidato [A,B] ya cubierto por rangos de bots activos."""
    if B <= A or not actives:
        return 0.0
    xs = np.linspace(A, B, 400)
    covered = np.zeros_like(xs, dtype=bool)
    for g in actives:
        covered |= (xs >= g["low"]) & (xs <= g["high"])
    return float(covered.mean() * 100.0)


def _build_aggregator_figure(prices, act_base_val, new_base_val, act_pct, all_pct,
                             act_pnl, all_pnl, current_price, quote: str, pair: str):
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    fig = make_subplots(
        rows=1, cols=3, shared_yaxes=True,
        column_widths=[0.38, 0.27, 0.35], horizontal_spacing=0.03,
        subplot_titles=("Capital en base", "% base agregado", "PnL vs HOLD"),
    )
    for ann in fig.layout.annotations:
        ann.font = dict(size=12, color="#c9d1d9")

    # ── P1: perfil de capital en base por precio — activos (sólido) + nueva (clara)
    fig.add_trace(go.Scatter(
        x=act_base_val, y=prices, mode="lines", name="Bots activos",
        line=dict(color="#f59e0b", width=0.5),
        fill="tozerox", fillcolor="rgba(245,158,11,0.55)",
        hovertemplate="P=%{y:,.2f}<br>activos: %{x:,.0f} " + quote + "<extra></extra>",
    ), row=1, col=1)
    fig.add_trace(go.Scatter(
        x=act_base_val + new_base_val, y=prices, mode="lines", name="+ Nueva grilla",
        line=dict(color="#fbbf24", width=1.5, dash="dot"),
        fill="tonextx", fillcolor="rgba(251,191,36,0.30)",
        hovertemplate="P=%{y:,.2f}<br>con nueva: %{x:,.0f} " + quote + "<extra></extra>",
    ), row=1, col=1)

    # ── P2: %base agregado — antes vs después ─────────────────────────────────
    fig.add_trace(go.Scatter(
        x=act_pct, y=prices, mode="lines", name="%base actual",
        line=dict(color="#a3adb8", width=1.5),
        hovertemplate="P=%{y:,.2f}<br>actual: %{x:.0f}%<extra></extra>",
    ), row=1, col=2)
    fig.add_trace(go.Scatter(
        x=all_pct, y=prices, mode="lines", name="%base con nueva",
        line=dict(color="#3fb950", width=2),
        hovertemplate="P=%{y:,.2f}<br>con nueva: %{x:.0f}%<extra></extra>",
    ), row=1, col=2)

    # ── P3: curva PnL vs HOLD (costo de convexidad) — antes vs después ───────
    fig.add_trace(go.Scatter(
        x=act_pnl, y=prices, mode="lines", name="PnL actual",
        line=dict(color="#a3adb8", width=1.5),
        hovertemplate="P=%{y:,.2f}<br>actual: %{x:,.0f} " + quote + "<extra></extra>",
    ), row=1, col=3)
    fig.add_trace(go.Scatter(
        x=all_pnl, y=prices, mode="lines", name="PnL con nueva",
        line=dict(color="#3fb950", width=2),
        fill="tonextx", fillcolor="rgba(239,68,68,0.18)",
        hovertemplate="P=%{y:,.2f}<br>con nueva: %{x:,.0f} " + quote + "<extra></extra>",
    ), row=1, col=3)
    fig.add_vline(x=0, line=dict(color="#30363d", width=1), row=1, col=3)

    for c in (1, 2, 3):
        fig.add_hline(y=current_price, line=dict(color="#ffffff", width=1, dash="dash"),
                      row=1, col=c)

    fig.update_layout(
        template="plotly_dark", paper_bgcolor="#0e1117", plot_bgcolor="#161b22",
        title=f"Agregador de operaciones · {pair}", height=520,
        hovermode="closest", margin=dict(l=70, r=40, t=70, b=55),
        legend=dict(orientation="h", yanchor="bottom", y=-0.16, xanchor="center", x=0.5),
    )
    fig.update_yaxes(title_text="Precio", gridcolor="#21262d", row=1, col=1)
    fig.update_xaxes(title_text=f"base ({quote})", tickformat=",.2s", gridcolor="#21262d", row=1, col=1)
    fig.update_xaxes(title_text="% base", range=[0, 100], gridcolor="#21262d", row=1, col=2)
    fig.update_xaxes(title_text=f"PnL vs HOLD ({quote})", tickformat=",.2s", gridcolor="#21262d", row=1, col=3)
    return fig


# --------------------------------------------------------------------------- #
# Chequeo de balances para el deploy (⚠ el controller NO convierte)
# --------------------------------------------------------------------------- #
async def _check_deploy_balances(client, config: "Config", price: float) -> tuple[bool, str]:
    """La wallet debe tener ~total/2 en quote Y ~total/2 (en valor) en base.
    Producción murió 2 veces por INSUFFICIENT_BALANCE — este gate lo previene."""
    base_asset, quote_asset = config.trading_pair.upper().split("-")
    need = config.total_amount_quote / 2.0
    margin = 0.98  # 2% de tolerancia
    try:
        state = await client.portfolio.get_state(refresh=True)
    except Exception as e:
        return False, f"no pude leer balances ({e}) — deploy bloqueado por seguridad"
    base_qty = quote_qty = 0.0
    for account_name, account_data in (state or {}).items():
        if config.deploy_account and account_name != config.deploy_account:
            continue
        for connector_balances in account_data.values():
            if not isinstance(connector_balances, list):
                continue
            for b in connector_balances:
                token = str(b.get("token", "")).upper()
                units = float(b.get("units", 0) or 0)
                if token == base_asset:
                    base_qty += units
                elif token == quote_asset:
                    quote_qty += units
    base_val = base_qty * price
    problems = []
    if quote_qty < need * margin:
        problems.append(f"faltan {need - quote_qty:,.0f} {quote_asset} (hay {quote_qty:,.0f}, necesita ~{need:,.0f})")
    if base_val < need * margin:
        problems.append(f"faltan ~{(need - base_val) / price:.5f} {base_asset} (hay {base_qty:.5f} ≈ {base_val:,.0f} {quote_asset}, necesita ~{need:,.0f})")
    if problems:
        return False, "; ".join(problems)
    return True, f"OK — {quote_qty:,.0f} {quote_asset} + {base_qty:.5f} {base_asset} (≈{base_val:,.0f})"


# --------------------------------------------------------------------------- #
# Figura: 4 columnas compartiendo el eje Y (precio)
# --------------------------------------------------------------------------- #
def _build_lite_figure(df, inv_df, vp, sim, supports, resistances,
                       current_price, A, B, y_lo, y_hi, pair, quote, nav_brl,
                       inv_floor_pct, inv_ceiling_pct):
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    base_asset = pair.split("-")[0] if "-" in pair else "base"
    centers, vol, step, poc = vp

    def _money(v):
        return f"{v:,.2f} {quote}"

    fig = make_subplots(
        rows=1, cols=4, shared_yaxes=True,
        column_widths=[0.14, 0.50, 0.18, 0.18], horizontal_spacing=0.012,
        subplot_titles=(
            "Volumen",
            f"OHLC · {pair}",
            "Inventario",
            "NAV vs HOLD",
        ),
    )
    for ann in fig["layout"]["annotations"]:
        ann["font"] = dict(size=12, color="#c9d1d9")

    # ── Col 1: volume profile (horizontal, eje X invertido = barras hacia velas)
    bar_colors = ["rgba(96,165,250,0.65)"] * len(vol)
    if poc >= 0:
        bar_colors[poc] = "rgba(250,204,21,0.95)"  # POC ámbar
    fig.add_trace(go.Bar(
        y=centers, x=vol, orientation="h", width=step * 0.92,
        marker_color=bar_colors, showlegend=False,
        hovertemplate="P=%{y:,.2f}<br>Vol: %{x:,.0f} " + base_asset + "<extra></extra>",
    ), row=1, col=1)

    # ── Col 2: candlestick + S/R + base(A)/techo(B) + niveles SIMULADOS ──────
    fig.add_trace(go.Candlestick(
        x=df["timestamp"], open=df["open"], high=df["high"],
        low=df["low"], close=df["close"], name=pair, showlegend=False,
        increasing_line_color="#26a69a", decreasing_line_color="#ef5350",
        increasing_fillcolor="#26a69a", decreasing_fillcolor="#ef5350",
        line_width=1, whiskerwidth=0.4,
    ), row=1, col=2)
    x0, x1 = df["timestamp"].iloc[0], df["timestamp"].iloc[-1]

    for s in supports:
        fig.add_shape(type="line", x0=x0, x1=x1, y0=s["price"], y1=s["price"],
                      line=dict(color="#22c55e", width=1, dash="dot"), row=1, col=2)
        fig.add_annotation(x=x1, y=s["price"], text=f"  S {_money(s['price'])}",
                           showarrow=False, xanchor="left",
                           font=dict(color="#22c55e", size=10), row=1, col=2)
    for r in resistances:
        fig.add_shape(type="line", x0=x0, x1=x1, y0=r["price"], y1=r["price"],
                      line=dict(color="#ef4444", width=1, dash="dot"), row=1, col=2)
        fig.add_annotation(x=x1, y=r["price"], text=f"  R {_money(r['price'])}",
                           showarrow=False, xanchor="left",
                           font=dict(color="#ef4444", size=10), row=1, col=2)

    # Banda [A,B] sombreada + bordes base/techo.
    fig.add_shape(type="rect", x0=x0, x1=x1, y0=A, y1=B,
                  fillcolor="rgba(253,230,138,0.06)", line_width=0,
                  layer="below", row=1, col=2)
    for lvl, label in [(A, "BASE (A)"), (B, "TECHO (B)")]:
        fig.add_shape(type="line", x0=x0, x1=x1, y0=lvl, y1=lvl,
                      line=dict(color="#fde68a", width=2), row=1, col=2)
        fig.add_annotation(x=x0, y=lvl, text=f"{label} {_money(lvl)}",
                           showarrow=False, xanchor="left", yanchor="bottom",
                           font=dict(color="#fde68a", size=10, family="monospace"),
                           row=1, col=2)

    # Niveles internos = los del EXECUTOR SIMULADO (lo que realmente va a operar).
    for L in sim["level_prices"][1:-1]:
        fig.add_shape(type="line", x0=x0, x1=x1, y0=L, y1=L,
                      line=dict(color="#9ca3af", width=0.4, dash="dot"),
                      opacity=0.45, row=1, col=2)

    # ── Col 3: inventario base/quote por precio — DERIVADO de inv_df (misma
    #    fuente que el panel 4; coherencia total) ──────────────────────────────
    n_bins = 40
    edges = np.linspace(y_lo, y_hi, n_bins + 1)
    inv_centers = (edges[:-1] + edges[1:]) / 2
    inv_step = float(edges[1] - edges[0]) if len(edges) > 1 else 1.0
    base_vals = np.interp(inv_centers, inv_df["price"], inv_df["base_val"])
    quote_vals = np.interp(inv_centers, inv_df["price"], inv_df["quote_val"])
    pct_base = np.interp(inv_centers, inv_df["price"], inv_df["pct_base"])
    fig.add_trace(go.Bar(
        y=inv_centers, x=base_vals, orientation="h", name=f"Base ({base_asset})",
        marker_color="rgba(245,158,11,0.9)", width=inv_step * 0.98,
        customdata=pct_base.reshape(-1, 1),
        hovertemplate="P=%{y:,.2f}<br>Base: %{x:,.0f} " + quote
                      + " (%{customdata[0]:.0f}%)<extra></extra>",
    ), row=1, col=3)
    fig.add_trace(go.Bar(
        y=inv_centers, x=quote_vals, orientation="h", name=f"Quote ({quote})",
        marker_color="rgba(45,212,191,0.85)", width=inv_step * 0.98,
        customdata=(100.0 - pct_base).reshape(-1, 1),
        hovertemplate="P=%{y:,.2f}<br>Quote: %{x:,.0f} " + quote
                      + " (%{customdata[0]:.0f}%)<extra></extra>",
    ), row=1, col=3)
    # Banda de inventario [floor, ceiling]: actúa en RELANZAMIENTOS (no clipea
    # la primera pasada de una grilla viva) — se marca como referencia.
    for i, (pct, lbl) in enumerate(((inv_floor_pct, "piso"), (inv_ceiling_pct, "techo"))):
        xv = nav_brl * pct / 100.0
        fig.add_shape(type="line", x0=xv, x1=xv, y0=y_lo, y1=y_hi,
                      line=dict(color="#fbbf24", width=1.5, dash="dashdot"),
                      opacity=0.9, row=1, col=3)
        y_anchor = y_lo + (y_hi - y_lo) * (0.06 + i * 0.06)
        fig.add_annotation(x=xv, y=y_anchor, text=f"{lbl} {pct:.0f}%",
                           showarrow=False, yanchor="bottom",
                           font=dict(color="#fbbf24", size=10),
                           bgcolor="rgba(14,17,23,0.8)",
                           borderpad=2, row=1, col=3)

    # ── Col 4: NAV estrategia vs HOLD vs precio (misma inv_df) ────────────────
    fig.add_trace(go.Scatter(
        x=inv_df["nav_hold_brl"], y=inv_df["price"], mode="lines",
        name="HOLD (benchmark)", line=dict(color="#a3adb8", width=1, dash="dash"),
        hovertemplate="P=%{y:,.2f}<br>HOLD: %{x:,.0f} " + quote + "<extra></extra>",
    ), row=1, col=4)
    fig.add_trace(go.Scatter(
        x=inv_df["nav_brl"], y=inv_df["price"], mode="lines",
        name="Estrategia", line=dict(color="#3fb950", width=2.5),
        fill="tonextx", fillcolor="rgba(63,185,80,0.15)",
        hovertemplate="P=%{y:,.2f}<br>Estrategia: %{x:,.0f} " + quote + "<extra></extra>",
    ), row=1, col=4)

    # Precio actual: línea punteada blanca cruzando los 4 paneles.
    for c in (1, 2, 3, 4):
        fig.add_hline(y=current_price, line=dict(color="#ffffff", width=1, dash="dash"),
                      row=1, col=c)
    fig.add_annotation(x=x0, y=current_price, text=f"  HOY {_money(current_price)}",
                       showarrow=False, xanchor="left", yanchor="bottom",
                       font=dict(color="#ffffff", size=10), row=1, col=2)

    fig.update_layout(
        template="plotly_dark", paper_bgcolor="#0e1117", plot_bgcolor="#161b22",
        title=f"Chessboard Lite Lab · {pair}", height=680,
        barmode="stack", bargap=0.04, hovermode="closest",
        margin=dict(l=70, r=40, t=80, b=60),
        legend=dict(orientation="h", yanchor="bottom", y=-0.14, xanchor="center", x=0.5),
    )
    fig.update_yaxes(range=[y_lo, y_hi], title_text="Precio", gridcolor="#21262d", row=1, col=1)
    fig.update_xaxes(title_text=f"Vol ({base_asset})", autorange="reversed", row=1, col=1)
    fig.update_xaxes(rangeslider_visible=False, title_text="Tiempo", gridcolor="#21262d", row=1, col=2)
    fig.update_xaxes(title_text=f"Inv ({quote})", tickformat=",.2s", gridcolor="#21262d", row=1, col=3)
    fig.update_xaxes(title_text=f"NAV ({quote})", tickformat=",.2s", gridcolor="#21262d", row=1, col=4)
    return fig


# --------------------------------------------------------------------------- #
# Emisión del controller config (YAML 1:1 con ChessboardLiteConfig)
# --------------------------------------------------------------------------- #
def _price_fmt(v: float) -> str:
    """Precio sin decimales innecesarios (BRL grandes → entero)."""
    return f"{v:.0f}" if abs(v - round(v)) < 1e-6 else f"{v:.2f}"


def _build_controller_yaml(config_id: str, config: "Config", A: float, B: float,
                           sim: dict) -> str:
    """YAML pegable a conf/controllers/<id>.yml — nombres/unidades idénticos a
    ChessboardLiteConfig. Requiere el controller FIXEADO (TP LIMIT + step de n_levels)."""
    return (
        f"# Geometría SIMULADA del executor: {sim['n_levels']} niveles × "
        f"{sim['order_amount_quote']:,.0f} quote/nivel · step {sim['step_pct']:.3%}\n"
        f"# ⚠ Requiere controller chessboard_lite >= fix 2026-07-02 (TP LIMIT).\n"
        f"id: {config_id}\n"
        f"controller_name: chessboard_lite\n"
        f"controller_type: generic\n"
        f"candles_config: []\n"
        f"\n"
        f"connector_name: {config.connector_name}          # ⚠ verificar connector real de {config.trading_pair.upper()}\n"
        f"trading_pair: {config.trading_pair.upper()}\n"
        f"\n"
        f"total_amount_quote: {config.total_amount_quote:.0f}\n"
        f"\n"
        f"min_price: {_price_fmt(A)}\n"
        f"max_price: {_price_fmt(B)}\n"
        f"\n"
        f"n_levels: {config.n_levels}\n"
        f"take_profit_pct: {config.take_profit_pct}\n"
        f"min_spread_between_orders: {config.min_spread_between_orders}\n"
        f"limit_buffer_pct: {config.limit_buffer_pct}\n"
        f"leverage: {config.leverage}\n"
        f"\n"
        f"inv_floor_pct: {config.inv_floor_pct:.0f}\n"
        f"inv_ceiling_pct: {config.inv_ceiling_pct:.0f}\n"
        f"\n"
        f"relaunch_margin_pct: {config.relaunch_margin_pct:.0f}\n"
        f"relaunch_cooldown: {config.relaunch_cooldown}\n"
    )


def _build_controller_config_dict(config_id: str, config: "Config", A: float, B: float) -> dict:
    """Dict para create_or_update_controller_config (mismos valores que el YAML).
    Campos Decimal van como string para coerción limpia en el backend."""
    return {
        "id": config_id,
        "controller_name": "chessboard_lite",
        "controller_type": "generic",
        "connector_name": config.connector_name,
        "trading_pair": config.trading_pair.upper(),
        "total_amount_quote": str(int(config.total_amount_quote)),
        "min_price": _price_fmt(A),
        "max_price": _price_fmt(B),
        "n_levels": int(config.n_levels),
        "take_profit_pct": str(config.take_profit_pct),
        "min_spread_between_orders": str(config.min_spread_between_orders),
        "limit_buffer_pct": str(config.limit_buffer_pct),
        "leverage": int(config.leverage),
        "inv_floor_pct": str(config.inv_floor_pct),
        "inv_ceiling_pct": str(config.inv_ceiling_pct),
        "relaunch_margin_pct": str(config.relaunch_margin_pct),
        "relaunch_cooldown": int(config.relaunch_cooldown),
    }


async def _deploy_controller_bot(client, config: "Config", config_id: str,
                                 config_dict: dict) -> str:
    """Sube el config al server y levanta el bot (deploy_v2_controllers).
    Devuelve un string de estado. ⚠ opera fondos reales."""
    await client.controllers.create_or_update_controller_config(config_id, config_dict)
    logger.info(f"[deploy] config '{config_id}' subido al server")
    result = await client.bot_orchestration.deploy_v2_controllers(
        instance_name=config_id,
        controllers_config=[config_id],
        credentials_profile=config.deploy_account,
        max_global_drawdown_quote=config.max_global_drawdown_quote,
        max_controller_drawdown_quote=None,
        image="hummingbot/hummingbot:latest",
    )
    logger.info(f"[deploy] bot '{config_id}' deployado: {result}")
    return f"🚀 Bot **{config_id}** deployado en `{config.deploy_account}` · {result}"


async def _next_config_id(client, config: "Config", out_dir) -> str:
    """id incremental `chessboard_lite_{pairslug}_{N}` que no pisa ni configs del
    server (bots corriendo / métricas) ni .yml ya generados localmente."""
    pair_slug = config.trading_pair.lower().replace("-", "")
    prefix = f"chessboard_lite_{pair_slug}_"
    taken = set()
    try:
        cfgs = await client.controllers.list_controller_configs()
        items = cfgs.get("configs", cfgs) if isinstance(cfgs, dict) else cfgs
        for c in (items or []):
            cid = c.get("id") if isinstance(c, dict) else getattr(c, "id", None)
            if cid:
                taken.add(str(cid))
    except Exception as e:
        logger.warning(f"No pude listar configs del server (uso solo dir local): {e}")
    for f in out_dir.glob(f"{prefix}*.yml"):
        taken.add(f.stem)
    n = 1
    while f"{prefix}{n}" in taken:
        n += 1
    return f"{prefix}{n}"


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
async def run(config: Config, context: ContextTypes.DEFAULT_TYPE) -> RoutineResult:
    chat_id = context._chat_id if hasattr(context, "_chat_id") else None
    client = await get_client(chat_id, context=context)
    if not client:
        return RoutineResult(text="No server available")

    base_asset, quote_asset = config.trading_pair.upper().split("-")

    # ── 1. Precio del par ────────────────────────────────────────────────────
    price = None
    try:
        resp = await client.market_data.get_prices(config.connector_name, [config.trading_pair.upper()])
        prices = resp.get("prices", resp) if isinstance(resp, dict) else {}
        for k, v in (prices or {}).items():
            if str(k).upper().replace("-", "") == config.trading_pair.upper().replace("-", "") and v:
                price = float(v)
                break
    except Exception as e:
        logger.warning(f"get_prices falló: {e}")

    # ── 2. S/R + velas (reciclado de chessboard_lab) ─────────────────────────
    supports, resistances, df = await _lab._fetch_sr_and_candles(
        client, config.connector_name, config.trading_pair,
        config.sr_lookback_days, config.sr_levels_per_side, config.candles_interval,
    )
    if df is None or df.empty:
        return RoutineResult(text=f"No se pudieron obtener velas para {config.trading_pair}.")
    if price is None:
        price = float(df["close"].iloc[-1])

    # ── 3. Capital: total/2 por grilla. ⚠ la wallet debe estar YA ~50/50 ─────
    nav_brl = float(config.total_amount_quote)
    cap_per_grid = nav_brl / 2.0
    base0 = cap_per_grid / price if price > 0 else 0.0  # base seed de la SHORT
    quote0 = cap_per_grid                                # quote seed de la LONG

    # ── 4. Bordes A (base) / B (techo) ───────────────────────────────────────
    A = config.border_a
    B = config.border_b
    if A is None:
        i = max(0, config.border_a_sr_idx - 1)
        A = supports[i]["price"] if i < len(supports) else (
            supports[-1]["price"] if supports else price * 0.97)
    if B is None:
        i = max(0, config.border_b_sr_idx - 1)
        B = resistances[i]["price"] if i < len(resistances) else (
            resistances[-1]["price"] if resistances else price * 1.03)
    A, B = sorted((float(A), float(B)))
    if B <= A:
        return RoutineResult(text=f"Rango inválido: A={A:,.2f} >= B={B:,.2f}.")

    # ── 5. SIMULACIÓN FIEL del executor (geometría real) ─────────────────────
    rules = await _fetch_trading_rules(client, config.connector_name, config.trading_pair.upper())
    sim = _simulate_executor_levels(
        A, B, cap_per_grid, price, config.n_levels,
        float(config.min_spread_between_orders), rules, config.min_order_amount_quote,
    )

    warnings = []
    if sim["n_levels"] != config.n_levels:
        warnings.append(
            f"⚠ el executor va a crear *{sim['n_levels']}* niveles, no {config.n_levels} "
            f"(capado por {sim['capped_by']}). Para {config.n_levels} niveles bajá "
            f"min_spread_between_orders a ≤ {((B - A) / A) / (config.n_levels + 0.5):.5f}."
        )
    step_pct = sim["step_pct"] * 100
    tp_vs_step = config.take_profit_pct / step_pct if step_pct > 0 else 1.0
    if not (0.75 <= tp_vs_step <= 1.25):
        warnings.append(
            f"⚠ take_profit ({config.take_profit_pct}%) difiere del step efectivo "
            f"({step_pct:.3f}%) en {abs(1 - tp_vs_step) * 100:.0f}% — TP≈step es lo sano."
        )
    if sim["rules_source"] == "fallback":
        warnings.append("⚠ trading-rules del exchange no disponibles — simulación con fallbacks.")

    # ── 5b. Economía: edge, cruces, PnL/día, f·u vs σ²/8 ─────────────────────
    econ = await _estimate_economics(client, config, sim, A, B, nav_brl)
    edge_pos = econ.get("edge_rt_pct", config.take_profit_pct - 2 * config.fee_maker_pct) > 0
    if not edge_pos:
        warnings.append(
            f"⛔ EDGE NEGATIVO: TP {config.take_profit_pct}% − 2×fee {config.fee_maker_pct}% "
            f"= {config.take_profit_pct - 2 * config.fee_maker_pct:.3f}% por round-trip. "
            f"Cada trade PIERDE plata — subí el TP o el spacing."
        )

    # ── 6. Proyección de inventario POR NIVEL (única fuente, paneles 3 y 4) ──
    y_lo = min(float(df["low"].min()), A * 0.95)
    y_hi = max(float(df["high"].max()), B * 1.02)
    proj_prices = np.linspace(A * 0.95, B * 1.02, 120)
    inv_df = _project_inventory(sim, price, base0, quote0, proj_prices)

    # ── 6b. Volume profile ────────────────────────────────────────────────────
    vp = _dash._volume_profile(df, y_lo, y_hi)

    # ── 7. Figura ────────────────────────────────────────────────────────────
    fig = _build_lite_figure(
        df, inv_df, vp, sim, supports, resistances, price, A, B, y_lo, y_hi,
        config.trading_pair.upper(), quote_asset, nav_brl,
        config.inv_floor_pct, config.inv_ceiling_pct,
    )

    # ── 7b. Agregador de operaciones: bots activos + esta grilla candidata ────
    actives = await _fetch_active_lite_grids(client, config)
    act_specs = [(g["low"], g["high"], g["liquidity"]) for g in actives]
    new_spec = (A, B, nav_brl)
    agg_lo = min([A] + [g["low"] for g in actives]) * 0.97
    agg_hi = max([B] + [g["high"] for g in actives]) * 1.03
    agg_prices = np.linspace(agg_lo, agg_hi, 241)

    act_bu = _base_units_profile(agg_prices, act_specs)          # unidades base activos
    new_bu = _base_units_profile(agg_prices, [new_spec])         # unidades base nueva
    act_base_val = act_bu * agg_prices
    new_base_val = new_bu * agg_prices
    act_cap = sum(s[2] for s in act_specs)
    all_cap = act_cap + nav_brl
    act_pct = np.divide(act_base_val, act_cap, out=np.zeros_like(act_base_val),
                        where=act_cap > 0) * 100.0
    all_pct = (act_base_val + new_base_val) / all_cap * 100.0
    act_pnl = _pnl_vs_hold(agg_prices, act_bu, price)
    all_pnl = _pnl_vs_hold(agg_prices, act_bu + new_bu, price)
    overlap = _range_overlap_pct(A, B, actives)
    act_pnl_live = sum(g.get("pnl", 0.0) for g in actives)

    fig_agg = _build_aggregator_figure(
        agg_prices, act_base_val, new_base_val, act_pct, all_pct,
        act_pnl, all_pnl, price, quote_asset, config.trading_pair.upper(),
    )

    # ── 8. Texto / KPIs ──────────────────────────────────────────────────────
    in_range = A <= price <= B
    centers, vol, step, poc = vp
    poc_price = float(centers[poc]) if poc >= 0 else price
    poc_in_range = A <= poc_price <= B

    # ── 8b. Controller config (YAML 1:1) — id incremental + emisión opcional ──
    out_dir = Path(__file__).resolve().parent.parent / "controller_configs"
    out_dir.mkdir(parents=True, exist_ok=True)
    config_id = await _next_config_id(client, config, out_dir)
    yaml_text = _build_controller_yaml(config_id, config, A, B, sim)
    yml_path = None
    if config.emit_yml:
        yml_path = out_dir / f"{config_id}.yml"
        yml_path.write_text(yaml_text, encoding="utf-8")
        yml_status = f"✅ YAML emitido `{config_id}.yml` → `{yml_path}`"
        logger.info(f"Controller YAML emitido: {yml_path}")
    else:
        yml_status = (f"💤 emit_yml=False — preview del próximo id `{config_id}` "
                      f"(no se escribió archivo)")

    # ── 8c. Deploy en vivo (⚠ FONDOS REALES) — gates: doble confirmación +
    #        precio en rango + EDGE > 0 + BALANCES suficientes ────────────────
    deploy_status = "🔒 deploy_bot=False"
    if config.deploy_bot:
        if not config.emit_yml:
            deploy_status = ("⛔ deploy CANCELADO — requiere doble confirmación: poné también "
                             "`emit_yml=True`. No se subió config ni se levantó bot.")
        elif not in_range:
            deploy_status = (f"⛔ deploy CANCELADO — precio {_price_fmt(price)} FUERA del rango "
                             f"[{_price_fmt(A)}, {_price_fmt(B)}].")
        elif not edge_pos:
            deploy_status = ("⛔ deploy CANCELADO — edge por round-trip NEGATIVO "
                             "(TP no cubre 2×fee). Corregí TP/spacing antes de operar.")
        else:
            bal_ok, bal_msg = await _check_deploy_balances(client, config, price)
            if not bal_ok:
                deploy_status = (f"⛔ deploy CANCELADO — balances insuficientes: {bal_msg}. "
                                 f"La wallet debe tener ~{nav_brl / 2:,.0f} {quote_asset} y "
                                 f"~{nav_brl / 2 / price:.5f} {base_asset} (el controller NO convierte).")
            else:
                if yml_path is None:
                    yml_path = out_dir / f"{config_id}.yml"
                    yml_path.write_text(yaml_text, encoding="utf-8")
                try:
                    cfg_dict = _build_controller_config_dict(config_id, config, A, B)
                    deploy_status = await _deploy_controller_bot(client, config, config_id, cfg_dict)
                    deploy_status += f" · balances {bal_msg}"
                except Exception as e:
                    deploy_status = f"❌ deploy FALLÓ: {e}"
                    logger.exception("Deploy del bot falló")
        if deploy_status.startswith("⛔"):
            logger.warning(deploy_status)

    def _m(v):
        return f"{v:,.2f} {quote_asset}"

    econ_lines = []
    if econ.get("ok"):
        econ_lines = [
            f"💰 Edge/round-trip *{econ['edge_rt_pct']:+.3f}%* (TP {config.take_profit_pct}% − 2×fee {config.fee_maker_pct}%)",
            f"🔁 Round-trips/día est. *{econ['roundtrips_day']:.0f}* → PnL/día est. *{econ['pnl_day']:+,.0f} {quote_asset}*",
            f"📈 σ diaria {econ['sigma_daily_pct']:.2f}% · time-in-range {econ['time_in_range_pct']:.0f}% "
            f"({config.econ_lookback_days}d @ {config.econ_interval})",
            f"⚖️ f·u={econ['fee_apr'] * 100:.0f}% vs σ²/8={econ['lvr_apr'] * 100:.1f}% → net "
            f"*{econ['net_apr'] * 100:+.0f}% APR* {'✅' if econ['net_apr'] > 0 else '⛔'}",
        ]

    lines = [
        f"*Chessboard Lite Lab · {config.trading_pair.upper()}*",
        "",
        f"🎯 Precio actual *{_m(price)}* — {'DENTRO' if in_range else 'FUERA'} de la grilla",
        f"🧱 Base (A) *{_m(A)}* · Techo (B) *{_m(B)}* (±{(B - A) / A * 100:.1f}%)",
        f"💧 Capital *{_m(nav_brl)}* (½ por grilla) — ⚠ la wallet debe estar ~50/50 base/quote, el controller NO convierte",
        f"🧩 Executor simulado: *{sim['n_levels']} niveles* × {sim['order_amount_quote']:,.0f} {quote_asset}/nivel · step {sim['step_pct']:.3%}",
        f"📊 POC: *{_m(poc_price)}* {'(dentro del rango ✅)' if poc_in_range else '(FUERA del rango ⚠ — el volumen vive en otro lado)'}",
        *econ_lines,
        f"🤝 Agregador: *{len(actives)} bots activos* ({act_cap:,.0f} {quote_asset} desplegados, "
        f"PnL vivo {act_pnl_live:+,.0f}) + nueva → total {all_cap:,.0f} · "
        f"solapamiento del rango nuevo {overlap:.0f}%",
        *warnings,
        f"📄 {yml_status}",
        f"🤖 {deploy_status}",
    ]
    text = "\n".join(lines)

    sections = [
        {"type": "kpi", "label": "Precio actual", "value": _m(price)},
        {"type": "kpi", "label": "Base (A)", "value": _m(A)},
        {"type": "kpi", "label": "Techo (B)", "value": _m(B)},
        {"type": "kpi", "label": "Niveles (executor)", "value": str(sim["n_levels"])},
        {"type": "kpi", "label": "Edge/RT",
         "value": f"{config.take_profit_pct - 2 * config.fee_maker_pct:+.3f}%"},
        {"type": "kpi", "label": "POC", "value": _m(poc_price)},
    ]

    table_data = [{
        "Grilla": side,
        "Rango": f"{A:,.2f}–{B:,.2f}",
        "Niveles": sim["n_levels"],
        "Capital": f"{cap_per_grid:,.0f}",
        "Order amt": f"{sim['order_amount_quote']:,.0f}",
        "Spread ef.": f"{sim['step_pct']:.3%}",
    } for side in ("LONG", "SHORT")]
    table_columns = ["Grilla", "Rango", "Niveles", "Capital", "Order amt", "Spread ef."]

    # ── 9. Reporte interactivo ───────────────────────────────────────────────
    try:
        from condor.reports import ReportBuilder

        builder = ReportBuilder("Chessboard Lite Lab")
        builder.source("routine", "chessboard_lite_lab").tags(
            ["chessboard", "lite", config.trading_pair.upper()])
        builder.manual_order()
        builder.kpi("Precio actual", _m(price))
        builder.kpi("Base (A)", _m(A))
        builder.kpi("Techo (B)", _m(B))
        builder.kpi("Niveles (executor)", str(sim["n_levels"]))
        builder.kpi("Edge/RT", f"{config.take_profit_pct - 2 * config.fee_maker_pct:+.3f}%")
        econ_md = ""
        if econ.get("ok"):
            econ_md = (
                f"\n### Economía estimada ({config.econ_lookback_days}d @ {config.econ_interval})\n"
                f"| Métrica | Valor |\n|---|---|\n"
                f"| Edge por round-trip (TP − 2×fee) | **{econ['edge_rt_pct']:+.3f}%** |\n"
                f"| Round-trips/día estimados | {econ['roundtrips_day']:.0f} |\n"
                f"| PnL/día esperado | **{econ['pnl_day']:+,.0f} {quote_asset}** |\n"
                f"| σ diaria / anual | {econ['sigma_daily_pct']:.2f}% / {econ['sigma_annual'] * 100:.0f}% |\n"
                f"| Time-in-range | {econ['time_in_range_pct']:.0f}% |\n"
                f"| f·u (fee APR) | {econ['fee_apr'] * 100:.0f}% |\n"
                f"| σ²/8 (LVR APR) | {econ['lvr_apr'] * 100:.1f}% |\n"
                f"| **Net APR (f·u − σ²/8)** | **{econ['net_apr'] * 100:+.0f}%** |\n"
            )
        warn_md = ("\n".join(f"> {w}" for w in warnings) + "\n") if warnings else ""
        builder.markdown(
            f"## Lite Lab — una grilla sobre **{config.trading_pair.upper()}**\n"
            f"Grilla única **LONG + SHORT** entre **base A {_m(A)}** y **techo B {_m(B)}** "
            f"(±{(B - A) / A * 100:.1f}%). Capital total **{_m(nav_brl)}**, ½ por grilla. "
            f"⚠ **La wallet debe estar fondeada ~50/50 {base_asset}/{quote_asset} — el "
            f"controller NO convierte.**\n\n"
            f"**Geometría simulada del executor** (réplica de grid_executor L126-182, "
            f"rules={sim['rules_source']}): **{sim['n_levels']} niveles** × "
            f"{sim['order_amount_quote']:,.0f} {quote_asset}/nivel, step {sim['step_pct']:.3%}.\n"
            f"{warn_md}"
            f"{econ_md}\n"
            f"Los cuatro paneles comparten el **eje de precio**: volumen por precio (POC "
            f"{_m(poc_price)}), OHLC con S/R y bordes A/B + niveles del executor, "
            f"inventario proyectado por nivel (misma fuente que el NAV), y NAV estrategia "
            f"vs HOLD."
        )
        builder.plotly(fig)
        builder.table(table_data)

        # ── Agregador de operaciones ──────────────────────────────────────────
        agg_rows = [{
            "Bot": g["id"],
            "Rango": f"{g['low']:,.2f}–{g['high']:,.2f}",
            "Capital": f"{g['liquidity']:,.0f}",
            "PnL vivo": f"{g.get('pnl', 0):+,.2f}",
            "Volumen": f"{g.get('volume', 0):,.0f}",
        } for g in actives]
        agg_rows.append({
            "Bot": "➕ NUEVA (esta config)",
            "Rango": f"{A:,.2f}–{B:,.2f}",
            "Capital": f"{nav_brl:,.0f}",
            "PnL vivo": "—",
            "Volumen": "—",
        })
        builder.markdown(
            f"## Agregador de operaciones\n"
            f"Impacto de sumar **esta grilla** al portfolio de bots `chessboard_lite` "
            f"activos en {config.trading_pair.upper()} ({config.connector_name}).\n\n"
            f"- Bots activos: **{len(actives)}** · capital desplegado **{act_cap:,.0f} {quote_asset}** "
            f"· PnL vivo **{act_pnl_live:+,.0f} {quote_asset}**\n"
            f"- Con la nueva: capital total **{all_cap:,.0f} {quote_asset}** · "
            f"solapamiento del rango nuevo con los activos: **{overlap:.0f}%**\n\n"
            f"**Cómo leer los paneles** (cada grilla = un rectángulo de densidad, "
            f"proyección lineal anclada 50/50 al precio de hoy):\n"
            f"- *Capital en base*: dónde queda apilada la densidad — el área clara es "
            f"lo que agrega la nueva.\n"
            f"- *% base agregado*: cómo se deforma la curva de inventario del portfolio.\n"
            f"- *PnL vs HOLD*: costo de convexidad del barrido (≤0 SIN fees — es la cara "
            f"IL/LVR de la grilla). El área roja entre curvas = convexidad extra que agrega "
            f"la nueva; se financia con sus round-trips (ver panel de economía: "
            f"{('PnL/día est. ' + format(econ['pnl_day'], '+,.0f') + ' ' + quote_asset) if econ.get('ok') else 'correr con econ disponible'})."
        )
        builder.plotly(fig_agg)
        builder.table(agg_rows)

        _emit_note = (f"**{yml_status}**\n\n" if config.emit_yml else
                      f"_{yml_status}. Marcá `emit_yml=True` para escribir el archivo._\n\n")
        builder.markdown(
            f"## Controller config — `chessboard_lite` (1:1, pegable)\n\n"
            f"{_emit_note}"
            f"**Deploy:** {deploy_status}\n\n"
            f"_Guardar como_ `conf/controllers/{config_id}.yml`\n\n"
            f"```yaml\n{yaml_text}```\n"
        )
        await builder.save()
    except Exception as e:
        logger.warning("Report generation failed: %s", e)

    # ── 10. PNG snapshot ─────────────────────────────────────────────────────
    chart_image = None
    try:
        import io
        buf = io.BytesIO()
        fig.write_image(buf, format="png", scale=2, width=1600, height=680)
        chart_image = buf.getvalue()
    except Exception as e:
        logger.debug("PNG export unavailable: %s", e)

    return RoutineResult(
        text=text,
        table_data=table_data,
        table_columns=table_columns,
        chart_image=chart_image,
        sections=sections,
    )
