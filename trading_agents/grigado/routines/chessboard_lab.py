"""Laboratorio del Tablero v2 — tabla comparativa de grillas + análisis profundo de selected_grid."""

CATEGORY = "Analysis"

import logging
import math
from typing import Literal, Optional

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field, field_validator
from telegram.ext import ContextTypes

from config_manager import get_client
from routines.base import RoutineResult

logger = logging.getLogger(__name__)

BINANCE_MIN_NOTIONAL_BRL = 20.0
BINANCE_MIN_ORDER_BTC = 0.00001


class Config(BaseModel):
    """Laboratorio del Tablero v2 — tabla comparativa de grillas + análisis profundo."""

    # ── Mercado ──────────────────────────────────────────────────────────────
    trading_pair: str = Field(default="BTC-BRL", description="Par de trading")
    connector_name: str = Field(default="binance", description="Exchange")
    candles_interval: Literal[
        "1m", "3m", "5m", "15m", "30m",
        "1h", "2h", "4h", "6h", "8h", "12h",
        "1d", "3d", "1w", "1M"
    ] = Field(default="1d", description="Intervalo de velas para S/R y gráfico")

    # ── Geometría del tablero (rango y nº de grillas) ────────────────────────
    border_a: Optional[float] = Field(default=None, description="Borde inferior A. Vacío = auto desde S/R")
    border_b: Optional[float] = Field(default=None, description="Borde superior B. Vacío = auto desde S/R")
    border_a_sr_idx: int = Field(default=2, description="Índice 1-based del soporte a usar como A (1=más cercano al precio). Ignorado si border_a está fijo.")
    border_b_sr_idx: int = Field(default=1, description="Índice 1-based de la resistencia a usar como B (1=más cercana al precio). Ignorado si border_b está fijo.")
    min_grids: int = Field(default=3, description="Mínimo de subrangos (N) a explorar en la tabla")
    max_grids: int = Field(default=10, description="Máximo de subrangos (N) a explorar en la tabla")
    selected_grid: int = Field(default=4, description="N de grillas para el análisis profundo (entre min y max)")

    # ── Capital (COMPONIBLE: NAV asignado + %BTC inicial) ────────────────────
    # Asignás un NAV total (en quote del par) y cómo está compuesto (%BTC inicial).
    # De ahí se derivan base (BTC) y quote (líquido):
    #   base  = nav × pct_btc_inicial / precio_par
    #   quote = nav × (1 - pct_btc_inicial)
    # Ej: nav 50000 BRL + pct_btc 1.0 -> TODO BTC (0.156 BTC), 0 líquido.
    #     nav 50000 + pct_btc 0.8     -> 0.125 BTC + 10000 líquido.
    # El tablero opera SOLO sobre ese NAV (aislado; no ve el resto del portfolio).
    nav_assigned: Optional[float] = Field(default=None, description="NAV total asignado al tablero, en quote del par (ej 50000 BRL). Vacío = todo el NAV global de la cuenta.")
    pct_btc_inicial: Optional[float] = Field(default=None, description="%BTC inicial del NAV asignado (1.0=100% en BTC, 0.0=todo líquido). Vacío = usa el %BTC global de la cuenta.")
    # Los DOS extremos de la curva de inventario (define qué tan agresiva):
    #   target_pct_btc = PISO (en B, precio alto, descargado al máximo).
    #   techo_pct      = TECHO (en A, precio bajo, cargado al máximo).
    # Recorrido = techo - piso. Más amplio = más agresivo (mueve más inventario).
    target_pct_btc: float = Field(default=0.60, description="PISO de %BTC (target de descarga, en B). 0.40 = 40%")
    techo_pct_btc: float = Field(default=1.0, description="TECHO de %BTC (carga máxima, en A). 1.0 = 100%. Define cuánto cargan las LONG.")
    hysteresis_pct: float = Field(default=0.1, description="Histéresis (fracción del ancho del escalón). No puebla la grilla si el precio está pegado a un borde. Evita el churn de oscilación. 0 = sin histéresis.")
    target_tolerance_pct: float = Field(default=0.01, description="Tolerancia alrededor del target local (puntos de %BTC, 0.01 = 1pp). Deja vivir a AMBOS lados del par con NAV·tol dentro de la banda [tl−tol, tl+tol] → el par cicla y hace volumen. Desvío máx vs teórico = ±tol (techo 97 puede tocar 98). 0 = bloqueo binario (en target exacto el par muere y no hay volumen).")
    min_order_amount_quote: float = Field(default=20.0, description="Min notional de Binance por orden (R$). Capa los niveles posibles por capital.")

    # ── Perfil de la grilla (niveles internos) ───────────────────────────────
    # Dos formas de definir la densidad de órdenes dentro de cada grilla:
    #  - target_levels_per_grid: fijás los niveles -> la routine DESPEJA el spread.
    #  - spread_per_subrange: fijás el spread -> los niveles salen del ancho/spread.
    # Si target_levels_per_grid está seteado, MANDA y spread_per_subrange se ignora.
    target_levels_per_grid: Optional[int] = Field(default=None, description="Niveles objetivo por grilla. Vacío = usar spread_per_subrange. Si seteado, despeja el spread necesario.")
    spread_per_subrange: float = Field(default=0.002, description="Spread entre órdenes (fracción, 0.001=0.1%). Se IGNORA si target_levels_per_grid está seteado.")
    take_profit_per_level: float = Field(default=0.0004, description="TP por nivel (fracción, 0.0004=0.04%). Cada nivel cierra a ±TP. Chico = más ciclos = más rebates. Solo afecta el YAML.")
    limit_distance_pct: float = Field(default=0.005, description="Distancia extra del limit más allá del borde de la banda (fracción)")

    # ── Operativos del executor (calibran cuántos trades hacés) ──────────────
    # Defaults orientados a MUCHOS TRADES (rebate farming): TP chico + max_open_orders
    # alto (no ahogar los niveles) + batch grande + cooldown corto. Solo afectan el YAML.
    max_open_orders: int = Field(default=20, description="Máx órdenes simultáneas por grilla. Alto = no ahoga los niveles densos. Ojo límite de órdenes de Binance.")
    max_orders_per_batch: int = Field(default=5, description="Máx órdenes que coloca por tick. Más alto = puebla la grilla más rápido.")
    order_frequency: int = Field(default=2, description="Cooldown (s) entre batches. Más bajo = repuebla más rápido.")

    # ── Detección de S/R ─────────────────────────────────────────────────────
    sr_lookback_days: int = Field(default=90, description="Lookback días para detección de S/R")
    sr_levels_per_side: int = Field(default=5, description="Niveles S/R por lado")

    # ── Experimental ─────────────────────────────────────────────────────────
    inverse_siding: bool = Field(default=False, description="Inversión de side en relevo entre grillas consecutivas (no-op hoy)")

    @field_validator("nav_assigned", "pct_btc_inicial", "border_a", "border_b",
                     "target_levels_per_grid", mode="before")
    @classmethod
    def _empty_str_to_none(cls, v):
        """La UI de Condor manda los campos vacíos como '' (string), no None.
        pydantic no parsea '' a número -> lo convertimos a None (= tomar del balance
        / auto desde S/R / usar el otro criterio, según el campo)."""
        if isinstance(v, str) and v.strip() == "":
            return None
        return v


def _brl(v: float) -> str:
    return f"R$ {v:,.0f}"

def _compact(v: float) -> str:
    if abs(v) >= 1_000_000:
        return f"R$ {v/1_000_000:.2f}M"
    if abs(v) >= 1_000:
        return f"R$ {v/1_000:.1f}k"
    return f"R$ {v:.0f}"

def _btc(v: float) -> str:
    return f"{v:.6f}"


