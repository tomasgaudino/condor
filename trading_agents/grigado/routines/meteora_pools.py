"""Meteora DLMM pools para los tokens del portfolio (ambos lados presentes).

Primer paso del Grigado-DLMM: descubrir en qué pools de Meteora podrías proveer
liquidez YA con lo que tenés en la wallet (Phantom/Solana del servidor). Lee el
portfolio, arma los pares posibles entre tus tokens, busca los pools de cada par
en Meteora y los rankea por fee/TVL ratio (eficiencia del capital — el mejor
proxy de dónde tu liquidez genera más fees, coherente con la tesis chessboard de
vivir del volumen/fees, no del precio).

NO abre posiciones ni toca la wallet: es exploración pura.
"""

CATEGORY = "Grigado DLMM"

import itertools
import logging

from pydantic import BaseModel, Field
from telegram.ext import ContextTypes

from config_manager import get_client
from routines.base import RoutineResult

logger = logging.getLogger(__name__)


class Config(BaseModel):
    """Pools de Meteora DLMM para los tokens del portfolio (ambos lados presentes)."""
    connector: str = Field(default="meteora", description="Conector CLMM (meteora).")
    solana_connector: str = Field(default="solana", description="Nombre del conector Solana en el portfolio (de get_state).")
    min_token_value_usd: float = Field(default=1.0, description="Valor mínimo (USD) de un token para considerarlo (filtra polvo).")
    max_pools_per_pair: int = Field(default=5, description="Cuántos pools traer por par (Meteora puede tener varios bin_step por par).")
    top_n: int = Field(default=25, description="Cuántos pools mostrar en el reporte final (rankeados por fee/TVL).")


# ── helpers de parsing robusto (el endpoint puede variar el formato) ──────────
def _pools_from_resp(resp) -> list:
    """La respuesta puede ser list o dict envolvente {pools|data|results: [...]}."""
    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        for k in ("pools", "data", "results", "items"):
            v = resp.get(k)
            if isinstance(v, list):
                return v
    return []


