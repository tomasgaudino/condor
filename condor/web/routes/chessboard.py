"""Chessboard Lite — datos para el tablero interactivo del frontend.

Recicla la lógica de la routine `grigado/chessboard_lite_dashboard` (grillas
activas, fills en vivo del contenedor, FIFO price-aware con atribución por
nivel, telemetría del controller) cargando el módulo por path — la misma
mecánica que usa el loader de routines. El endpoint devuelve JSON crudo; toda
la visualización (perfiles de volumen, curva de inventario, PnL por nivel)
se computa en el frontend, que reacciona a la ventana visible del chart.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
import types
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from config_manager import get_config_manager
from condor.web import chessboard_journal as journal
from condor.web import chessboard_rules as rules_engine
from condor.web.auth import get_current_user
from condor.web.models import WebUser

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chessboard"])

_ROUTINES_DIR = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "trading_agents" / "grigado" / "routines"
)
_ROUTINE_PATH = _ROUTINES_DIR / "chessboard_lite_dashboard.py"
_MODULE_NAME = "condor_web_chessboard_lite_dashboard"
_LAB_PATH = _ROUTINES_DIR / "chessboard_lite_lab.py"
_LAB_MODULE_NAME = "condor_web_chessboard_lite_lab"


def _load_module(module_name: str, path: Path):
    """Carga (una vez) un módulo de routine por path — misma mecánica que el
    loader de routines: no son importables por nombre de paquete."""
    mod = sys.modules.get(module_name)
    if mod is not None:
        return mod
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"No pude cargar la routine en {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return mod


def _routine():
    """Load (once) the dashboard routine module and reuse its data functions."""
    return _load_module(_MODULE_NAME, _ROUTINE_PATH)


def _lab():
    """Routine de la lab (simulación fiel del executor + economía)."""
    return _load_module(_LAB_MODULE_NAME, _LAB_PATH)


def _iso(ts) -> Optional[str]:
    """pandas Timestamp -> ISO string (o None)."""
    try:
        return ts.isoformat() if ts is not None else None
    except Exception:
        return None


def _clean_fill(f: dict) -> dict:
    return {
        "ts": int(f.get("ts", 0)),
        "price": float(f.get("price", 0.0)),
        "base": float(f.get("base", 0.0)),
        "qvol": float(f.get("qvol", 0.0)),
        "side": f.get("side", ""),
        "matched": float(f.get("matched", 0.0)),
        "open": float(f.get("open", 0.0)),
        "fee_quote": float(f.get("fee_quote", 0.0)),
    }


def _clean_fifo(fifo: dict) -> dict:
    return {
        "realized": float(fifo.get("realized", 0.0)),
        "fees": float(fifo.get("fees", 0.0)),
        "be_buy": fifo.get("be_buy"),
        "be_sell": fifo.get("be_sell"),
        "be_general": fifo.get("be_general"),
        "open_long_base": float(fifo.get("open_long_base", 0.0)),
        "open_short_base": float(fifo.get("open_short_base", 0.0)),
        "matched_quote": float(fifo.get("matched_quote", 0.0)),
        "open_quote": float(fifo.get("open_quote", 0.0)),
        "t0": fifo.get("t0"),
        "matches": [
            {"side": m["side"], "level": float(m["level"]), "spread": float(m["spread"])}
            for m in (fifo.get("matches") or [])
        ],
        "open_lots": [
            {"side": l["side"], "price": float(l["price"]), "base": float(l["base"])}
            for l in (fifo.get("open_lots") or [])
        ],
    }


@router.get("/servers/{name}/chessboard/state")
async def get_chessboard_state(
    name: str,
    controller_filter: str = Query("chessboard_lite"),
    user: WebUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Estado completo del ecosistema chessboard_lite en un server.

    grids: config + performance + telemetría + alertas + fills (FIFO aplicado).
    prices: precio actual por trading_pair.
    """
    cm = get_config_manager()
    if not cm.has_server_access(user.id, name):
        raise HTTPException(status_code=403, detail="No access")

    client = await cm.get_client(name)
    try:
        rt = _routine()
    except Exception as e:
        logger.error("No pude cargar la routine chessboard_lite_dashboard: %s", e)
        raise HTTPException(status_code=500, detail=f"routine load: {e}")

    try:
        grids, bots = await rt._collect_grids(client, controller_filter)
    except Exception as e:
        logger.warning("chessboard: _collect_grids falló en '%s': %s", name, e)
        return {"server_online": False, "error_hint": str(e), "grids": [], "prices": {}}

    if not grids:
        return {"server_online": True, "grids": [], "prices": {}, "bots": bots}

    # Telemetría (custom_info) — degradación limpia si el controller es pre-fix.
    try:
        tele = await rt._fetch_telemetry(client, grids)
    except Exception as e:
        logger.debug("chessboard: telemetría no disponible: %s", e)
        tele = {}

    # Precio actual por par (una vez por (connector, pair)).
    prices: dict[str, float] = {}
    for key in {(g["connector"], g["trading_pair"]) for g in grids}:
        connector, pair = key
        try:
            p = await client.market_data.get_prices(connector, pair)
            live = p.get("prices", {}).get(pair) if isinstance(p, dict) else None
            if live:
                prices[pair] = float(live)
        except Exception:
            pass

    # Fills en vivo por bot (cache) + FIFO por grilla, en paralelo por bot.
    bot_names = sorted({g["bot_name"] for g in grids})

    async def _fetch(bn: str):
        try:
            return bn, await rt._bot_history_trades(client, bn)
        except Exception as e:
            logger.debug("chessboard: fills de %s fallaron: %s", bn, e)
            return bn, []

    hist_cache = dict(await asyncio.gather(*(_fetch(bn) for bn in bot_names)))

    out_grids = []
    for g in grids:
        fills = [t for t in hist_cache.get(g["bot_name"], [])
                 if not t["symbol"] or t["symbol"] == g["trading_pair"]]
        fifo = rt._fifo_match(fills)
        latest = (tele.get(g["id"]) or {}).get("latest", {})
        alerts = rt._telemetry_alerts(g, latest) if latest else []
        out_grids.append({
            "id": g["id"],
            "bot_name": g["bot_name"],
            "connector": g["connector"],
            "trading_pair": g["trading_pair"],
            "low": float(g["low"]),
            "high": float(g["high"]),
            "n_levels": int(g["n_levels"]),
            "liquidity": float(g["liquidity"]),
            "inv_floor": float(g["inv_floor"]),
            "inv_ceiling": float(g["inv_ceiling"]),
            "realized": float(g["realized"]),
            "unrealized": float(g["unrealized"]),
            "pnl": float(g["pnl"]),
            "volume": float(g["volume"]),
            "deployed_at": _iso(g.get("deployed_at")),
            "telemetry": latest or None,
            "alerts": alerts,
            "fifo": _clean_fifo(fifo),
            "fills": [_clean_fill(f) for f in fills],
        })

    return {
        "server_online": True,
        "grids": out_grids,
        "prices": prices,
        "bots": bots,
    }