async def _fetch_sr_and_candles(
    client, connector: str, trading_pair: str,
    lookback_days: int, n_levels: int, interval: str
) -> tuple[list[dict], list[dict], pd.DataFrame | None]:
    result = await client.market_data.get_candles_last_days(
        connector, trading_pair, days=lookback_days, interval=interval
    )
    records = (
        result if isinstance(result, list)
        else result.get("data", result.get("candles", [])) if isinstance(result, dict)
        else []
    )
    if not records:
        return [], [], None

    df = pd.DataFrame(records)
    df.columns = [c.lower() for c in df.columns]
    for col in ["open", "high", "low", "close", "volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    ts = df["timestamp"]
    unit = "s" if ts.iloc[0] < 1e12 else "ms"
    df["timestamp"] = pd.to_datetime(ts, unit=unit, utc=True)
    df = df.sort_values("timestamp").reset_index(drop=True)

    if len(df) < 10:
        return [], [], df

    current_price = float(df["close"].iloc[-1])
    now = df["timestamp"].iloc[-1]

    def _find_pivots(window, ptype):
        pivots = []
        n = len(df)
        for i in range(window, n - window):
            if df["high"].iloc[i] == df["high"].iloc[i - window: i + window + 1].max():
                pivots.append({"price": float(df["high"].iloc[i]), "ts": df["timestamp"].iloc[i], "side": "resistance", "type": ptype})
            if df["low"].iloc[i] == df["low"].iloc[i - window: i + window + 1].min():
                pivots.append({"price": float(df["low"].iloc[i]), "ts": df["timestamp"].iloc[i], "side": "support", "type": ptype})
        return pivots

    structural = _find_pivots(10, "structural")
    tactical = _find_pivots(3, "tactical")
    struct_prices = {
        "resistance": [s["price"] for s in structural if s["side"] == "resistance"],
        "support": [s["price"] for s in structural if s["side"] == "support"],
    }
    all_pivots = list(structural)
    for t in tactical:
        if not any(abs(t["price"] - sp) / max(current_price, 1) * 100 <= 0.5 for sp in struct_prices[t["side"]]):
            all_pivots.append(t)

    def _cluster(pivots):
        if not pivots:
            return []
        sp = sorted(pivots, key=lambda x: x["price"])
        clusters = [[sp[0]]]
        for p in sp[1:]:
            last = float(np.mean([x["price"] for x in clusters[-1]]))
            if abs(p["price"] - last) / max(current_price, 1) * 100 <= 0.5:
                clusters[-1].append(p)
            else:
                clusters.append([p])
        result = []
        for c in clusters:
            touches = len(c)
            avg_price = float(np.mean([x["price"] for x in c]))
            last_ts = max(x["ts"] for x in c)
            last_days = (now - last_ts).days
            is_struct = any(x["type"] == "structural" for x in c)
            s_t = max(0.0, min(1.0, (touches - 1) / 4))
            if last_days < 14:
                s_r = 0.15
            elif last_days > 365:
                s_r = 0.30
            else:
                s_r = max(0.0, min(1.0, 1.0 - abs(math.log(max(last_days, 1)) - math.log(90)) / math.log(6)))
            s_s = 1.0 if is_struct else 0.4
            score = 0.55 * s_t + 0.30 * s_r + 0.15 * s_s
            result.append({"price": avg_price, "side": c[0]["side"], "touches": touches, "last_days": last_days, "score": score})
        return result

    res_raw = _cluster([p for p in all_pivots if p["side"] == "resistance"])
    sup_raw = _cluster([p for p in all_pivots if p["side"] == "support"])
    # Orden por PROXIMIDAD al precio actual (no por score): índice 1 = el más cercano.
    # Resistencias (>= precio): ascendente por precio. Soportes (<= precio): descendente.
    resistances = sorted([r for r in res_raw if r["price"] >= current_price], key=lambda x: x["price"])[:n_levels]
    supports = sorted([s for s in sup_raw if s["price"] <= current_price], key=lambda x: -x["price"])[:n_levels]
    return supports, resistances, df


def _descarga_para_target(base_btc: float, quote_brl: float, price: float,
                          target_pct_btc: float, n: int) -> dict:
    """Dimensionamiento al TARGET: cuánto debe vender cada SHORT para que las N
    grillas, juntas, lleven el %BTC de actual -> target en N saltos parejos.

    Modelo (igual que el controller, quote valuado a precio actual):
      %BTC = (btc*p) / (btc*p + quote).  Vendés x BTC -> btc'=btc-x, quote'=quote+x*p.
    Resolvemos el x_total que da exactamente el target, y lo repartimos en N.

    NOTA clave: total/N/2 NO está calibrado al target (está atado al NAV total, que
    suele ser MUCHO mayor que lo que hay que descargar). Por eso con total/N/2 una
    sola grilla ya cruza el target y el corte frena el resto (1 salto, no N). Acá
    calculamos el capital que SÍ da N saltos.

    Devuelve: btc_total (a vender), btc_por_grilla, cap_por_grilla (R$, al precio
    actual; sube levemente por escalón si se usa el centro de masa real), pct_actual.
    """
    nav = base_btc * price + quote_brl
    pct_actual = (base_btc * price / nav) if nav > 0 else 0.0
    if pct_actual <= target_pct_btc:
        # ya estás en o por debajo del target: no hay que descargar
        return {"btc_total": 0.0, "btc_por_grilla": 0.0, "cap_por_grilla": 0.0,
                "pct_actual": pct_actual, "descarga_pts": 0.0}
    # Despeje cerrado: (base-x)*p / ((base-x)*p + quote + x*p) = target
    #   (base-x)*p = target * (base*p + quote)   [el denominador NAV es invariante a x:
    #    btc baja x*p, quote sube x*p -> NAV constante = base*p+quote]
    # -> base*p - x*p = target*nav -> x = (base*p - target*nav) / p
    x_total = (base_btc * price - target_pct_btc * nav) / price
    x_total = max(0.0, x_total)
    btc_por_grilla = x_total / n
    cap_por_grilla = btc_por_grilla * price  # capital BRL nominal por grilla (al precio actual)
    return {"btc_total": x_total, "btc_por_grilla": btc_por_grilla,
            "cap_por_grilla": cap_por_grilla, "pct_actual": pct_actual,
            "descarga_pts": (pct_actual - target_pct_btc) * 100}


def _recorrido_inventario(base_btc: float, quote_brl: float, price: float,
                          piso_pct: float, techo_pct: float) -> dict:
    """Recorrido COMPLETO de inventario entre dos extremos (define la agresividad):
      - PISO  (target): %BTC mínimo, en B (precio alto) -> las SHORT descargan hasta acá.
      - TECHO: %BTC máximo, en A (precio bajo) -> las LONG cargan hasta acá.

    El NAV es invariante a comprar/vender (intercambiás BTC<->BRL al precio), así que:
      btc_para(%) = % * NAV / price.
    Desde el %BTC actual:
      descarga (SHORT, arriba) = btc_actual - btc_piso
      carga    (LONG,  abajo)  = btc_techo  - btc_actual
    """
    nav = base_btc * price + quote_brl
    pct_actual = (base_btc * price / nav) if nav > 0 else 0.0
    btc_piso = piso_pct * nav / price
    btc_techo = techo_pct * nav / price
    btc_descarga = max(0.0, base_btc - btc_piso)   # BTC a vender (SHORT)
    btc_carga = max(0.0, btc_techo - base_btc)     # BTC a comprar (LONG)
    return {
        "pct_actual": pct_actual,
        "btc_descarga": btc_descarga, "brl_descarga": btc_descarga * price,
        "btc_carga": btc_carga, "brl_carga": btc_carga * price,
        "btc_piso": btc_piso, "btc_techo": btc_techo,
    }


def _resolve_grid_profile(ancho_grilla: float, capital_grilla: float,
                          btc_brl_price: float, spread_per_subrange: float,
                          target_levels: Optional[int], min_order: float) -> dict:
    """Resuelve el perfil interno de UNA grilla. Dos modos:

    - target_levels seteado: DESPEJA el spread necesario para esos niveles
      (spread = ancho / (niveles × precio)). Se respeta tu número aunque el
      capital no alcance (cada nivel < min_order) -> se avisa con capped_by_capital.
    - target_levels vacío: usa spread_per_subrange y deriva los niveles del
      ancho/spread, CAPADO por capital (como hace el executor real:
      niveles = min(ancho/step, capital/min_order)).

    Devuelve: niveles, spread_efectivo (fracción), order_amount (BRL/nivel),
    capped_by_capital (bool), source ('target'|'spread').
    """
    if target_levels and target_levels > 0:
        niveles = int(target_levels)
        spread_efectivo = (ancho_grilla / niveles) / btc_brl_price if niveles > 0 else 0.0
        order_amount = capital_grilla / niveles if niveles > 0 else capital_grilla
        # el executor capará por min_order; avisamos pero respetamos el número.
        capped = order_amount < min_order
        return {"niveles": niveles, "spread_efectivo": spread_efectivo,
                "order_amount": order_amount, "capped_by_capital": capped,
                "source": "target"}
    # modo spread: niveles por spread, capado por capital (fiel al executor)
    step_brl = btc_brl_price * spread_per_subrange
    niveles_por_spread = math.floor(ancho_grilla / step_brl) if step_brl > 0 else 1
    niveles_por_capital = math.floor(capital_grilla / min_order) if min_order > 0 else niveles_por_spread
    niveles = max(1, min(niveles_por_spread, niveles_por_capital))
    spread_efectivo = (ancho_grilla / niveles) / btc_brl_price if niveles > 0 else 0.0
    order_amount = capital_grilla / niveles if niveles > 0 else capital_grilla
    capped = niveles_por_capital < niveles_por_spread
    return {"niveles": niveles, "spread_efectivo": spread_efectivo,
            "order_amount": order_amount, "capped_by_capital": capped,
            "source": "spread"}


def _build_comparison_table(
    A: float, B: float, min_grids: int, max_grids: int, selected_grid: int,
    btc_brl_price: float, nav_brl: float, spread_per_subrange: float,
    target_levels: Optional[int] = None, min_order: float = BINANCE_MIN_NOTIONAL_BRL,
    base_tablero: float = None, target_pct_btc: float = None, techo_pct: float = 1.0,
) -> tuple[list[dict], list[str]]:
    quote_tablero = (nav_brl - base_tablero * btc_brl_price) if base_tablero is not None else None
    # Recorrido (no depende de N): cuánto cargar/descargar entre techo y piso.
    rec = (_recorrido_inventario(base_tablero, quote_tablero, btc_brl_price,
                                 target_pct_btc, techo_pct)
           if base_tablero is not None and target_pct_btc is not None else None)
    rows = []
    for n in range(min_grids, max_grids + 1):
        ancho_grilla = (B - A) / n
        if rec is not None:
            # Capital por grilla = NAV·Δtarget_local (un salto por grilla, igual que
            # el controller). En régimen es PAREJO: quantum = NAV·(techo−piso)/n.
            # La 1ª grilla cruzada desde el ancla absorbe |pct_actual − su target|.
            quantum = nav_brl * (techo_pct - target_pct_btc) / n
            pct_actual = rec["pct_actual"]
            tl_aqui = _target_local_at(btc_brl_price, A, B, target_pct_btc, techo_pct)
            cap_ancla = nav_brl * abs(pct_actual - tl_aqui)
            capital_grilla = quantum  # perfil de niveles con el capital de régimen
            grid_total = rec["brl_descarga"] + rec["brl_carga"]
        else:
            capital_grilla = nav_brl / n / 2
            grid_total = capital_grilla * n * 2
        prof = _resolve_grid_profile(ancho_grilla, capital_grilla, btc_brl_price,
                                     spread_per_subrange, target_levels, min_order)
        niveles = prof["niveles"]
        order_amount = prof["order_amount"]
        min_ok = order_amount >= min_order
        marker = " ★" if n == selected_grid else ""
        niveles_str = f"{niveles}⚠" if prof["capped_by_capital"] else str(niveles)
        row = {
            "N grillas": f"{n}{marker}",
            "Niveles/grilla": niveles_str,
            "Spread ef.": f"{prof['spread_efectivo']:.3%}",
            "Order amt (BRL)": _compact(order_amount),
            "Min notional": "✅" if min_ok else "❌",
            "Total grillas": _compact(grid_total) if grid_total > 0 else "—",
        }
        if rec is not None:
            row["Cap./salto"] = _compact(quantum)
            row["Cap. 1ª (ancla)"] = _compact(cap_ancla)
        rows.append(row)
    cols = ["N grillas", "Niveles/grilla", "Spread ef.", "Order amt (BRL)",
            "Min notional", "Total grillas"]
    if rec is not None:
        cols += ["Cap./salto", "Cap. 1ª (ancla)"]
    return rows, cols


def _target_local_at(price: float, A: float, B: float,
                     piso: float, techo: float) -> float:
    """%BTC objetivo de la curva determinística en un precio (lineal techo en A ->
    piso en B). Mismo modelo que _target_local del controller."""
    if B <= A:
        return piso
    frac = max(0.0, min(1.0, (price - A) / (B - A)))
    return techo - (techo - piso) * frac


def _build_grids(A: float, B: float, n: int, btc_brl_price: float,
                 nav_brl: float, spread_per_subrange: float,
                 limit_distance_pct: float, target_levels: Optional[int] = None,
                 min_order: float = BINANCE_MIN_NOTIONAL_BRL,
                 cap_override: float = None,
                 target_pct_btc: float = None, techo_pct: float = 1.0,
                 pct_actual: float = None, current_price: float = None) -> list[dict]:
    """Construye los pares LONG+SHORT por escalón.

    Capital por grilla = NAV · Δtarget_local (UN salto por grilla, igual que el
    controller `_capital_for`): cada grilla mueve el %BTC exactamente hasta el
    target local de SU escalón. Como Δ%BTC = BRL/NAV (NAV invariante):
      - régimen: cap = NAV·(techo−piso)/n  (saltos PAREJOS, ambos lados)
      - la PRIMERA grilla que se cruza desde el ancla (%BTC actual) absorbe el gap:
        SHORT primera arriba del precio: NAV·(pct_actual − tl(i));
        LONG  primera abajo del precio:  NAV·(tl(i) − pct_actual).
    Fallback: cap_override o total/N/2 si no hay target/ancla.
    """
    ancho = (B - A) / n
    mids = [A + (i + 0.5) * ancho for i in range(n)]

    con_target = (target_pct_btc is not None and pct_actual is not None
                  and current_price is not None)
    quantum = nav_brl * (techo_pct - target_pct_btc) / n if con_target else None
    # La PRIMERA grilla de cada lado absorbe el gap del ancla. Por BANDA (no por com):
    # la del escalón ACTUAL ya está activa aunque su com haya quedado atrás del precio.
    idx_first_short = next((i for i in range(n)
                            if A + (i + 1) * ancho >= current_price), None) \
        if con_target else None
    idx_first_long = next((i for i in reversed(range(n))
                           if A + i * ancho <= current_price), None) \
        if con_target else None

    def _cap_for(side: str, i: int) -> float:
        if not con_target:
            return cap_override if cap_override is not None else nav_brl / n / 2
        tl = _target_local_at(mids[i], A, B, target_pct_btc, techo_pct)
        if side == "SHORT" and i == idx_first_short:
            return max(0.0, nav_brl * (pct_actual - tl))
        if side == "LONG" and i == idx_first_long:
            return max(0.0, nav_brl * (tl - pct_actual))
        return quantum

    grids = []
    for i in range(n):
        low = A + i * ancho
        high = low + ancho
        mid = (low + high) / 2
        for side in ("LONG", "SHORT"):
            capital_grilla = _cap_for(side, i)
            prof = _resolve_grid_profile(ancho, capital_grilla, btc_brl_price,
                                         spread_per_subrange, target_levels, min_order)
            limit_price = (low * (1 - limit_distance_pct) if side == "LONG"
                           else high * (1 + limit_distance_pct))
            grids.append({
                "idx": i, "low": low, "high": high, "mid": mid,
                "side": side, "limit_price": limit_price,
                "capital_brl": capital_grilla, "niveles": prof["niveles"],
                "order_amount": prof["order_amount"],
                "spread_efectivo": prof["spread_efectivo"],
            })
    return grids


def _inventory_curve(grids: list[dict], current_price: float,
                     base_tablero: float, nav_brl: float,
                     btc_brl_price: float, A: float, B: float,
                     target_pct_btc: float = None, techo_pct: float = 1.0,
                     n_points: int = 80) -> pd.DataFrame:
    """Curva de cómo se movería TU portfolio REAL según el precio.

    ANCLADA en el inventario actual (base_tablero, quote_inicial) en current_price.
    Para cada precio P se cuentan SOLO las grillas que el precio cruzaría yendo
    desde current_price hasta P (no desde A). Así:
      - P = current_price -> delta 0 -> %BTC = el real de hoy (coincide con el KPI).
      - P sube -> cruza SHORT (entre current y P) -> VENDE BTC -> %BTC baja (descarga).
      - P baja -> cruza LONG (entre P y current) -> COMPRA BTC -> %BTC sube (carga).

    TARGET LOCAL COMO SETPOINT (igual que el controller `_capital_for`): cada
    grilla cruzada mueve el %BTC EXACTAMENTE hasta el target local de SU escalón
    (vende/compra NAV·Δpct, ni más ni menos). Un salto por grilla, aterrizando en
    la curva determinística — sin concentrar el recorrido ni atravesar el target.
    """
    prices = np.linspace(A * 0.95, B * 1.02, n_points)
    quote_inicial = nav_brl - base_tablero * btc_brl_price

    def _target_local(com_price: float) -> float:
        # %BTC objetivo en el precio com (lineal: techo en A, piso en B).
        if target_pct_btc is None or B <= A:
            return None
        return _target_local_at(com_price, A, B, target_pct_btc, techo_pct)

    rows = []
    for P in prices:
        delta_btc = 0.0
        delta_brl = 0.0
        if P >= current_price:
            # Subiendo: cada SHORT cruzada vende hasta SU target. El trigger es el
            # com, EXCEPTO para la grilla del escalón ACTUAL (com ya pasado pero la
            # SHORT está activa y vende en lo que queda de su banda): dispara en el
            # punto medio de la zona restante (current..high). Sin esto, la grilla
            # del escalón del precio desaparecía de la proyección.
            def _trig_s(g):
                return max(g["mid"], (current_price + g["high"]) / 2)
            cruzadas = sorted(
                [g for g in grids if g["side"] == "SHORT"
                 and current_price <= _trig_s(g) <= P],
                key=_trig_s)  # orden de cruce al subir
            for g in cruzadas:
                com = _trig_s(g)  # precio al que ejecuta (proyección)
                tl = _target_local(g["mid"])  # target del ESCALÓN (= controller)
                btc_prov = base_tablero + delta_btc
                nav_prov = btc_prov * P + quote_inicial + delta_brl
                pct_prov = (btc_prov * P / nav_prov) if nav_prov > 0 else 0
                if tl is None:
                    x_brl = g["capital_brl"]  # sin target: vende su capital completo
                elif pct_prov <= tl:
                    continue  # ya en/bajo el target local de este escalón
                else:
                    x_brl = nav_prov * (pct_prov - tl)  # vender JUSTO hasta tl
                delta_btc -= x_brl / com
                delta_brl += x_brl
        else:
            # Bajando: espejo — la LONG del escalón actual dispara en el punto medio
            # de su zona restante (low..current); las demás en su com.
            def _trig_l(g):
                return min(g["mid"], (current_price + g["low"]) / 2)
            cruzadas = sorted(
                [g for g in grids if g["side"] == "LONG"
                 and P <= _trig_l(g) <= current_price],
                key=_trig_l, reverse=True)  # orden de cruce al bajar
            for g in cruzadas:
                com = _trig_l(g)  # precio al que ejecuta (proyección)
                tl = _target_local(g["mid"])  # target del ESCALÓN (= controller)
                btc_prov = base_tablero + delta_btc
                nav_prov = btc_prov * P + quote_inicial + delta_brl
                pct_prov = (btc_prov * P / nav_prov) if nav_prov > 0 else 0
                if tl is None:
                    x_brl = g["capital_brl"]  # sin target: compra su capital completo
                elif pct_prov >= tl:
                    continue  # ya en/sobre el target local de este escalón
                else:
                    x_brl = nav_prov * (tl - pct_prov)  # comprar JUSTO hasta tl
                delta_btc += x_brl / com
                delta_brl -= x_brl
        btc_at_P = base_tablero + delta_btc
        nav_brl_at_P = btc_at_P * P + quote_inicial + delta_brl
        pct_btc = (btc_at_P * P / nav_brl_at_P) if nav_brl_at_P > 0 else 0
        # Benchmark HOLD: inventario actual congelado desde hoy, valorizado a P.
        nav_hold_at_P = base_tablero * P + quote_inicial
        rows.append({
            "price": P,
            "btc": max(btc_at_P, 0),
            "pct_btc": max(0.0, min(1.0, pct_btc)),
            "nav_brl": nav_brl_at_P,
            "nav_hold_brl": nav_hold_at_P,
        })
    return pd.DataFrame(rows)


def _build_candle_chart(df: pd.DataFrame, grids: list[dict],
                        supports: list[dict], resistances: list[dict],
                        current_price: float, A: float, B: float,
                        trading_pair: str, selected_grid: int):
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        row_heights=[0.78, 0.22], vertical_spacing=0.03)

    fig.add_trace(go.Candlestick(
        x=df["timestamp"],
        open=df["open"], high=df["high"], low=df["low"], close=df["close"],
        increasing_line_color="#22c55e", decreasing_line_color="#ef4444",
        name="OHLC", showlegend=False,
    ), row=1, col=1)

    colors = ["#22c55e" if c >= o else "#ef4444" for c, o in zip(df["close"], df["open"])]
    fig.add_trace(go.Bar(
        x=df["timestamp"], y=df["volume"],
        marker_color=colors, opacity=0.5, name="Volumen", showlegend=False,
    ), row=2, col=1)

    x_min = df["timestamp"].iloc[0]
    x_max = df["timestamp"].iloc[-1]

    for s in supports:
        fig.add_shape(type="line", x0=x_min, x1=x_max, y0=s["price"], y1=s["price"],
                      line=dict(color="#22c55e", width=2, dash="solid"), row=1, col=1)
        fig.add_annotation(x=x_max, y=s["price"], text=f"  S {_brl(s['price'])}",
                           showarrow=False, xanchor="left", font=dict(color="#22c55e", size=9), row=1, col=1)
    for r in resistances:
        fig.add_shape(type="line", x0=x_min, x1=x_max, y0=r["price"], y1=r["price"],
                      line=dict(color="#ef4444", width=2, dash="solid"), row=1, col=1)
        fig.add_annotation(x=x_max, y=r["price"], text=f"  R {_brl(r['price'])}",
                           showarrow=False, xanchor="left", font=dict(color="#ef4444", size=9), row=1, col=1)

    for lvl, label, color in [(A, "A", "#fde68a"), (B, "B", "#fde68a")]:
        fig.add_shape(type="line", x0=x_min, x1=x_max, y0=lvl, y1=lvl,
                      line=dict(color=color, width=2.5, dash="solid"), row=1, col=1)
        fig.add_annotation(x=x_min, y=lvl, text=f"{label} {_brl(lvl)}",
                           showarrow=False, xanchor="right",
                           font=dict(color=color, size=10, family="monospace"), row=1, col=1)

    for g in grids:
        color = "#22c55e" if g["side"] == "LONG" else "#ef4444"
        for lvl in [g["low"], g["high"]]:
            fig.add_shape(type="line", x0=x_min, x1=x_max, y0=lvl, y1=lvl,
                          line=dict(color=color, width=0.8, dash="dot"), row=1, col=1)
        ancho = g["high"] - g["low"]
        for k in range(1, g["niveles"]):
            sub_lvl = g["low"] + k * (ancho / g["niveles"])
            fig.add_shape(type="line", x0=x_min, x1=x_max, y0=sub_lvl, y1=sub_lvl,
                          line=dict(color=color, width=0.4, dash="dot"), opacity=0.5, row=1, col=1)

    fig.add_hline(y=current_price, line_color="#ffffff", line_width=1.5, line_dash="dash",
                  annotation_text=f"  HOY {_brl(current_price)}", annotation_position="right", row=1, col=1)

    fig.update_layout(
        title=f"Candles + Niveles — {trading_pair} (selected={selected_grid} grillas)",
        template="plotly_dark", paper_bgcolor="#0e1117", plot_bgcolor="#161b22",
        height=600, xaxis_rangeslider_visible=False,
        margin=dict(l=60, r=100, t=50, b=40), showlegend=False,
    )
    fig.update_yaxes(gridcolor="#21262d", tickformat=",.0f", row=1, col=1)
    fig.update_yaxes(gridcolor="#21262d", row=2, col=1)
    return fig