def _f(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _pool_pair_tokens(pool: dict) -> tuple[str, str] | None:
    """(base, quote) en MAYÚSCULAS desde el pool, tolerando varios nombres de campo."""
    tp = pool.get("trading_pair") or pool.get("name") or ""
    if isinstance(tp, str) and ("-" in tp or "/" in tp):
        sep = "-" if "-" in tp else "/"
        a, b = tp.upper().split(sep)[:2]
        return a.strip(), b.strip()
    # fallback a símbolos de los mints
    a = pool.get("base_symbol") or pool.get("mint_x_symbol")
    b = pool.get("quote_symbol") or pool.get("mint_y_symbol")
    if a and b:
        return str(a).upper(), str(b).upper()
    return None


def _fee_tvl_ratio(pool: dict) -> float:
    """fee/TVL del pool — el ranking pedido. Usa el campo nativo si está; si no,
    lo deriva de fees_24h / liquidity."""
    for k in ("fee_tvl_ratio_hour_24", "fee_tvl_ratio", "feetvlratio"):
        if k in pool:
            return _f(pool[k])
    liq = _f(pool.get("liquidity") or pool.get("tvl"))
    fees = _f(pool.get("fees_24h") or pool.get("volume_hour_24"))
    return (fees / liq) if liq > 0 else 0.0


async def run(config: Config, context: ContextTypes.DEFAULT_TYPE) -> RoutineResult:
    chat_id = context._chat_id if hasattr(context, "_chat_id") else None
    client = await get_client(chat_id, context=context)
    if not client:
        return RoutineResult(text="No server available")

    # ── 1. Tokens del portfolio en el conector Solana ────────────────────────
    try:
        state = await client.portfolio.get_state(refresh=False)
    except Exception as e:
        return RoutineResult(text=f"❌ get_state falló: {e}")

    held: dict[str, float] = {}  # símbolo -> valor USD acumulado
    for account_data in (state or {}).values():
        for conn_name, balances in account_data.items():
            if config.solana_connector.lower() not in conn_name.lower():
                continue
            if not isinstance(balances, list):
                continue
            for b in balances:
                sym = str(b.get("token", "")).upper()
                val = _f(b.get("value"))
                if sym:
                    held[sym] = held.get(sym, 0.0) + val

    held = {s: v for s, v in held.items() if v >= config.min_token_value_usd}
    if len(held) < 2:
        toks = ", ".join(sorted(held)) or "ninguno"
        return RoutineResult(
            text=f"Solo {len(held)} token(s) en {config.solana_connector} "
                 f"(≥${config.min_token_value_usd}): {toks}. Hacen falta ≥2 para armar pares.")

    symbols = sorted(held, key=lambda s: -held[s])

    # ── 2. Buscar pools por par; quedarse con los que tienen AMBOS lados ──────
    seen_addr = set()
    matched: list[dict] = []
    notes: list[str] = []
    for tok_a, tok_b in itertools.combinations(symbols, 2):
        try:
            resp = await client.gateway_clmm.get_pools(
                connector=config.connector, search_term=tok_a,
                limit=max(50, config.max_pools_per_pair * 4),
                sort_key="feetvlratio", order_by="desc", include_unknown=True)
        except Exception as e:
            notes.append(f"búsqueda {tok_a} falló: {e}")
            continue
        kept = 0
        for pool in _pools_from_resp(resp):
            pair = _pool_pair_tokens(pool)
            if not pair:
                continue
            x, y = pair
            # ambos lados deben estar en el portfolio Y matchear este par
            if {x, y} == {tok_a, tok_b}:
                addr = pool.get("address") or pool.get("pool_address")
                if addr in seen_addr:
                    continue
                seen_addr.add(addr)
                matched.append({
                    "pair": f"{x}-{y}", "address": addr,
                    "bin_step": pool.get("bin_step"),
                    "price": _f(pool.get("current_price")),
                    "tvl": _f(pool.get("liquidity") or pool.get("tvl")),
                    "vol_24h": _f(pool.get("volume_24h") or pool.get("volume_hour_24")),
                    "fee_pct": _f(pool.get("base_fee_percentage")),
                    "apr": _f(pool.get("apr")),
                    "fee_tvl": _fee_tvl_ratio(pool),
                    "held_usd": held.get(x, 0) + held.get(y, 0),
                })
                kept += 1
                if kept >= config.max_pools_per_pair:
                    break

    if not matched:
        toks = ", ".join(symbols)
        return RoutineResult(
            text=f"Tokens en Solana: {toks}\nNingún pool de Meteora con AMBOS lados "
                 f"en el portfolio.\n" + ("\n".join(notes) if notes else ""))

    matched.sort(key=lambda p: -p["fee_tvl"])
    top = matched[:config.top_n]

    # ── 3. Reporte ───────────────────────────────────────────────────────────
    rows = [{
        "Par": p["pair"],
        "bin": p["bin_step"],
        "fee/TVL": f"{p['fee_tvl']:.4f}",
        "Fee %": f"{p['fee_pct']:.3f}",
        "APR": f"{p['apr']:.1f}%",
        "TVL": f"${p['tvl']:,.0f}",
        "Vol 24h": f"${p['vol_24h']:,.0f}",
        "Precio": f"{p['price']:,.4f}",
    } for p in top]

    text = (f"🌊 Meteora DLMM — {len(matched)} pools con ambos lados en tu portfolio "
            f"({len(symbols)} tokens). Top {len(top)} por fee/TVL:\n"
            f"  1) {top[0]['pair']} bin {top[0]['bin_step']} · "
            f"fee/TVL {top[0]['fee_tvl']:.4f} · TVL ${top[0]['tvl']:,.0f}")

    try:
        from condor.reports import ReportBuilder
        builder = ReportBuilder("Meteora DLMM — pools del portfolio")
        builder.source("routine", "meteora_pools").tags(["grigado", "dlmm", "meteora", "solana"])
        builder.kpi("Pools (ambos lados)", str(len(matched)),
                    delta=f"{len(symbols)} tokens en portfolio", trend="neutral")
        builder.kpi("Mejor fee/TVL", f"{top[0]['fee_tvl']:.4f}",
                    delta=top[0]["pair"], trend="up")
        builder.markdown(
            "**Ranking por fee/TVL** (fee generado por unidad de liquidez — proxy de "
            "dónde tu capital trabaja más). Filtro: pools donde tenés AMBOS tokens "
            "del par. Próximo paso del Grigado-DLMM: elegir un pool y definir el rango "
            "de bins (equivalente al rango A-B del tablero spot).")
        builder.table(rows)
        if notes:
            builder.markdown("**Notas:**\n" + "\n".join(f"- {n}" for n in notes))
        await builder.save()
    except Exception as e:
        logger.warning(f"Report save falló: {e}")

    return RoutineResult(text=text, table_data=rows,
                         table_columns=list(rows[0].keys()) if rows else None)
