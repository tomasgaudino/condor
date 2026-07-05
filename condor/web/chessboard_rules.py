"""Motor de reglas de riesgo para ajustes en vivo de chessboard_lite (Fase A).

Funciones puras y deterministas: mismo cambio → mismo veredicto, siempre.
Cada regla está nombrada y versionada (spec: docs/chessboard_lite_live_ops.md);
devuelve ``{"tag", "rule", "reason"}`` o ``None`` si no aplica. El journal
guarda qué reglas dispararon y con qué versión → calibración posterior.

Tags: "green" (ok explícito) · "yellow" (ojo) · "red" (frená y mirá).
El veredicto global es el peor tag presente (worst_tag).
"""

from __future__ import annotations

from typing import Optional

RULES_VERSION = "v1"

# Fees medidos en producción (binance BTC-BRL, % por leg).
FEE_MAKER_PCT = 0.043
REBATE_MAKER_PCT = 0.015

_TAG_ORDER = {"green": 0, "yellow": 1, "red": 2}


def worst_tag(tags: list) -> str:
    """El peor tag presente manda. Sin tags → green (nada disparó)."""
    tags = [t for t in tags if t in _TAG_ORDER]
    if not tags:
        return "green"
    return max(tags, key=lambda t: _TAG_ORDER[t])


def _res(tag: str, rule: str, reason: str) -> dict:
    return {"tag": tag, "rule": rule, "reason": reason}


# --------------------------------------------------------------------------- #
# Reglas
# --------------------------------------------------------------------------- #
def r1_precio_pegado_al_borde(price: float, min_price: float, max_price: float,
                              margin_pct: float = 10.0) -> Optional[dict]:
    """R1: precio adentro pero pegado a un borde del rango nuevo.

    Distancia al borde más cercano, en % del ancho del rango:
    < 2% → red (nace al borde de dormirse) · < margen de histéresis → yellow.
    """
    width = max_price - min_price
    if width <= 0 or not (min_price <= price <= max_price):
        return None  # rango degenerado o precio afuera → lo cubre R2
    d_lo, d_hi = price - min_price, max_price - price
    dist_pct = min(d_lo, d_hi) / width * 100.0
    borde = "piso" if d_lo <= d_hi else "techo"
    if dist_pct < 2.0:
        return _res("red", "R1",
                    f"precio a {dist_pct:.1f}% del {borde} nuevo — casi afuera (umbral 2%)")
    if dist_pct < margin_pct:
        return _res("yellow", "R1",
                    f"precio a {dist_pct:.1f}% del {borde} nuevo (margen hist. {margin_pct:.0f}%)")
    return None


def r2_precio_fuera_de_rango(price: float, min_price: float,
                             max_price: float) -> Optional[dict]:
    """R2: precio actual fuera del [min,max] nuevo → la grilla nace dormida."""
    if min_price <= price <= max_price:
        return None
    lado = "debajo del piso" if price < min_price else "encima del techo"
    return _res("red", "R2",
                f"precio actual {price:,.2f} {lado} nuevo "
                f"[{min_price:,.2f}, {max_price:,.2f}] — la grilla nace dormida")


def r4_lado_nace_bloqueado(side: str, inv_pct: Optional[float], floor_pct: float,
                           ceiling_pct: float, rebalance: bool) -> Optional[dict]:
    """R4: un lado nace BLOCKED_BAND con el inventario actual vs la banda.

    LONG no lanza si inv >= techo (ya sobra base); SHORT no lanza si inv <= piso
    (no hay base para vender). Con rebalanceo pedido → green: el rebalanceo
    lleva el inventario a 50% y el lado nace ACTIVE.
    """
    if inv_pct is None:
        return None  # sin dato de inventario no se puede afirmar nada
    side = side.lower()
    if side == "long" and inv_pct >= ceiling_pct:
        detalle = f"inv {inv_pct:.0f}% ≥ techo {ceiling_pct:.0f}% — LONG nace BLOCKED_BAND"
    elif side == "short" and inv_pct <= floor_pct:
        detalle = f"inv {inv_pct:.0f}% ≤ piso {floor_pct:.0f}% — SHORT nace BLOCKED_BAND"
    else:
        return None
    if rebalance:
        return _res("green", "R4",
                    f"{detalle}; el rebalanceo pedido lo resuelve (inv → 50%)")
    return _res("yellow", "R4", f"{detalle}; sin rebalanceo queda medio motor apagado")