def _build_inv_nav_chart(
    inv_df: pd.DataFrame, current_price: float,
    target_pct_btc: float, A: float, B: float,
    usdt_brl: float,
    supports: list[dict], resistances: list[dict],
    trading_pair: str,
    pct_btc_actual: float = None,
):
    """3 rows: inventario (%BTC) + NAV estrategia-vs-hold (BRL+USDT) + volumen BRL
    equivalente para cubrir la brecha vs hold (delta / rebate)."""
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    fig = make_subplots(
        rows=3, cols=1,
        shared_xaxes=True,
        row_heights=[0.44, 0.34, 0.22],
        vertical_spacing=0.05,
        specs=[[{"secondary_y": False}], [{"secondary_y": True}], [{"secondary_y": True}]],
        subplot_titles=[
            "Curva de Inventario (%BTC)",
            "NAV Teórico: Estrategia vs HOLD (BRL + USDT)",
            "Volumen BRL necesario para cubrir la brecha vs hold (vía rebates 0.015%)",
        ],
    )

    # ── Row 1: curva de inventario ───────────────────────────────────────────
    fig.add_trace(go.Scatter(
        x=inv_df["price"], y=inv_df["pct_btc"] * 100,
        mode="lines", name="%BTC(P)",
        line=dict(color="#58a6ff", width=2.5),
        fill="tozeroy", fillcolor="rgba(88,166,255,0.08)",
        hovertemplate="P=%{x:,.0f} BRL<br>%BTC=%{y:.1f}%<extra></extra>",
    ), row=1, col=1)

    fig.add_hline(y=target_pct_btc * 100, line_dash="dash", line_color="#f85149",
                  line_width=1.5, annotation_text=f"Target {target_pct_btc:.0%}",
                  annotation_position="right", row=1, col=1)

    target_rows = inv_df[abs(inv_df["pct_btc"] - target_pct_btc) == abs(inv_df["pct_btc"] - target_pct_btc).min()]
    if not target_rows.empty:
        p_target = float(target_rows["price"].iloc[0])
        fig.add_vline(x=p_target, line_dash="dot", line_color="#f85149",
                      annotation_text=f"{_brl(p_target)}", annotation_position="top", row=1, col=1)

    full_rows = inv_df[inv_df["pct_btc"] >= 0.95]
    if not full_rows.empty:
        p_full = float(full_rows["price"].iloc[0])
        fig.add_vline(x=p_full, line_dash="dot", line_color="#fde68a",
                      annotation_text=f"100% BTC {_brl(p_full)}", annotation_position="top left", row=1, col=1)

    # Línea HOY con el %BTC REAL en la anotación (= KPI "%BTC inicial", no la curva).
    _here_pct = (float(pct_btc_actual) * 100 if pct_btc_actual is not None
                 else float(inv_df.iloc[(inv_df["price"] - current_price).abs().argmin()]["pct_btc"]) * 100)
    fig.add_vline(x=current_price, line_color="#ffffff", line_width=1.5, line_dash="dash",
                  annotation_text=f"HOY {_brl(current_price)} · {_here_pct:.1f}%",
                  annotation_position="top right", row=1, col=1)

    # S/R como vlines en ambos rows (shared_xaxes propaga automáticamente)
    p_min, p_max = inv_df["price"].min(), inv_df["price"].max()
    for s in supports:
        if p_min <= s["price"] <= p_max:
            fig.add_vline(x=s["price"], line_color="#22c55e", line_width=1, line_dash="dot",
                          annotation_text=f"S {_brl(s['price'])}", annotation_position="bottom right",
                          annotation_font=dict(color="#22c55e", size=9))
    for r in resistances:
        if p_min <= r["price"] <= p_max:
            fig.add_vline(x=r["price"], line_color="#ef4444", line_width=1, line_dash="dot",
                          annotation_text=f"R {_brl(r['price'])}", annotation_position="top right",
                          annotation_font=dict(color="#ef4444", size=9))

    fig.update_xaxes(gridcolor="#21262d", showticklabels=False, row=1, col=1)
    fig.update_yaxes(title_text="%BTC del capital", ticksuffix="%", range=[0, 105],
                     gridcolor="#21262d", row=1, col=1)

    # ── Row 2: NAV teórico (estrategia vs HOLD) BRL + USDT vs precio ─────────
    # El fill va ENTRE la curva de estrategia y la de hold (benchmark de holdear
    # el inventario inicial quieto). El área = delta estrategia-vs-hold.
    # Volumen equivalente en BRL para cubrir ese delta vía rebates = delta / 0.00015.
    REBATE = 0.00015
    nav_brl_curve = inv_df["nav_brl"]
    nav_hold_brl_curve = inv_df["nav_hold_brl"]
    nav_usdt_curve = nav_brl_curve / usdt_brl if usdt_brl else nav_brl_curve
    nav_hold_usdt_curve = nav_hold_brl_curve / usdt_brl if usdt_brl else nav_hold_brl_curve

    delta_brl = (nav_brl_curve - nav_hold_brl_curve).to_numpy()
    vol_equiv = delta_brl / REBATE  # volumen BRL equivalente para cubrir el gap por rebates

    # --- BRL (eje izquierdo) ---
    # 1) Línea HOLD: gris tenue y fina, claramente "de fondo".
    fig.add_trace(go.Scatter(
        x=inv_df["price"], y=nav_hold_brl_curve,
        name="HOLD (benchmark)",
        line=dict(color="#8b949e", width=1, dash="dash"),
        opacity=0.6,
        hovertemplate="P=%{x:,.0f}<br>HOLD BRL: R$ %{y:,.0f}<extra></extra>",
    ), row=2, col=1, secondary_y=False)
    # 2) Estrategia: verde sólido grueso, con fill hacia el hold (la traza anterior).
    fig.add_trace(go.Scatter(
        x=inv_df["price"], y=nav_brl_curve,
        name="NAV BRL (estrategia)",
        line=dict(color="#3fb950", width=3, shape="spline", smoothing=1.2),
        fill="tonexty", fillcolor="rgba(63,185,80,0.18)",
        customdata=np.column_stack([delta_brl, vol_equiv]),
        hovertemplate=("P=%{x:,.0f} BRL<br><b>NAV BRL</b>: R$ %{y:,.0f}"
                       "<br>Δ vs hold: R$ %{customdata[0]:,.0f}"
                       "<br>Vol. equiv. p/ cubrir: R$ %{customdata[1]:,.0f}<extra></extra>"),
    ), row=2, col=1, secondary_y=False)

    # --- USDT (eje derecho) ---
    # Estrategia en azul sólido. Hold USDT en azul tenue punteado (sin fill para
    # no ensuciar; el área que importa es la de BRL).
    fig.add_trace(go.Scatter(
        x=inv_df["price"], y=nav_hold_usdt_curve,
        name="HOLD USDT",
        line=dict(color="#58a6ff", width=1, dash="dot"),
        opacity=0.4, showlegend=False,
        hovertemplate="P=%{x:,.0f}<br>HOLD USDT: $%{y:,.0f}<extra></extra>",
    ), row=2, col=1, secondary_y=True)
    fig.add_trace(go.Scatter(
        x=inv_df["price"], y=nav_usdt_curve,
        name="NAV USDT (estrategia)",
        line=dict(color="#58a6ff", width=2.5, shape="spline", smoothing=1.2),
        hovertemplate="P=%{x:,.0f} BRL<br><b>NAV USDT</b>: $%{y:,.0f}<extra></extra>",
    ), row=2, col=1, secondary_y=True)

    # ── Row 3: volumen equivalente para cubrir la brecha (barplot) ───────────
    # |delta_brl| / rebate = volumen BRL que habría que transar para que los
    # rebates cubran la brecha contra el hold. Se muestra en MAGNITUD (siempre
    # positivo, hacia arriba): es "cuánto volumen necesito". La barra es única
    # (en BRL); el eje derecho la re-escala a USDT (mismo dato).
    vol_abs = np.abs(vol_equiv)
    vol_abs_usdt = vol_abs / usdt_brl if usdt_brl else vol_abs
    fig.add_trace(go.Bar(
        x=inv_df["price"], y=vol_abs,
        name="Vol. equivalente",
        marker_color="#58a6ff", opacity=0.75, showlegend=False,
        customdata=np.asarray(vol_abs_usdt).reshape(-1, 1),
        hovertemplate=("P=%{x:,.0f} BRL<br>Vol. p/ cubrir brecha: R$ %{y:,.0f}"
                       "<br>= $ %{customdata[0]:,.0f} USDT<extra></extra>"),
    ), row=3, col=1, secondary_y=False)
    # Traza fantasma en el eje secundario para fijar su escala = BRL / usdt_brl.
    fig.add_trace(go.Scatter(
        x=inv_df["price"], y=vol_abs_usdt,
        mode="markers", marker=dict(opacity=0), showlegend=False,
        hoverinfo="skip",
    ), row=3, col=1, secondary_y=True)

    # Eje X (precio) va en la row de abajo (row 3); row 2 sin labels por shared_xaxes.
    fig.update_xaxes(showticklabels=False, gridcolor="#21262d", row=2, col=1)
    fig.update_yaxes(
        title_text="NAV (BRL)", tickformat=",.0f", tickprefix="R$ ",
        gridcolor="#21262d",
        title_font=dict(color="#3fb950"), tickfont=dict(color="#3fb950"),
        row=2, col=1, secondary_y=False,
    )
    fig.update_yaxes(
        title_text="NAV (USDT)", tickformat=",.0f", tickprefix="$ ",
        gridcolor="rgba(0,0,0,0)",
        title_font=dict(color="#58a6ff"), tickfont=dict(color="#58a6ff"),
        row=2, col=1, secondary_y=True,
    )
    fig.update_xaxes(title_text="Precio BTC-BRL", tickformat=",.0f", gridcolor="#21262d", row=3, col=1)
    # Magnitud (siempre positiva). Ambos ejes 0→vmax, escalas proporcionales.
    vmax = float(vol_abs.max()) * 1.1 if len(vol_abs) else 1.0
    fig.update_yaxes(
        title_text="Vol. (BRL)", tickformat=",.2s", tickprefix="R$ ",
        gridcolor="#21262d", range=[0, vmax],
        title_font=dict(color="#8b949e"), tickfont=dict(color="#8b949e"),
        row=3, col=1, secondary_y=False,
    )
    fig.update_yaxes(
        title_text="Vol. (USDT)", tickformat=",.2s", tickprefix="$ ",
        gridcolor="rgba(0,0,0,0)",
        title_font=dict(color="#58a6ff"), tickfont=dict(color="#58a6ff"),
        range=[0, vmax / usdt_brl] if usdt_brl else [0, vmax],
        row=3, col=1, secondary_y=True,
    )

    fig.update_layout(
        template="plotly_dark", paper_bgcolor="#0e1117", plot_bgcolor="#161b22",
        height=920,
        legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="center", x=0.5,
                    bgcolor="rgba(0,0,0,0)", font=dict(size=11)),
        margin=dict(l=80, r=80, t=60, b=50),
        hovermode="x unified",
    )
    return fig