# --------------------------------------------------------------------------- #
# Live ops Fase A — cuadro de impacto + journal de decisiones
# (spec: hummingbot/docs/chessboard_lite_live_ops.md)
# --------------------------------------------------------------------------- #
class DraftConfig(BaseModel):
    """Draft de ajuste: solo lo tocado; None = mantener el valor actual."""

    min_price: float
    max_price: float
    n_levels: Optional[int] = None
    take_profit_pct: Optional[float] = None
    total_amount_quote: Optional[float] = None


class ImpactRequest(BaseModel):
    draft: DraftConfig
    rebalance: bool = True


class AdjustmentRequest(BaseModel):
    """Registro de una decisión: el impact completo + qué decidió el operador."""

    impact: dict
    draft: dict
    accepted: bool
    contradicted: bool = False
    note: Optional[str] = None


def _f(value, default: float = 0.0) -> float:
    """Coerción best-effort a float (los configs a veces vienen como strings)."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _jsonable(obj):
    """Sanitiza tipos numpy/pandas para la respuesta JSON."""
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if obj is None or isinstance(obj, (str, bool, int, float)):
        return obj
    try:
        return float(obj)
    except (TypeError, ValueError):
        return str(obj)


def _num_changed(a, b) -> bool:
    """¿Cambió un valor numérico? (tolerancia relativa, evita falsos diffs)."""
    try:
        fa, fb = float(a), float(b)
    except (TypeError, ValueError):
        return a != b
    return abs(fa - fb) > 1e-9 * max(1.0, abs(fa), abs(fb))


def _entry(name: str, current, proposed, rule_res: Optional[dict] = None) -> dict:
    """Fila del diff (params o derivados): valor actual vs propuesto + tag de regla."""
    if isinstance(current, str) or isinstance(proposed, str):
        changed = current != proposed
    else:
        changed = _num_changed(current, proposed)
    return {
        "name": name,
        "current": current,
        "proposed": proposed,
        "changed": changed,
        "tag": (rule_res or {}).get("tag"),
        "rule": (rule_res or {}).get("rule"),
        "reason": (rule_res or {}).get("reason"),
    }


def _worst_rule(*results: Optional[dict]) -> Optional[dict]:
    """De varias reglas sobre el mismo campo, la de peor tag."""
    hits = [r for r in results if r]
    if not hits:
        return None
    return max(hits, key=lambda r: {"green": 0, "yellow": 1, "red": 2}.get(r["tag"], 0))


def _birth_state(side: str, inv_pct: Optional[float], floor_pct: float,
                 ceiling_pct: float) -> str:
    """Estado con el que nace un lado dado el inventario vs la banda dura."""
    if inv_pct is None:
        return "s/d"
    if side == "long" and inv_pct >= ceiling_pct:
        return "BLOCKED_BAND"
    if side == "short" and inv_pct <= floor_pct:
        return "BLOCKED_BAND"
    return "ACTIVE"


@router.post("/servers/{name}/chessboard/{grid_id}/impact")
async def post_chessboard_impact(
    name: str,
    grid_id: str,
    req: ImpactRequest,
    user: WebUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Cuadro de impacto de un ajuste de grilla — NO muta nada.

    Tres bloques (contrato con el frontend): diff de parámetros directos,
    diff de derivados (geometría real del executor + economía proyectada) y
    orden de rebalanceo. Cada tag sale de una regla nombrada y versionada
    (condor/web/chessboard_rules.py); el veredicto global es el peor tag.
    """
    cm = get_config_manager()
    if not cm.has_server_access(user.id, name):
        raise HTTPException(status_code=403, detail="No access")

    if req.draft.max_price <= req.draft.min_price:
        raise HTTPException(status_code=422, detail="max_price debe ser > min_price")

    client = await cm.get_client(name)
    try:
        rt = _routine()
        lab = _lab()
    except Exception as e:
        logger.error("impact: no pude cargar las routines: %s", e)
        raise HTTPException(status_code=500, detail=f"routine load: {e}")

    # ── Grilla viva + config crudo del controller ────────────────────────────
    try:
        grids, _bots = await rt._collect_grids(client, "chessboard_lite")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"server offline: {e}")
    g = next((x for x in grids if x["id"] == grid_id), None)
    if g is None:
        raise HTTPException(status_code=404, detail=f"grilla '{grid_id}' no encontrada")

    raw_cfg: dict = {}
    try:
        cfgs = await client.controllers.get_bot_controller_configs(g["bot_name"])
        for c in cfgs or []:
            if isinstance(c, dict) and grid_id in (c.get("id"), c.get("controller_id")):
                raw_cfg = c
                break
    except Exception as e:
        logger.debug("impact: config crudo de %s no disponible: %s", grid_id, e)

    tp_cur = _f(raw_cfg.get("take_profit_pct"), 0.2)
    min_spread = _f(raw_cfg.get("min_spread_between_orders"), 0.002)
    margin_pct = _f(raw_cfg.get("relaunch_margin_pct"), 10.0)
    floor_pct, ceiling_pct = float(g["inv_floor"]), float(g["inv_ceiling"])

    cur = {
        "min_price": float(g["low"]),
        "max_price": float(g["high"]),
        "n_levels": max(1, int(g["n_levels"] or 0)),
        "take_profit_pct": tp_cur,
        "total_amount_quote": float(g["liquidity"]),
    }
    d = req.draft
    new = {
        "min_price": float(d.min_price),
        "max_price": float(d.max_price),
        "n_levels": int(d.n_levels) if d.n_levels else cur["n_levels"],
        "take_profit_pct": float(d.take_profit_pct) if d.take_profit_pct is not None else tp_cur,
        "total_amount_quote": (float(d.total_amount_quote)
                               if d.total_amount_quote is not None
                               else cur["total_amount_quote"]),
    }

    # ── Precio actual ────────────────────────────────────────────────────────
    price = 0.0
    try:
        p = await client.market_data.get_prices(g["connector"], g["trading_pair"])
        live = p.get("prices", {}).get(g["trading_pair"]) if isinstance(p, dict) else None
        price = float(live or 0.0)
    except Exception as e:
        logger.warning("impact: get_prices falló: %s", e)
    if price <= 0:
        raise HTTPException(status_code=502, detail="sin precio actual del par")

    # ── Telemetría (inv_pct) + fills → FIFO ──────────────────────────────────
    tele_latest: dict = {}
    try:
        tele = await rt._fetch_telemetry(client, [g])
        tele_latest = (tele.get(grid_id) or {}).get("latest") or {}
    except Exception as e:
        logger.debug("impact: telemetría no disponible: %s", e)
    inv_pct = tele_latest.get("inv_pct")
    inv_pct = float(inv_pct) if inv_pct is not None else None

    try:
        fills_all = await rt._bot_history_trades(client, g["bot_name"])
    except Exception:
        fills_all = []
    fills = [t for t in fills_all if not t["symbol"] or t["symbol"] == g["trading_pair"]]
    fifo = rt._fifo_match(fills)

    inv_estimated = False
    if inv_pct is None and cur["total_amount_quote"] > 0:
        # Sin telemetría: partiendo del baseline 50/50, el neto abierto del FIFO
        # (open buys − open sells, valorizado a precio actual) desplaza el inv%.
        net_base = fifo["open_long_base"] - fifo["open_short_base"]
        inv_pct = max(0.0, min(100.0, 50.0 + net_base * price / cur["total_amount_quote"] * 100.0))
        inv_estimated = True

    # ── Simulación fiel del executor: geometría vieja vs nueva ──────────────
    ex_rules = await lab._fetch_trading_rules(client, g["connector"], g["trading_pair"])
    min_notional = float(ex_rules.get("min_notional_size", 10.0))
    # El capital se parte 50/50 entre la grilla LONG y la SHORT (cap por grilla).
    sim_old = lab._simulate_executor_levels(
        cur["min_price"], cur["max_price"], cur["total_amount_quote"] / 2.0, price,
        cur["n_levels"], min_spread, ex_rules, 5.0)
    sim_new = lab._simulate_executor_levels(
        new["min_price"], new["max_price"], new["total_amount_quote"] / 2.0, price,
        new["n_levels"], min_spread, ex_rules, 5.0)

    # ── Economía proyectada (matemática de la lab, 7d de velas 5m) ───────────
    def _econ_cfg(tp: float) -> types.SimpleNamespace:
        return types.SimpleNamespace(
            connector_name=g["connector"], trading_pair=g["trading_pair"],
            econ_lookback_days=7, econ_interval="5m",
            take_profit_pct=tp, fee_maker_pct=rules_engine.FEE_MAKER_PCT,
        )

    econ_old = await lab._estimate_economics(
        client, _econ_cfg(cur["take_profit_pct"]), sim_old,
        cur["min_price"], cur["max_price"], cur["total_amount_quote"])
    econ_new = await lab._estimate_economics(
        client, _econ_cfg(new["take_profit_pct"]), sim_new,
        new["min_price"], new["max_price"], new["total_amount_quote"])
    pnl_day_old = econ_old.get("pnl_day")
    pnl_day_new = econ_new.get("pnl_day")

    # ── Orden de rebalanceo (inventario scopeado → 50/50) ────────────────────
    rebalance_block = None
    rebalance_effective = False
    if req.rebalance and inv_pct is not None:
        delta_quote = (50.0 - inv_pct) / 100.0 * new["total_amount_quote"]
        if abs(delta_quote) >= min_notional:
            rebalance_effective = True
            side = "BUY" if delta_quote > 0 else "SELL"
            base_amount = abs(delta_quote) / price

            # IL que se realiza: consumir lotes abiertos FIFO (más viejos primero)
            # del lado opuesto — SELL realiza los open BUY, BUY los open SELL.
            lots_side = "BUY" if side == "SELL" else "SELL"
            remaining, il_realized = base_amount, 0.0
            for lot in fifo["open_lots"]:
                if lot["side"] != lots_side or remaining <= 1e-12:
                    continue
                take = min(remaining, lot["base"])
                if side == "SELL":
                    il_realized += (price - lot["price"]) * take
                else:
                    il_realized += (lot["price"] - price) * take
                remaining -= take

            # LIMIT_MAKER sumándose al BBO → rebate: fee negativa = ingreso.
            est_fee = -(rules_engine.REBATE_MAKER_PCT / 100.0) * abs(delta_quote)

            # Payback: costo hoy (IL realizada como pérdida) / edge por día ganado.
            cost = max(0.0, -il_realized)
            gain = None
            if pnl_day_old is not None and pnl_day_new is not None:
                gain = pnl_day_new - pnl_day_old
            blocked_now = inv_pct >= ceiling_pct or inv_pct <= floor_pct
            if (gain is None or gain <= 0) and blocked_now and pnl_day_new and pnl_day_new > 0:
                gain = pnl_day_new / 2.0  # edge del lado que el rebalanceo desbloquea
            if cost <= 1e-9:
                payback = 0.0
            elif gain and gain > 0:
                payback = cost / gain
            else:
                payback = None

            r12 = rules_engine.r12_payback_rebalanceo(payback)
            reason_bits = []
            if r12:
                reason_bits.append(r12["reason"])
            if inv_estimated:
                reason_bits.append("inv% estimado por FIFO (sin telemetría)")
            rebalance_block = {
                "side": side,
                "base_amount": base_amount,
                "quote_value": abs(delta_quote),
                "from_inv_pct": inv_pct,
                "to_inv_pct": 50.0,
                "il_realized_quote": il_realized,
                "est_fee_quote": est_fee,
                "payback_days": payback,
                "tag": (r12 or {}).get("tag"),
                "rule": (r12 or {}).get("rule"),
                "reason": "; ".join(reason_bits) or None,
            }

    # ── Reglas sobre params y derivados ──────────────────────────────────────
    r1 = rules_engine.r1_precio_pegado_al_borde(
        price, new["min_price"], new["max_price"], margin_pct)
    r2 = rules_engine.r2_precio_fuera_de_rango(price, new["min_price"], new["max_price"])
    r7 = rules_engine.r7_edge_negativo(new["take_profit_pct"])
    step_old_pct = sim_old["step_pct"] * 100.0
    step_new_pct = sim_new["step_pct"] * 100.0
    r8 = rules_engine.r8_tp_step_insano(new["take_profit_pct"], step_new_pct)
    r9 = rules_engine.r9_order_amount_bajo_minimo(sim_new["order_amount_quote"], min_notional)
    r10 = rules_engine.r10_caida_de_edge(pnl_day_old, pnl_day_new)
    r4_long = rules_engine.r4_lado_nace_bloqueado(
        "long", inv_pct, floor_pct, ceiling_pct, rebalance_effective)
    r4_short = rules_engine.r4_lado_nace_bloqueado(
        "short", inv_pct, floor_pct, ceiling_pct, rebalance_effective)
    r11 = rules_engine.r11_be_huerfano_fuera(
        fifo["open_quote"], fifo["be_buy"], fifo["be_sell"],
        new["min_price"], new["max_price"])

    # R1/R2 se anclan al borde relevante (el otro borde queda sin tag).
    range_rule = _worst_rule(r1, r2)
    rule_min = rule_max = None
    if range_rule is not None:
        if price < new["min_price"]:
            rule_min = range_rule
        elif price > new["max_price"]:
            rule_max = range_rule
        elif (price - new["min_price"]) <= (new["max_price"] - price):
            rule_min = range_rule
        else:
            rule_max = range_rule

    params = [
        _entry("min_price", cur["min_price"], new["min_price"], rule_min),
        _entry("max_price", cur["max_price"], new["max_price"], rule_max),
        _entry("n_levels", cur["n_levels"], new["n_levels"]),
        _entry("take_profit_pct", cur["take_profit_pct"], new["take_profit_pct"], r7),
        _entry("total_amount_quote", cur["total_amount_quote"], new["total_amount_quote"]),
    ]

    # Estado de cada lado al nacer: hoy = telemetría (si publica), mañana =
    # inventario vs banda, con el rebalanceo aplicado si corresponde (inv → 50).
    inv_after = 50.0 if rebalance_effective else inv_pct
    birth_long_cur = ((tele_latest.get("long") or {}).get("state")
                      or _birth_state("long", inv_pct, floor_pct, ceiling_pct))
    birth_short_cur = ((tele_latest.get("short") or {}).get("state")
                       or _birth_state("short", inv_pct, floor_pct, ceiling_pct))
    derived = [
        _entry("step_pct", step_old_pct, step_new_pct, r8),
        _entry("order_amount_quote", sim_old["order_amount_quote"],
               sim_new["order_amount_quote"], r9),
        _entry("edge_day", pnl_day_old, pnl_day_new, r10),
        _entry("time_in_range_pct", econ_old.get("time_in_range_pct"),
               econ_new.get("time_in_range_pct")),
        _entry("side_at_birth_long", birth_long_cur,
               _birth_state("long", inv_after, floor_pct, ceiling_pct), r4_long),
        _entry("side_at_birth_short", birth_short_cur,
               _birth_state("short", inv_after, floor_pct, ceiling_pct), r4_short),
    ]

    # ── Abierto huérfano al frenar ───────────────────────────────────────────
    bes = [b for b in (fifo["be_buy"], fifo["be_sell"]) if b is not None]
    orphan = {
        "open_quote": fifo["open_quote"],
        "be_buy": fifo["be_buy"],
        "be_sell": fifo["be_sell"],
        "be_inside_new_range": (all(new["min_price"] <= b <= new["max_price"] for b in bes)
                                if bes else True),
        "tag": (r11 or {}).get("tag"),
        "rule": (r11 or {}).get("rule"),
        "reason": (r11 or {}).get("reason"),
    }

    # ── Veredicto global = el peor tag presente ──────────────────────────────
    all_tags = [e["tag"] for e in params + derived]
    all_tags.append(orphan["tag"])
    if rebalance_block:
        all_tags.append(rebalance_block["tag"])
    verdict = rules_engine.worst_tag(all_tags)

    return _jsonable({
        "grid_id": grid_id,
        "rules_version": rules_engine.RULES_VERSION,
        "verdict": verdict,
        "params": params,
        "derived": derived,
        "rebalance": rebalance_block,
        "orphan": orphan,
        "economics": {"old": econ_old, "new": econ_new},
    })