def r7_edge_negativo(take_profit_pct: float, fee_pct: float = FEE_MAKER_PCT,
                     rebate_pct: float = REBATE_MAKER_PCT) -> Optional[dict]:
    """R7: edge por round-trip ≤ 0 → cada trade pierde plata.

    edge_rt = tp − 2·fee + 2·rebate (ambos legs LIMIT_MAKER, en %).
    """
    edge = take_profit_pct - 2.0 * fee_pct + 2.0 * rebate_pct
    if edge <= 0:
        return _res("red", "R7",
                    f"edge negativo: TP {take_profit_pct}% − 2×fee {fee_pct}% "
                    f"+ 2×rebate {rebate_pct}% = {edge:.3f}% por round-trip")
    return None


def r8_tp_step_insano(take_profit_pct: float, step_pct: float) -> Optional[dict]:
    """R8: ratio tp/step fuera de [0.5, 2] — la lab dice tp≈step es lo sano."""
    if step_pct <= 0:
        return None
    ratio = take_profit_pct / step_pct
    if ratio < 0.5 or ratio > 2.0:
        return _res("yellow", "R8",
                    f"tp/step = {ratio:.2f} fuera de [0.5, 2] "
                    f"(TP {take_profit_pct}% vs step {step_pct:.3f}%) — tp≈step es lo sano")
    return None


def r9_order_amount_bajo_minimo(order_amount_quote: float,
                                min_notional: float) -> Optional[dict]:
    """R9: order_amount por nivel del sim nuevo < min_notional del exchange."""
    if order_amount_quote < min_notional:
        return _res("red", "R9",
                    f"order_amount {order_amount_quote:,.2f} < min_notional "
                    f"{min_notional:,.2f} del exchange — las órdenes rebotan")
    return None


def r10_caida_de_edge(pnl_day_old: Optional[float],
                      pnl_day_new: Optional[float]) -> Optional[dict]:
    """R10: el edge/día proyectado cae a menos de la mitad → yellow; sube → green."""
    if pnl_day_old is None or pnl_day_new is None:
        return None
    if pnl_day_old > 0 and pnl_day_new < 0.5 * pnl_day_old:
        return _res("yellow", "R10",
                    f"edge/día proyectado cae {pnl_day_old:,.2f} → {pnl_day_new:,.2f} "
                    f"({pnl_day_new / pnl_day_old * 100:.0f}% del actual)")
    if pnl_day_new > pnl_day_old:
        return _res("green", "R10",
                    f"edge/día proyectado sube {pnl_day_old:,.2f} → {pnl_day_new:,.2f}")
    return None


def r11_be_huerfano_fuera(open_quote: float, be_buy: Optional[float],
                          be_sell: Optional[float], min_price: float,
                          max_price: float) -> Optional[dict]:
    """R11: hay abierto huérfano y su break-even queda fuera del rango nuevo →
    la grilla nueva no le arma la escalera de TPs para cerrarlo."""
    if open_quote <= 0:
        return None
    afuera = []
    for label, be in (("BE buy", be_buy), ("BE sell", be_sell)):
        if be is not None and not (min_price <= be <= max_price):
            afuera.append(f"{label} {be:,.2f}")
    if afuera:
        return _res("yellow", "R11",
                    f"abierto huérfano de {open_quote:,.0f} quote con "
                    f"{' y '.join(afuera)} fuera del rango nuevo — queda sin escalera de TPs")
    return None


def r12_payback_rebalanceo(payback_days: Optional[float]) -> Optional[dict]:
    """R12: payback del costo del rebalanceo vs edge/día ganado.

    ≤ 30 días → green · > 30 → yellow · > 90 → red.
    """
    if payback_days is None:
        return None
    if payback_days > 90:
        return _res("red", "R12",
                    f"payback del rebalanceo ≈ {payback_days:.0f} días (> 90) — "
                    f"el costo no se recupera en un horizonte razonable")
    if payback_days > 30:
        return _res("yellow", "R12",
                    f"payback del rebalanceo ≈ {payback_days:.0f} días (> 30)")
    return _res("green", "R12", f"payback del rebalanceo ≈ {payback_days:.1f} días (≤ 30)")