async def run(config: Config, context: ContextTypes.DEFAULT_TYPE) -> RoutineResult:
    chat_id = context._chat_id if hasattr(context, "_chat_id") else None
    client = await get_client(chat_id, context=context)
    if not client:
        return RoutineResult(text="No server available")

    selected_grid = max(config.min_grids, min(config.max_grids, config.selected_grid))

    # ── 1. Precios ──────────────────────────────────────────────────────────
    # Par genérico: el quote sale del trading_pair (BTC-BRL -> BRL, BTC-USDT -> USDT).
    _base_asset, _quote_asset = config.trading_pair.upper().split("-")
    # btc_brl_price = PRECIO DEL PAR (base/quote del config), no necesariamente BRL.
    # Se mantiene el nombre por compatibilidad con el resto del archivo; conceptualmente
    # es "precio del par" y el NAV queda denominado en el quote del par.
    btc_brl_price = btc_usdt_price = usdt_brl = None
    try:
        # pedimos el par real + los auxiliares para convertir el NAV a USDT.
        # OJO: el ticker de conversión es USDT-{quote} (ej USDT-BRL); el invertido
        # ({quote}-USDT) no existe y un par inválido tumba el batch ENTERO con 500
        # (el precio caía al fallback de velas diarias: lento y stale).
        _pairs = {config.trading_pair.upper(), "BTC-USDT"}
        if _quote_asset != "USDT":
            _pairs.add(f"USDT-{_quote_asset}")
        _pairs = list(_pairs)
        prices_resp = await client.market_data.get_prices(config.connector_name, _pairs)
        # La respuesta es un dict envolvente {connector, prices, timestamp}.
        # Los precios reales están en la clave "prices". Si no, usar el dict tal cual.
        prices = prices_resp.get("prices", prices_resp) if isinstance(prices_resp, dict) else {}

        # Lookup robusto: tolera distintos formatos de clave del par.
        def _price_of(d: dict, *keys) -> float | None:
            for k in keys:
                if k in d and d[k]:
                    return float(d[k])
            norm = {str(kk).upper().replace("-", "").replace("/", "").replace("_", ""): vv
                    for kk, vv in d.items()}
            for k in keys:
                nk = k.upper().replace("-", "").replace("/", "").replace("_", "")
                if nk in norm and norm[nk]:
                    return float(norm[nk])
            return None
        # precio del PAR real (ej BTC-BRL o BTC-USDT)
        btc_brl_price = _price_of(prices, config.trading_pair.upper(),
                                  config.trading_pair.upper().replace("-", ""))
        btc_usdt_price = _price_of(prices, "BTC-USDT", "BTCUSDT", "BTC/USDT")
        # quote->USDT: si el quote ES USDT, 1; si no, USDT-quote (ej USDT-BRL).
        if _quote_asset == "USDT":
            usdt_brl = 1.0
        else:
            usdt_brl = _price_of(prices, f"USDT-{_quote_asset}", f"USDT{_quote_asset}")
    except Exception as e:
        logger.warning(f"get_prices falló: {e}")

    if not btc_brl_price:
        try:
            candles = await client.market_data.get_candles_last_days(
                config.connector_name, config.trading_pair, days=2, interval="1d"
            )
            recs = (
                candles if isinstance(candles, list)
                else candles.get("data", candles.get("candles", [])) if isinstance(candles, dict)
                else []
            )
            if recs:
                df_c = pd.DataFrame(recs)
                df_c.columns = [c.lower() for c in df_c.columns]
                btc_brl_price = float(pd.to_numeric(df_c["close"], errors="coerce").dropna().iloc[-1])
        except Exception as e:
            logger.warning(f"Fallback precio falló: {e}")

    if not btc_brl_price:
        return RoutineResult(text=f"No se pudo obtener precio {config.trading_pair}.")

    # usdt_brl = cuántas unidades de QUOTE vale 1 USDT (factor quote->USDT).
    # Si el quote es USDT, es 1. Si no, se DERIVA de precio_par / BTC-USDT (tipo de
    # cambio implícito, coherente con el resto del portfolio; el directo puede estar
    # stale). Generaliza el viejo "usdt_brl" a cualquier quote.
    usdt_brl_directo = usdt_brl  # guardado solo para diagnóstico
    if _quote_asset == "USDT":
        usdt_brl = 1.0
    elif btc_usdt_price:
        usdt_brl = btc_brl_price / btc_usdt_price   # precio_par(quote) / precio_usdt = quote por USDT
    elif not usdt_brl:
        usdt_brl = 5.8  # último recurso (solo aplicaba al caso BRL)
    price_coherent = True
    if usdt_brl_directo and usdt_brl:
        price_coherent = abs(usdt_brl_directo - usdt_brl) / usdt_brl <= 0.01

    # ── 2. Balance real ─────────────────────────────────────────────────────
    # base/quote assets salen del trading_pair (BTC-BRL -> BTC/BRL, BTC-USDT -> BTC/USDT).
    base_asset, quote_asset = config.trading_pair.upper().split("-")
    base_qty = quote_qty = usdt_qty = 0.0
    token_values_usd: dict[str, float] = {}  # value (USD) por token, del endpoint
    balance_ok = False
    try:
        state = await client.portfolio.get_state(refresh=False)
        token_totals: dict[str, float] = {}
        for account_data in state.values():
            for connector_balances in account_data.values():
                if not isinstance(connector_balances, list):
                    continue
                for b in connector_balances:
                    token = b.get("token", "").upper()
                    units = float(b.get("units", 0) or 0)
                    token_totals[token] = token_totals.get(token, 0.0) + units
                    token_values_usd[token] = (token_values_usd.get(token, 0.0)
                                               + float(b.get("value", 0) or 0))
        base_qty = token_totals.get(base_asset, 0.0)   # BTC global
        quote_qty = token_totals.get(quote_asset, 0.0)  # quote global (BRL/USDT/...)
        usdt_qty = token_totals.get("USDT", 0.0)
        balance_ok = True
    except Exception as e:
        logger.warning(f"get_state falló: {e}")

    # ── 3. Inventario del tablero — NAV asignado + %BTC inicial (COMPONIBLE) ──
    # Asignás un NAV total (en quote) y su composición (%BTC inicial). De ahí se
    # derivan base (BTC) y quote (líquido). El tablero opera SOLO sobre ese NAV
    # (aislado; no ve el resto del portfolio).
    warnings = []
    # %BTC global de la cuenta (referencia; default del %BTC inicial si no se indica).
    # Preferir el value (USD) del endpoint, que cubre TODOS los tokens de la cuenta
    # — sumar solo BTC+USDT+quote excluía el resto (BNB/ETH/stables) e INFLABA el
    # %BTC (ej: marcaba 98% cuando el portfolio real era 93%). Fallback al cálculo
    # por unidades si el endpoint no trae values.
    total_value_usd = sum(token_values_usd.values()) if balance_ok else 0.0
    base_value_usd = token_values_usd.get(base_asset, 0.0) if balance_ok else 0.0
    if total_value_usd > 0:
        nav_portfolio_brl = total_value_usd * usdt_brl
        pct_btc_portfolio = base_value_usd / total_value_usd
    else:
        nav_portfolio_brl = base_qty * btc_brl_price + usdt_qty * usdt_brl + quote_qty
        pct_btc_portfolio = (base_qty * btc_brl_price / nav_portfolio_brl) if nav_portfolio_brl > 0 else 0

    if not balance_ok and config.nav_assigned is None:
        return RoutineResult(
            text="⚠️ No se pudo leer balance — asigná nav_assigned para continuar."
        )

    # NAV del tablero (en quote): el asignado, o TODO el NAV global de la cuenta.
    nav_brl = config.nav_assigned if config.nav_assigned is not None else nav_portfolio_brl

    # %BTC inicial: el indicado, o el %BTC global de la cuenta.
    pct_btc_inicial = (config.pct_btc_inicial if config.pct_btc_inicial is not None
                       else pct_btc_portfolio)
    pct_btc_inicial = max(0.0, min(1.0, pct_btc_inicial))

    # Derivación base/quote desde NAV + %BTC inicial.
    base_tablero = (nav_brl * pct_btc_inicial) / btc_brl_price if btc_brl_price > 0 else 0.0
    quote_tablero = nav_brl * (1.0 - pct_btc_inicial)

    nav_usdt = nav_brl / usdt_brl if usdt_brl else nav_brl
    if balance_ok and config.nav_assigned is not None and nav_brl > nav_portfolio_brl > 0:
        warnings.append(f"⚠️ nav_assigned ({nav_brl:.0f} {quote_asset}) > NAV global de la cuenta ({nav_portfolio_brl:.0f})")

    if not price_coherent:
        warnings.append(
            f"ℹ️ USDT-BRL directo ({usdt_brl_directo:.3f}) difería del implícito "
            f"BTC-BRL/BTC-USDT ({usdt_brl:.3f}). Se usó el implícito para el NAV USDT."
        )

    # ── 4. S/R y rango A-B ──────────────────────────────────────────────────
    supports, resistances, candle_df = await _fetch_sr_and_candles(
        client, config.connector_name, config.trading_pair,
        config.sr_lookback_days, config.sr_levels_per_side, config.candles_interval
    )

    A = config.border_a
    B = config.border_b
    if A is None and supports:
        idx_a = max(0, min(config.border_a_sr_idx - 1, len(supports) - 1))
        A = supports[idx_a]["price"]
    if B is None and resistances:
        idx_b = max(0, min(config.border_b_sr_idx - 1, len(resistances) - 1))
        B = resistances[idx_b]["price"]
    if A is None:
        A = btc_brl_price * 0.90
    if B is None:
        B = btc_brl_price * 1.10

    if not (A < btc_brl_price < B):
        warnings.append(f"⚠️ Rango incoherente A={_brl(A)} / precio={_brl(btc_brl_price)} / B={_brl(B)}. Ajustando.")
        A = min(A, btc_brl_price * 0.95)
        B = max(B, btc_brl_price * 1.05)

    # ── 5. Tabla comparativa ─────────────────────────────────────────────────
    comp_rows, comp_cols = _build_comparison_table(
        A, B, config.min_grids, config.max_grids, selected_grid,
        btc_brl_price, nav_brl, config.spread_per_subrange,
        target_levels=config.target_levels_per_grid,
        min_order=config.min_order_amount_quote,
        base_tablero=base_tablero, target_pct_btc=config.target_pct_btc,
        techo_pct=config.techo_pct_btc,
    )

    # ── 6. Análisis profundo — RECORRIDO ENTRE LOS DOS EXTREMOS ──────────────
    # La curva recorre %BTC de TECHO (en A, carga máx) a PISO (en B, descarga máx).
    # Capital por grilla = NAV·Δtarget_local (UN salto por grilla, igual que el
    # controller): en régimen quantum = NAV·(techo−piso)/N parejo; la 1ª grilla
    # cruzada desde el ancla absorbe |%BTC actual − su target local|.
    quote_tablero = nav_brl - base_tablero * btc_brl_price
    rec = _recorrido_inventario(base_tablero, quote_tablero, btc_brl_price,
                                config.target_pct_btc, config.techo_pct_btc)
    # total que entra en grillas = descarga (SHORT) + carga (LONG).
    grid_total_quote = rec["brl_descarga"] + rec["brl_carga"]

    grids = _build_grids(A, B, selected_grid, btc_brl_price, nav_brl,
                         config.spread_per_subrange, config.limit_distance_pct,
                         target_levels=config.target_levels_per_grid,
                         min_order=config.min_order_amount_quote,
                         target_pct_btc=config.target_pct_btc,
                         techo_pct=config.techo_pct_btc,
                         pct_actual=pct_btc_inicial, current_price=btc_brl_price)
    ancho_sel = (B - A) / selected_grid
    # perfil del selected_grid: capital de régimen (quantum = NAV·(techo−piso)/N).
    cap_sel = nav_brl * (config.techo_pct_btc - config.target_pct_btc) / selected_grid
    prof_sel = _resolve_grid_profile(ancho_sel, cap_sel if cap_sel > 0 else nav_brl / selected_grid / 2,
                                     btc_brl_price, config.spread_per_subrange,
                                     config.target_levels_per_grid,
                                     config.min_order_amount_quote)
    if config.techo_pct_btc < pct_btc_inicial:
        warnings.append(
            f"⚠️ techo ({config.techo_pct_btc:.0%}) < %BTC actual ({pct_btc_inicial:.1%}): "
            f"las LONG no cargarían (ya estás por encima del techo)."
        )
    if grid_total_quote > nav_brl:
        warnings.append(
            f"⚠️ El recorrido {config.techo_pct_btc:.0%}→{config.target_pct_btc:.0%} pide "
            f"{_compact(grid_total_quote)} pero el NAV es {_compact(nav_brl)}: no alcanza el capital."
        )

    inv_df = _inventory_curve(grids, btc_brl_price, base_tablero,
                               nav_brl, btc_brl_price, A, B,
                               target_pct_btc=config.target_pct_btc,
                               techo_pct=config.techo_pct_btc, n_points=80)

    # ── 7. Gráficos ───────────────────────────────────────────────────────────
    fig_candles = None
    if candle_df is not None and len(candle_df) > 0:
        fig_candles = _build_candle_chart(
            candle_df, grids, supports, resistances,
            btc_brl_price, A, B, config.trading_pair, selected_grid
        )

    fig_inv_nav = _build_inv_nav_chart(
        inv_df, btc_brl_price, config.target_pct_btc, A, B,
        usdt_brl, supports, resistances, config.trading_pair,
        pct_btc_actual=pct_btc_inicial,
    )

    # ── 8. Texto resumen ─────────────────────────────────────────────────────
    _amplitud = ((B - A) / A) if A > 0 else 0
    _dist_a = (btc_brl_price - A) / (B - A) if (B - A) > 0 else 0
    _dist_b = (B - btc_brl_price) / (B - A) if (B - A) > 0 else 0
    _dentro = "✅ dentro" if A < btc_brl_price < B else "⚠️ FUERA"
    _warn_txt = "\n".join(warnings) if warnings else "ninguno"
    tg_text = (
        f"🔬 *Grigado — Chessboard Lab*\n"
        f"Rango: {_brl(A)} → {_brl(B)} ({_amplitud:.1%}) · {selected_grid} grillas\n"
        f"Precio: {_brl(btc_brl_price)} {_dentro} · A {_dist_a:.0%} / B {_dist_b:.0%}\n"
        f"%BTC actual: {pct_btc_inicial:.1%} · Cap/salto: {_compact(cap_sel)}\n"
        f"NAV asignado: {_compact(nav_brl)}\n"
        f"Warnings: {_warn_txt}"
    )

    # ── 9. ReportBuilder ─────────────────────────────────────────────────────
    try:
        from condor.reports import ReportBuilder

        builder = ReportBuilder(f"Lab Tablero: {config.trading_pair}")
        builder.source("routine", "chessboard_lab").tags(["chessboard", "tablero", config.trading_pair])
        builder.manual_order()

        # 1) NAV asignado: quote arriba, base abajo + % del portfolio global.
        _pct_del_portfolio = (nav_brl / nav_portfolio_brl) if nav_portfolio_brl > 0 else 0
        builder.kpi("NAV asignado", _compact(nav_brl),
                    delta=f"{_btc(base_tablero)} {base_asset} · {_pct_del_portfolio:.0%} del portfolio",
                    trend="neutral")
        # 2) Base Asset Range: %BTC actual + desde→hasta que se tradea (techo→piso).
        builder.kpi(f"{base_asset} Range", f"{pct_btc_inicial:.1%}",
                    delta=f"tradea {config.techo_pct_btc:.0%} (A) → {config.target_pct_btc:.0%} (B)",
                    trend="neutral")
        # 3) Rango A-B + amplitud porcentual.
        a_label = f"S#{config.border_a_sr_idx}" if config.border_a is None else "manual"
        b_label = f"R#{config.border_b_sr_idx}" if config.border_b is None else "manual"
        _amplitud = ((B - A) / A) if A > 0 else 0
        builder.kpi("Rango A-B", f"{_brl(A)} / {_brl(B)}",
                    delta=f"{a_label} → {b_label} · amplitud {_amplitud:.2%}", trend="neutral")
        # 4) Precio actual.
        builder.kpi(f"{base_asset}-{quote_asset}", _brl(btc_brl_price))

        if warnings:
            builder.markdown("**Advertencias:**\n" + "\n".join(f"- {w}" for w in warnings))

        builder.markdown("## Tabla comparativa (★ = selected_grid)")
        builder.table(comp_rows, comp_cols)

        # Bloque copiable con la config resuelta. Dos vistas:
        #  1) Resumen legible (KPIs informativos).
        #  2) YAML EXACTO del controller chessboard: nombres de campo idénticos a
        #     ChessboardConfig, pegable directo a conf/controllers/<id>.yml sin
        #     traducir nada (evita el error de mapear spread_per_subrange ->
        #     min_spread_between_orders, min_order_amount -> min_order_amount_quote).
        cap_grilla = cap_sel  # capital ideal por grilla (dimensionado al target)
        config_id = f"chessboard-{config.trading_pair.lower().replace('-', '-')}-1"
        # Spread que va al YAML: el DESPEJADO si se usó target_levels, sino el crudo.
        _yaml_spread = (prof_sel["spread_efectivo"] if prof_sel["source"] == "target"
                        else config.spread_per_subrange)
        _modo = (f"{prof_sel['niveles']} niveles objetivo → spread {_yaml_spread:.3%}"
                 if prof_sel["source"] == "target"
                 else f"spread {config.spread_per_subrange:.3%} → {prof_sel['niveles']} niveles")
        # YAML 1:1 con ChessboardConfig (controllers/generic/chessboard.py).
        # take_profit_order_type / open_order_type = 3 == OrderType.LIMIT_MAKER.
        yaml_block = (
            "## Config resuelta — YAML del controller (pegar directo)\n\n"
            f"_Guardar como_ `conf/controllers/{config_id}.yml`\n\n"
            "```yaml\n"
            f"# recorrido %BTC {config.techo_pct_btc:.0%}(A) -> {config.target_pct_btc:.0%}(B) | "
            f"actual {pct_btc_inicial:.1%} | perfil: {_modo} | "
            f"cap/salto R$ {cap_sel:.0f} (NAV·(techo-piso)/N) | "
            f"hold {_compact(nav_brl - grid_total_quote)}\n"
            f"# El controller dimensiona cada grilla EN VIVO: cap = NAV·|%BTC - target_local|\n"
            f"# (un salto por grilla hasta su target local; la 1ª absorbe el gap del ancla).\n"
            f"# total_amount_quote es informativo; lo que manda es el target local.\n"
            f"id: {config_id}\n"
            "controller_name: chessboard\n"
            "controller_type: generic\n"
            f"total_amount_quote: '{nav_brl:.0f}'\n"
            "manual_kill_switch: false\n"
            "initial_positions: []\n"
            f"connector_name: {config.connector_name}\n"
            f"trading_pair: {config.trading_pair}\n"
            "leverage: 1\n"
            f"border_a: '{A:.0f}'\n"
            f"border_b: '{B:.0f}'\n"
            f"n_grids: {selected_grid}\n"
            f"target_pct_btc: '{config.target_pct_btc:.2f}'\n"
            f"techo_pct_btc: '{config.techo_pct_btc:.2f}'\n"
            f"hysteresis_pct: '{config.hysteresis_pct}'\n"
            f"target_tolerance_pct: '{config.target_tolerance_pct}'\n"
            f"base_assigned: '{base_tablero:.8f}'\n"
            f"quote_assigned: '{quote_tablero:.2f}'\n"
            f"limit_distance_pct: '{config.limit_distance_pct}'\n"
            # Si se usó target_levels, va el spread DESPEJADO que produce esos niveles;
            # si no, el spread_per_subrange tal cual.
            f"min_spread_between_orders: '{_yaml_spread:.6f}'\n"
            f"min_order_amount_quote: '{config.min_order_amount_quote:.0f}'\n"
            f"max_open_orders: {config.max_open_orders}\n"
            f"max_orders_per_batch: {config.max_orders_per_batch}\n"
            f"order_frequency: {config.order_frequency}\n"
            "activation_bounds: null\n"
            "keep_position: true\n"
            "triple_barrier_config:\n"
            f"  take_profit: '{config.take_profit_per_level}'\n"
            "  open_order_type: 3\n"
            "  take_profit_order_type: 3\n"
            "```\n"
        )
        # Extracto conceptual: qué muestra cada gráfico (teórico) y de dónde sale
        # su espejo REAL en el controller — lo que hay que relevar en vivo para
        # medir dónde está parado el experimento contra esta proyección.
        builder.markdown(
            "## Cómo leer los gráficos (y su espejo en el controller)\n\n"
            f"**Curva de inventario %{base_asset}(P)** — proyección TEÓRICA anclada en tu "
            "inventario actual: qué composición tendrías si el precio fuera a P, "
            "cruzando las grillas del camino, cada una cortada por su **target local** "
            "(techo en A → piso en B). En el precio actual coincide con tu % real.\n"
            "→ *En vivo:* snapshots `data/chessboard_snapshots_<id>.jsonl` "
            "(`real` vs `teorico` vs `drift`, cada ~60s) y el gráfico del `status` "
            "(● real contra la curva objetivo).\n\n"
            "**NAV estrategia vs HOLD** — el NAV si el tablero recorre la curva, "
            "contra no hacer nada (inventario inicial congelado, valorizado a P). "
            "La brecha es lo que el rebalanceo + rebates deben justificar.\n"
            "→ *En vivo:* `nav` del snapshot (real, desde fills); el hold se "
            "reconstruye valorizando `base_assigned`/`quote_assigned` al precio.\n\n"
            "**Volumen equivalente de rebates** — cuánto volumen maker hace falta "
            "para que el rebate (+0.015%) cubra esa brecha.\n"
            "→ *En vivo:* línea *Rotación* del `status` (volumen, turnover, tasa/h) "
            "+ eventos `data/chessboard_events_<id>.jsonl` (un cierre por línea, "
            "con `capital.filled_quote` y `delta_btc`).\n\n"
            "**Candles + niveles** — contexto de mercado: velas, S/R, bordes A/B y "
            "los niveles internos de cada grilla. No requiere relevar nada del "
            "controller."
        )

        if fig_candles is not None:
            builder.plotly(fig_candles)

        # Nota sobre la banda de tolerancia, antes del gráfico de variación del
        # portfolio: la curva es el ideal determinístico; en vivo el par cicla
        # alrededor de ella dentro de ±tol (eso genera el volumen maker).
        _tol = config.target_tolerance_pct
        builder.markdown(
            f"**Banda de tolerancia (`target_tolerance_pct` = {_tol:.2%}):** la curva "
            f"de abajo es la trayectoria determinística IDEAL. En vivo, el controller "
            f"deja vivir a AMBOS lados del par dentro de ±{_tol:.1%} alrededor del "
            f"target local de cada escalón (capital NAV·tol ≈ "
            f"{_compact(nav_brl * _tol)} por lado al estar en la curva) — el par "
            f"cicla y genera volumen maker en vez de morir al llegar al target. "
            f"Implicancia: el %{base_asset} real puede desviarse hasta ±{_tol:.1%} "
            f"del teórico en cualquier punto, incluidos los extremos "
            f"(techo {config.techo_pct_btc:.0%} puede tocar "
            f"{config.techo_pct_btc + _tol:.0%}; piso {config.target_pct_btc:.0%} "
            f"puede tocar {config.target_pct_btc - _tol:.0%})."
        )
        builder.plotly(fig_inv_nav)

        # El YAML va al FINAL del reporte (es el artefacto de salida, no la lectura).
        builder.markdown(yaml_block)

        await builder.save()

    except Exception as e:
        logger.error(f"ReportBuilder falló: {e}", exc_info=True)

    # ── 10. Enviar imágenes + caption + botón por Telegram ───────────────────
    if chat_id and context.bot:
        try:
            import io
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup

            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Confirmar deploy", callback_data="routines:chessboard_lab:confirm_deploy")
            ]])

            imgs = []
            if fig_candles is not None:
                imgs.append(("📊 Candles + Grillas", fig_candles, 1200, 600))
            if fig_inv_nav is not None:
                imgs.append(("📈 Curva Inventario / NAV", fig_inv_nav, 1200, 920))

            for i, (label, fig, w, h) in enumerate(imgs):
                png = fig.to_image(format="png", width=w, height=h)
                # caption en la primera foto; el botón va en un mensaje aparte
                caption = tg_text[:1020] if i == 0 else label
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=io.BytesIO(png),
                    caption=caption,
                    parse_mode="Markdown",
                )

            # Mensaje separado con el botón
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            kb = InlineKeyboardMarkup.from_button(
                InlineKeyboardButton(text="✅ Confirmar deploy", callback_data="routines:chessboard_lab:confirm_deploy")
            )
            msg = await context.bot.send_message(
                chat_id=chat_id,
                text="¿Lanzamos el bot con esta config?",
                reply_markup=kb,
            )
            logger.info(f"Mensaje con botón enviado: id={msg.message_id} has_markup={msg.reply_markup is not None}")
        except Exception as e:
            logger.error(f"Telegram send_photo/keyboard falló: {e}", exc_info=True)
            try:
                await context.bot.send_message(chat_id=chat_id, text=tg_text, parse_mode="Markdown")
            except Exception as e2:
                logger.warning(f"Telegram fallback send_message falló: {e2}")

    # ── 11. Datos estructurados para el agente ────────────────────────────────
    sections = [
        {
            "type": "kpis",
            "data": {
                "rango_a": A,
                "rango_b": B,
                "amplitud_pct": round(_amplitud * 100, 2),
                "n_grillas": selected_grid,
                "precio_actual": btc_brl_price,
                "precio_dentro_rango": A < btc_brl_price < B,
                "dist_borde_a_pct": round(_dist_a * 100, 1),
                "dist_borde_b_pct": round(_dist_b * 100, 1),
                "pct_btc_actual": round(pct_btc_inicial * 100, 1),
                "cap_por_salto_brl": round(cap_sel, 0),
                "nav_asignado_brl": round(nav_brl, 0),
                "warnings": warnings,
            },
        },
        {
            "type": "yaml_config",
            "data": yaml_block,
        },
    ]

    return RoutineResult(text=tg_text, sections=sections)


async def handle_callback(update, context: ContextTypes.DEFAULT_TYPE, action: str, params: list) -> None:
    """Responde al botón ✅ Confirmar deploy (sin deployar nada por ahora)."""
    query = update.callback_query
    await query.answer("Recibido ✅")
    if action == "confirm_deploy":
        await query.edit_message_caption(
            caption=(query.message.caption or "") + "\n\n✅ *Confirmado* — pendiente de implementación del deploy.",
            parse_mode="Markdown",
        )