@router.post("/servers/{name}/chessboard/{grid_id}/adjustments")
async def post_chessboard_adjustment(
    name: str,
    grid_id: str,
    req: AdjustmentRequest,
    user: WebUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Journal de decisiones: registra el impact completo + qué hizo el operador
    (aceptó / contradijo el semáforo). Insumo de la routine de outcome (Fase C)."""
    cm = get_config_manager()
    if not cm.has_server_access(user.id, name):
        raise HTTPException(status_code=403, detail="No access")
    try:
        adj_id = journal.record_adjustment(
            server=name, grid_id=grid_id, draft=req.draft, impact=req.impact,
            accepted=req.accepted, contradicted=req.contradicted, note=req.note)
    except Exception as e:
        logger.error("journal: no pude registrar el ajuste: %s", e)
        raise HTTPException(status_code=500, detail=f"journal: {e}")
    return {"id": adj_id, "ok": True}


@router.get("/servers/{name}/chessboard/adjustments")
async def get_chessboard_adjustments(
    name: str,
    limit: int = Query(50, ge=1, le=500),
    user: WebUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Últimas decisiones journalizadas del server (más nuevas primero)."""
    cm = get_config_manager()
    if not cm.has_server_access(user.id, name):
        raise HTTPException(status_code=403, detail="No access")
    try:
        items = journal.list_adjustments(server=name, limit=limit)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"journal: {e}")
    return {"adjustments": items}
