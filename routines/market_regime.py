"""Market Regime — multi-timeframe classifier of market conditions.

Second MVP routine of the Adaptive Strategy Framework. Classifies the
current market regime for a trading pair across three timeframes
(5m / 1h / 1d) and combines them into a canonical label plus a
favorability flag for PMM strategies.

Spec:
    `.planning/strategy-framework/MARKET_REGIME_SPEC.md`

Output contract (user view vs agent view):
    `.planning/strategy-framework/AGENT_VS_USER_VIEW.md`
    section "market_regime".

Two views are emitted in the RoutineResult.sections list:

- `{"type": "data", "title": "payload", "data": <full payload>}` for
  archival, dashboards, debugging.
- `{"type": "data", "title": "agent",   "data": <minimal dict>}` for
  the adaptive agent's LLM — no narrative, no indicators, no glossary,
  no S/R prices.

Both per-routine views must evolve together when the agent's needs
change (rule 3 of AGENT_VS_USER_VIEW.md).
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timezone
from typing import Any

import numpy as np
from pydantic import BaseModel, Field
from telegram.ext import ContextTypes

from condor.trading_agent.adaptive import indicators as ind
from config_manager import get_client
from routines.base import RoutineResult

logger = logging.getLogger(__name__)

CATEGORY = "Adaptive Framework"


# ---------------------------------------------------------------------------
# Constants — thresholds and periods_per_year
# ---------------------------------------------------------------------------

# Periods-per-year for annualized vol, by candle interval.
PERIODS_PER_YEAR = {
    "1m":  525_600,
    "5m":  105_120,
    "15m": 35_040,
    "1h":  8_760,
    "4h":  2_190,
    "1d":  365,
}

# Directionality thresholds (overridable via Config)
HURST_TRENDING = 0.55
HURST_MEAN_REV = 0.45
ADX_TRENDING = 25.0


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class Config(BaseModel):
    """Multi-timeframe market regime classifier (multi-pair).

    Operates on N trading pairs in a single run. If ``trading_pairs``
    is empty the routine autodetects the pairs from the active
    controllers on the connected server (default behavior).
    """

    trading_pairs: list[str] = Field(
        default=[],
        description=(
            "List of pairs to classify. Empty = autodetect from active "
            "controllers (spot only). Explicit list overrides autodetect."
        ),
    )
    connector_name: str = Field(
        default="binance",
        description="Spot connector — pmm_mister does not run on perps.",
    )

    # Legacy single-pair shim. Kept so old callers / saved instance configs
    # still work; if non-empty and `trading_pairs` is empty, it becomes
    # the single pair to process.
    trading_pair: str | None = Field(
        default=None,
        description="DEPRECATED — use `trading_pairs`. Kept for back-compat.",
    )

    # Timeframes
    micro_interval: str = Field(default="5m")
    meso_interval: str = Field(default="1h")
    macro_interval: str = Field(default="1d")

    # Lookbacks
    micro_lookback_candles: int = Field(default=288)   # 24h of 5m
    meso_lookback_candles: int = Field(default=200)    # ~8d of 1h (Hurst needs lots)
    macro_lookback_candles: int = Field(default=90)    # 90d of 1d

    # Directionality thresholds
    hurst_trending_threshold: float = Field(default=HURST_TRENDING)
    hurst_mean_rev_threshold: float = Field(default=HURST_MEAN_REV)
    adx_trending_threshold: float = Field(default=ADX_TRENDING)


# ---------------------------------------------------------------------------
# Candle fetch
# ---------------------------------------------------------------------------


async def _fetch_candles(
    client, connector: str, pair: str, interval: str, max_records: int
) -> list[dict]:
    """Wrapper around `market_data.get_candles` that tolerates errors.

    The smoke test on 2026-05-12 confirmed the SDK returns `list[dict]`
    ascending by timestamp, with `timestamp/open/high/low/close/volume/
    quote_asset_volume/n_trades/taker_buy_*`.
    """
    try:
        raw = await client.market_data.get_candles(
            connector_name=connector,
            trading_pair=pair,
            interval=interval,
            max_records=max_records,
        )
    except Exception as e:
        logger.warning(
            "market_regime: fetch_candles failed for %s %s @ %s: %s",
            connector, pair, interval, e,
        )
        return []
    if not isinstance(raw, list):
        logger.warning(
            "market_regime: unexpected candle response shape %s (expected list)",
            type(raw).__name__,
        )
        return []
    return raw


# ---------------------------------------------------------------------------
# Per-timeframe classification
# ---------------------------------------------------------------------------


def _classify_directionality(
    *,
    hurst: float | None,
    adx_val: float | None,
    slope: float | None,
    cfg: Config,
) -> str:
    """Return one of: trending_up / trending_down / mean_reverting / random_walk.

    Logic:
    - If Hurst >= trending threshold OR ADX >= trending threshold:
      direction is trending — up/down determined by slope sign.
    - Elif Hurst <= mean-rev threshold: mean_reverting.
    - Else: random_walk.

    NaN inputs are treated as missing — fall through to random_walk if
    no signal is conclusive.
    """
    def _is_num(x):
        return x is not None and not (isinstance(x, float) and math.isnan(x))

    is_trending_h = _is_num(hurst) and hurst >= cfg.hurst_trending_threshold
    is_trending_a = _is_num(adx_val) and adx_val >= cfg.adx_trending_threshold

    if is_trending_h or is_trending_a:
        if _is_num(slope):
            return "trending_up" if slope > 0 else "trending_down"
        # No slope signal but signs of trend → call it random_walk (safer)
        return "random_walk"

    if _is_num(hurst) and hurst <= cfg.hurst_mean_rev_threshold:
        return "mean_reverting"

    return "random_walk"


def _classify_volatility(
    *, natr_now: float | None, natr_history: np.ndarray
) -> tuple[str, dict[str, Any]]:
    """Return (label, info). Label one of: low / moderate / high / unknown.

    Uses percentiles of the par's own NATR history (24h-ish window) —
    "high vol" is relative to the par, not absolute.
    """
    if natr_now is None or (isinstance(natr_now, float) and math.isnan(natr_now)):
        return "unknown", {"p33": None, "p67": None, "samples": 0}

    samples = int(len(natr_history))
    p33 = ind.percentile_safe(natr_history, 33)
    p67 = ind.percentile_safe(natr_history, 67)

    if samples < 20 or p33 is None or p67 is None:
        return "moderate", {"p33": p33, "p67": p67, "samples": samples}

    if natr_now < p33:
        label = "low"
    elif natr_now > p67:
        label = "high"
    else:
        label = "moderate"
    return label, {"p33": p33, "p67": p67, "samples": samples}


def _rolling_natr_history(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int
) -> np.ndarray:
    """Compute a rolling NATR series of length len(close)-period.

    Used to build the volatility percentile reference (relative
    classification — see spec).
    """
    n = len(close)
    if n < period + 1:
        return np.array([], dtype=float)
    tr = ind.true_range(high, low, close)  # length n-1
    if len(tr) < period:
        return np.array([], dtype=float)
    cum = np.cumsum(np.insert(tr, 0, 0.0))
    atr_series = (cum[period:] - cum[:-period]) / period   # length len(tr)-period+1
    # Align to close[i] such that ATR uses TRs up to and including step i.
    # close index of the ending step is `period`..`n-1`. So:
    aligned_close = close[period: period + len(atr_series)]
    return np.divide(
        atr_series, aligned_close, out=np.full_like(atr_series, np.nan), where=aligned_close > 0
    )


def _compute_micro_block(candles: list[dict], cfg: Config) -> dict[str, Any]:
    """5m: NATR, BB width, EMA slope, ADX, realized vol annualized."""
    arrs = ind.to_arrays(candles)
    high, low, close = arrs["high"], arrs["low"], arrs["close"]

    natr_14 = ind.natr(high, low, close, 14)
    bb_width_20 = ind.bollinger_width(close, 20)
    ema_slope_20 = ind.ema_slope(close, 20, lookback=10)
    adx_14 = ind.adx(high, low, close, 14)
    realized_vol = ind.realized_vol_annualized(close, 60, PERIODS_PER_YEAR["5m"])

    natr_history = _rolling_natr_history(high, low, close, 14)
    vol_label, vol_info = _classify_volatility(natr_now=natr_14, natr_history=natr_history)
    direction = _classify_directionality(
        hurst=None, adx_val=adx_14, slope=ema_slope_20, cfg=cfg
    )

    return {
        "directionality": direction,
        "volatility": vol_label,
        "indicators": {
            "natr_14": _safe(natr_14),
            "bb_width_20": _safe(bb_width_20),
            "ema_slope_20": _safe(ema_slope_20),
            "adx_14": _safe(adx_14),
            "realized_vol_60_annual": _safe(realized_vol),
        },
        "vol_history_24h": vol_info,
        "samples": len(close),
    }


def _compute_meso_block(candles: list[dict], cfg: Config) -> dict[str, Any]:
    """1h: Hurst, EMA slope, ATR (relative), linreg slope, S/R."""
    arrs = ind.to_arrays(candles)
    high, low, close = arrs["high"], arrs["low"], arrs["close"]

    hurst_100 = ind.hurst_rs(close, max_lag=20)
    ema_slope_50 = ind.ema_slope(close, 50, lookback=25)
    atr_50 = ind.atr(high, low, close, 50)
    atr_50_rel = (atr_50 / close[-1]) if (len(close) > 0 and close[-1] > 0 and not math.isnan(atr_50)) else math.nan
    linreg = ind.linreg_slope_relative(close[-100:]) if len(close) >= 100 else math.nan

    direction = _classify_directionality(
        hurst=hurst_100, adx_val=None, slope=linreg, cfg=cfg
    )
    # Volatility on the meso scale: classify by NATR(50) — uses same
    # relative-history approach as micro.
    meso_natr = atr_50_rel
    natr_history = _rolling_natr_history(high, low, close, 50)
    vol_label, vol_info = _classify_volatility(
        natr_now=meso_natr, natr_history=natr_history
    )

    sr = _detect_support_resistance(candles, window=min(168, len(candles)))

    return {
        "directionality": direction,
        "volatility": vol_label,
        "indicators": {
            "hurst_100": _safe(hurst_100),
            "ema_slope_50": _safe(ema_slope_50),
            "atr_50_relative": _safe(atr_50_rel),
            "linreg_slope_100": _safe(linreg),
        },
        "vol_history": vol_info,
        "support_resistance": sr,
        "samples": len(close),
    }


def _compute_macro_block(candles: list[dict], cfg: Config) -> dict[str, Any]:
    """1d: vol vs 30d, range, position in range, levels."""
    arrs = ind.to_arrays(candles)
    high, low, close, qvol = arrs["high"], arrs["low"], arrs["close"], arrs["qvol"]
    n = len(close)

    # Today
    today_high = float(high[-1]) if n > 0 else math.nan
    today_low = float(low[-1]) if n > 0 else math.nan
    today_open = float(arrs["open"][-1]) if n > 0 else math.nan
    today_qvol = float(qvol[-1]) if n > 0 else math.nan

    daily_range_pct = (
        (today_high - today_low) / today_open
        if today_open and not math.isnan(today_open) and today_open > 0
        else math.nan
    )

    # 30d benchmarks
    last30 = qvol[-30:] if n >= 30 else qvol
    qvol_mean30 = float(np.mean(last30[last30 > 0])) if np.any(last30 > 0) else math.nan
    volume_today_vs_avg30 = (
        today_qvol / qvol_mean30 if qvol_mean30 and not math.isnan(qvol_mean30) and qvol_mean30 > 0
        else math.nan
    )

    vol_30d = ind.realized_vol_annualized(close[-31:] if n >= 31 else close, 30, PERIODS_PER_YEAR["1d"])

    # Position in 30d range
    if n >= 30:
        win = close[-30:]
        hi30, lo30 = float(np.max(win)), float(np.min(win))
        position_in_range_30d = (
            (close[-1] - lo30) / (hi30 - lo30) if hi30 > lo30 else 0.5
        )
    else:
        hi30 = lo30 = math.nan
        position_in_range_30d = math.nan

    # Levels macro
    def _hi(window):
        sl = close[-window:] if n >= window else close
        return float(np.max(arrs["high"][-len(sl):])) if len(sl) else math.nan

    def _lo(window):
        sl = close[-window:] if n >= window else close
        return float(np.min(arrs["low"][-len(sl):])) if len(sl) else math.nan

    # Direction: use ema_slope and linreg over the macro window for confirmation.
    ema_slope_macro = ind.ema_slope(close, 30, lookback=15) if n >= 50 else math.nan
    linreg_macro = ind.linreg_slope_relative(close[-30:]) if n >= 30 else math.nan
    direction = _classify_directionality(
        hurst=None, adx_val=None, slope=linreg_macro, cfg=cfg
    )
    # Macro: trending only if linreg is decisively non-flat AND consistent
    # with ema_slope_macro. Otherwise random_walk.
    if direction in ("trending_up", "trending_down"):
        if math.isnan(ema_slope_macro) or abs(linreg_macro) < 0.001:
            direction = "random_walk"

    # Volatility: relative to itself across the lookback.
    macro_vol_history = _rolling_natr_history(arrs["high"], arrs["low"], close, 14)
    macro_natr = ind.natr(arrs["high"], arrs["low"], close, 14)
    vol_label, _vol_info = _classify_volatility(
        natr_now=macro_natr, natr_history=macro_vol_history
    )

    return {
        "directionality": direction,
        "volatility": vol_label,
        "indicators": {
            "volume_today_vs_avg30": _safe(volume_today_vs_avg30),
            "daily_range_pct": _safe(daily_range_pct),
            "volatility_30d_annual": _safe(vol_30d),
            "position_in_range_30d": _safe(position_in_range_30d),
            "ema_slope_30": _safe(ema_slope_macro),
            "linreg_slope_30": _safe(linreg_macro),
        },
        "levels_macro": {
            "high_7d": _hi(7),
            "low_7d": _lo(7),
            "high_30d": hi30,
            "low_30d": lo30,
            "high_90d": _hi(90),
            "low_90d": _lo(90),
        },
        "samples": n,
    }


# ---------------------------------------------------------------------------
# S/R detection (in meso)
# ---------------------------------------------------------------------------


def _detect_support_resistance(
    candles: list[dict], window: int = 168, k: int = 3, cluster_tol: float = 0.003
) -> dict[str, Any]:
    """Simple pivot-based S/R, see MARKET_REGIME_SPEC.md §S/R.

    Returns:
        {"support": {price, distance_pct, touches} | None,
         "resistance": {price, distance_pct, touches} | None}

    Returns Nones if not enough data or no valid level.
    """
    if not candles or len(candles) < 2 * k + 1:
        return {"support": None, "resistance": None}

    cs = candles[-window:]
    if len(cs) < 2 * k + 1:
        return {"support": None, "resistance": None}

    pivots: list[tuple[str, float]] = []
    for i in range(k, len(cs) - k):
        ch = float(cs[i].get("high", 0) or 0)
        cl = float(cs[i].get("low", 0) or 0)
        window_slice = cs[i - k:i + k + 1]
        if ch == max(float(c.get("high", 0) or 0) for c in window_slice):
            pivots.append(("R", ch))
        if cl == min(float(c.get("low", 0) or 0) for c in window_slice):
            pivots.append(("S", cl))

    if not pivots:
        return {"support": None, "resistance": None}

    # Cluster pivots within `cluster_tol` of each other
    pivots_sorted = sorted(pivots, key=lambda p: p[1])
    clusters: list[dict[str, Any]] = []
    for kind, price in pivots_sorted:
        if clusters and abs(price - clusters[-1]["price"]) / clusters[-1]["price"] <= cluster_tol:
            cl = clusters[-1]
            cl["count"] += 1
            cl["price"] = (cl["price"] * (cl["count"] - 1) + price) / cl["count"]
            cl["kinds"].add(kind)
        else:
            clusters.append({"price": price, "count": 1, "kinds": {kind}})

    valid = [cl for cl in clusters if cl["count"] >= 2]
    if not valid:
        return {"support": None, "resistance": None}

    current = float(cs[-1].get("close", 0) or 0)
    if current <= 0:
        return {"support": None, "resistance": None}

    below = [cl for cl in valid if cl["price"] < current]
    above = [cl for cl in valid if cl["price"] > current]
    support = max(below, key=lambda cl: cl["price"]) if below else None
    resistance = min(above, key=lambda cl: cl["price"]) if above else None

    def _wrap(cl):
        if cl is None:
            return None
        return {
            "price": round(cl["price"], 6),
            "distance_pct": round((cl["price"] - current) / current, 6),
            "touches": cl["count"],
        }

    return {"support": _wrap(support), "resistance": _wrap(resistance)}


# ---------------------------------------------------------------------------
# Canonical regime
# ---------------------------------------------------------------------------


_OPPOSITE = {"trending_up": "trending_down", "trending_down": "trending_up"}


def _build_canonical(micro: dict, meso: dict, macro: dict) -> dict[str, Any]:
    """Combine the three timeframe classifications into one canonical
    regime + favorability + confidence.

    Logic (see MARKET_REGIME_SPEC.md §Régimen canónico):

    1. Trending-with-pullback exception: macro trends, meso/micro disagree
       (or are inverse) → emit `trending_*_with_pullback`.
    2. Otherwise majority vote across the three timeframes.
    3. Volatility canonical = micro's volatility (operates the moment).
    4. Confidence = number of timeframes that agree on directionality (3 high,
       2 medium, otherwise low). Pullback exception → medium.
    """
    m_dir, e_dir, ma_dir = micro["directionality"], meso["directionality"], macro["directionality"]
    m_vol = micro["volatility"]

    # 1. Pullback exception
    if ma_dir in ("trending_up", "trending_down"):
        opp = _OPPOSITE[ma_dir]
        if e_dir in ("mean_reverting", "random_walk", opp) and \
           m_dir in ("mean_reverting", "random_walk"):
            direction_label = "up" if ma_dir == "trending_up" else "down"
            regime = f"trending_{direction_label}_with_pullback"
            return {
                "regime": _combine(regime, m_vol),
                "directionality": regime,
                "volatility": m_vol,
                "confidence": "med",
                "bias": direction_label,
            }

    # 2. Majority vote
    votes = [m_dir, e_dir, ma_dir]
    counts: dict[str, int] = {}
    for v in votes:
        counts[v] = counts.get(v, 0) + 1
    direction = max(counts.items(), key=lambda kv: kv[1])[0]

    # Confidence
    top = counts[direction]
    confidence = "high" if top == 3 else "med" if top == 2 else "low"

    # Bias (set only for trending)
    bias = None
    if direction == "trending_up":
        bias = "up"
    elif direction == "trending_down":
        bias = "down"

    return {
        "regime": _combine(direction, m_vol),
        "directionality": direction,
        "volatility": m_vol,
        "confidence": confidence,
        "bias": bias,
    }


def _combine(directionality: str, volatility: str) -> str:
    """Canonical regime label = `<directionality>_<volatility_short>`."""
    short = {"low": "low_vol", "moderate": "mod_vol", "high": "high_vol", "unknown": "unknown_vol"}.get(
        volatility, volatility
    )
    # Direction labels with `_with_pullback` are already long enough.
    if directionality.endswith("with_pullback"):
        return f"{directionality}_{short}"
    return f"{directionality}_{short}"


# ---------------------------------------------------------------------------
# Favorability + trigger candidates
# ---------------------------------------------------------------------------


# Map (directionality_root, volatility_label) → (favorability, trigger_candidates)
# Follows MARKET_REGIME_SPEC.md §"Favorabilidad para PMM" matrix.
_FAVOR_MATRIX: dict[tuple[str, str], tuple[str, list[str]]] = {
    ("mean_reverting", "low"):      ("suboptimal", ["spreads"]),
    ("mean_reverting", "moderate"): ("optimal",    []),
    ("mean_reverting", "high"):     ("suboptimal", ["take_profit", "spreads"]),
    ("random_walk", "low"):         ("suboptimal", ["spreads"]),
    ("random_walk", "moderate"):    ("optimal",    []),
    ("random_walk", "high"):        ("adverse",    ["max_active_executors_by_level"]),
    ("trending_up", "low"):         ("adverse",    ["target_base_pct", "portfolio_allocation"]),
    ("trending_up", "moderate"):    ("adverse",    ["target_base_pct", "portfolio_allocation"]),
    ("trending_up", "high"):        ("adverse",    ["target_base_pct", "portfolio_allocation"]),
    ("trending_down", "low"):       ("adverse",    ["target_base_pct", "portfolio_allocation"]),
    ("trending_down", "moderate"):  ("adverse",    ["target_base_pct", "portfolio_allocation"]),
    ("trending_down", "high"):      ("adverse",    ["target_base_pct", "portfolio_allocation"]),
}


def _favorability(canon: dict[str, Any]) -> tuple[str, list[str]]:
    """Return (favorability, trigger_candidates) for the canonical regime."""
    direction_full = canon["directionality"]
    vol = canon["volatility"] if canon["volatility"] != "unknown" else "moderate"

    if direction_full.endswith("with_pullback"):
        # Trending-with-pullback: suboptimal opportunity with bias.
        if vol == "high":
            return "suboptimal", ["buy_amounts_pct", "sell_amounts_pct"]
        return "suboptimal", ["buy_amounts_pct", "sell_amounts_pct"]

    fav, triggers = _FAVOR_MATRIX.get((direction_full, vol), ("suboptimal", []))
    return fav, list(triggers)


# ---------------------------------------------------------------------------
# Persistence (stub for MVP — full implementation reads regime_history.jsonl)
# ---------------------------------------------------------------------------


def _compute_persistence_minutes(canonical_regime: str) -> int | None:
    """For MVP, returns None — the regime_history persistence file is part
    of the agent's state directory, not the routine's. The agent will
    populate `persistence_minutes` when it reads the routine output.
    """
    return None


# ---------------------------------------------------------------------------
# Compose payload
# ---------------------------------------------------------------------------


def _safe(x: float) -> float | None:
    """NaN → None so JSON serialization works."""
    if isinstance(x, (int, float)) and not math.isnan(float(x)):
        return float(x)
    return None


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _compose_payload(
    cfg: Config,
    micro: dict, meso: dict, macro: dict,
    *,
    pair: str | None = None,
) -> dict[str, Any]:
    """Build the per-pair payload.

    ``pair`` overrides the (legacy) ``cfg.trading_pair`` field. The
    multi-pair entry point passes ``pair`` explicitly; the
    back-compat path falls back to ``cfg.trading_pair`` so old tests
    keep working with `Config(trading_pair="BTC-USDT")`.
    """
    canon = _build_canonical(micro, meso, macro)
    fav, triggers = _favorability(canon)
    persistence = _compute_persistence_minutes(canon["regime"])

    history_available = (
        micro["samples"] >= 50
        and meso["samples"] >= 100
        and macro["samples"] >= 30
    )

    chosen_pair = pair if pair is not None else (cfg.trading_pair or "")

    return {
        "ts": _utc_iso(),
        "trading_pair": chosen_pair,
        "connector": cfg.connector_name,
        "summary": {
            "canonical_regime": canon["regime"],
            "directionality": canon["directionality"],
            "volatility": canon["volatility"],
            "favorability": fav,
            "confidence": canon["confidence"],
            "persistence_minutes": persistence,
            "bias": canon["bias"],
            "trigger_candidates": triggers,
        },
        "micro_5m": micro,
        "meso_1h": meso,
        "macro_1d": macro,
        "history_required": {
            "available": history_available,
            "samples_micro": micro["samples"],
            "samples_meso": meso["samples"],
            "samples_macro": macro["samples"],
        },
    }


# ---------------------------------------------------------------------------
# Agent view
# ---------------------------------------------------------------------------


def _build_agent_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Minimal dict the adaptive agent's LLM reads.

    Contract is the one documented in AGENT_VS_USER_VIEW.md §market_regime.
    """
    s = payload["summary"]
    meso = payload.get("meso_1h", {}) or {}
    sr = meso.get("support_resistance") or {}
    sup = sr.get("support") or {}
    res = sr.get("resistance") or {}

    return {
        "ts": payload["ts"],
        "pair": payload["trading_pair"],
        "connector": payload["connector"],
        "regime": s["canonical_regime"],
        "favorability": s["favorability"],
        "confidence": s["confidence"],
        "persistence_minutes": s["persistence_minutes"],
        "bias": s["bias"],
        "trigger_candidates": list(s.get("trigger_candidates") or []),
        "by_timeframe": {
            "micro_5m": {
                "dir": payload["micro_5m"]["directionality"],
                "vol": payload["micro_5m"]["volatility"],
            },
            "meso_1h": {
                "dir": payload["meso_1h"]["directionality"],
                "vol": payload["meso_1h"]["volatility"],
            },
            "macro_1d": {
                "dir": payload["macro_1d"]["directionality"],
                "vol": payload["macro_1d"]["volatility"],
            },
        },
        "key_levels": {
            "support_distance_pct": sup.get("distance_pct"),
            "resistance_distance_pct": res.get("distance_pct"),
        },
        "history_available": payload["history_required"]["available"],
    }


# ---------------------------------------------------------------------------
# User view — compact summary, KPIs, table, narrative, glossary
# ---------------------------------------------------------------------------


_FAV_EMOJI = {"optimal": "🟢", "suboptimal": "🟡", "adverse": "🔴"}


def _render_compact_summary(payload: dict[str, Any]) -> str:
    """One-line ASCII summary for Telegram `text` (≤ 220 chars, no backticks)."""
    s = payload["summary"]
    emoji = _FAV_EMOJI.get(s["favorability"], "?")
    persist = (
        f"{s['persistence_minutes']}m" if s.get("persistence_minutes") is not None else "n/a"
    )
    return (
        f"{payload['trading_pair']} · {s['canonical_regime']} · "
        f"{emoji} {s['favorability']} · conf={s['confidence']} · {persist}"
    )


def _build_kpi_sections(payload: dict[str, Any]) -> list[dict[str, Any]]:
    s = payload["summary"]
    fav = s["favorability"]
    fav_trend = {"optimal": "up", "suboptimal": "neutral", "adverse": "down"}.get(fav, "neutral")

    persistence = s.get("persistence_minutes")
    persistence_value = f"{persistence}m" if persistence is not None else "n/a"

    return [
        {
            "type": "kpi",
            "label": "Regime",
            "value": s["canonical_regime"],
            "delta": s["directionality"],
            "trend": "neutral",
        },
        {
            "type": "kpi",
            "label": "Favorability",
            "value": f"{_FAV_EMOJI.get(fav, '?')} {fav}",
            "delta": None,
            "trend": fav_trend,
        },
        {
            "type": "kpi",
            "label": "Confidence",
            "value": s["confidence"],
            "delta": None,
            "trend": "neutral",
        },
        {
            "type": "kpi",
            "label": "Persistence",
            "value": persistence_value,
            "delta": "since transition" if persistence is not None else "(agent tracks)",
            "trend": "neutral",
        },
    ]


def _build_timeframe_table(payload: dict[str, Any]) -> tuple[list[str], list[dict]]:
    columns = ["timeframe", "directionality", "volatility", "key_indicator", "value", "samples"]

    def _row(label: str, blk: dict, key_name: str) -> dict:
        ind_val = blk.get("indicators", {}).get(key_name)
        if ind_val is None:
            ind_str = "n/a"
        elif isinstance(ind_val, (int, float)):
            ind_str = f"{ind_val:.4f}" if abs(ind_val) < 10 else f"{ind_val:.2f}"
        else:
            ind_str = str(ind_val)
        return {
            "timeframe": label,
            "directionality": blk.get("directionality", "?"),
            "volatility": blk.get("volatility", "?"),
            "key_indicator": key_name,
            "value": ind_str,
            "samples": blk.get("samples", 0),
        }

    rows = [
        _row("5m",  payload.get("micro_5m", {}), "natr_14"),
        _row("1h",  payload.get("meso_1h", {}),  "hurst_100"),
        _row("1d",  payload.get("macro_1d", {}), "position_in_range_30d"),
    ]
    return columns, rows


def _build_narrative(payload: dict[str, Any]) -> str:
    """2-5 deterministic Spanish sentences interpreting the snapshot."""
    s = payload["summary"]
    fav, dirn, conf = s["favorability"], s["directionality"], s["confidence"]
    pair = payload["trading_pair"]
    emoji = _FAV_EMOJI.get(fav, "")

    sentences: list[str] = []

    if dirn.endswith("with_pullback"):
        base = "trending_up" if "up" in dirn else "trending_down"
        sentences.append(
            f"{emoji} {pair} muestra **{base.replace('_', ' ')}** macro "
            f"con pullback en timeframes cortos — el agente lo trata como "
            f"{fav} con sesgo {s['bias']}."
        )
        sentences.append(
            "Operar con bias hacia el macro: ajustar amounts a favor de la "
            "dirección estructural para no quedar atrapado cuando el pullback termine."
        )
    elif dirn.startswith("trending"):
        sentences.append(
            f"{emoji} {pair} en **{dirn.replace('_', ' ')}** ({s['volatility']} vol). "
            f"PMM en {fav}: inventory expuesto unilateralmente, fills asimétricos."
        )
    elif dirn == "mean_reverting":
        if fav == "optimal":
            sentences.append(
                f"{emoji} {pair} en mean-reverting con volatilidad moderada — "
                "zona ideal para PMM, alta rotación con exposición direccional baja."
            )
        else:
            sentences.append(
                f"{emoji} {pair} mean-reverting pero con vol {s['volatility']} → "
                f"{fav}: el spread captura puede no compensar slippage / ociosidad."
            )
    else:  # random_walk
        if fav == "optimal":
            sentences.append(
                f"{emoji} {pair} en random walk con volatilidad moderada — "
                "condiciones cómodas para PMM."
            )
        else:
            sentences.append(
                f"{emoji} {pair} en random walk con vol {s['volatility']} → {fav}."
            )

    if conf == "low":
        sentences.append(
            "_Confidence baja_: los tres timeframes dan señales distintas. "
            "Tratar la clasificación canónica con cautela."
        )

    if not payload["history_required"]["available"]:
        sentences.append(
            "_Histórico insuficiente_ — algunas métricas pueden no ser confiables; "
            "la confianza se degrada automáticamente."
        )

    triggers = s.get("trigger_candidates") or []
    if triggers:
        sentences.append(
            f"Dimensiones candidatas para acción (a priori): **{', '.join(triggers)}**."
        )

    return " ".join(sentences)


_GLOSSARY_TERMS = {
    "regimen_canonico": (
        "**Régimen canónico** — composición de los 3 timeframes en una etiqueta "
        "única (`directionality_volatility_short`). Excepción: trending con pullback."
    ),
    "favorabilidad": (
        "**Favorabilidad** — qué tan amigable es el régimen para un PMM (proveedor "
        "de liquidez): 🟢 optimal (zona ideal), 🟡 suboptimal (ocioso o con bias), "
        "🔴 adverse (inventory atrapado o riesgo alto)."
    ),
    "natr": (
        "**NATR** — ATR normalizado por close. Mide volatilidad relativa al precio. "
        "Útil para calibrar spreads y take_profit."
    ),
    "hurst": (
        "**Hurst exponent** — sobre log returns. H>0.5 momentum; H<0.5 mean-reversion; "
        "~0.5 random walk. Detecta estructura del par sin necesidad de tendencia visible."
    ),
    "persistence": (
        "**Persistence** — minutos desde la última transición del régimen canónico. "
        "Lo trackea el agente entre ticks (no la routine)."
    ),
    "trending_with_pullback": (
        "**Trending con pullback** — macro va en una dirección pero los timeframes "
        "cortos están laterales o contrarios. PMM en lateral parece cómodo pero el "
        "pullback va a terminar y el precio salta — uno de los escenarios donde el "
        "agente agrega valor real al humano."
    ),
    "support_resistance": (
        "**Soporte/Resistencia** — pivots detectados en 1h con clustering. `distance_pct` "
        "negativo = soporte abajo; positivo = resistencia arriba. Solo niveles tocados "
        "≥2 veces se reportan."
    ),
    "random_walk": (
        "**Random walk** — direccionalidad sin estructura clara. Returns iid: no "
        "momentum ni reversion detectable."
    ),
    "position_in_range_30d": (
        "**Position in range 30d** — `(close - low_30d) / (high_30d - low_30d)`. "
        "Cerca de 1 = techo del rango, cerca de 0 = piso. Contexto macro."
    ),
}


def _select_glossary_terms(payload: dict[str, Any]) -> list[str]:
    """Pick which glossary terms to render based on what the snapshot
    actually mentions. Always include the 5 base terms."""
    base = ["regimen_canonico", "favorabilidad", "natr", "hurst", "persistence"]
    extras: list[str] = []

    dirn = payload["summary"]["directionality"]
    if dirn.endswith("with_pullback"):
        extras.append("trending_with_pullback")

    sr = (payload.get("meso_1h", {}) or {}).get("support_resistance") or {}
    if (sr.get("support") or {}).get("distance_pct") is not None or \
       (sr.get("resistance") or {}).get("distance_pct") is not None:
        # Show only if at least one is within 1% of price
        s_d = abs((sr.get("support") or {}).get("distance_pct") or 1.0)
        r_d = abs((sr.get("resistance") or {}).get("distance_pct") or 1.0)
        if s_d < 0.01 or r_d < 0.01:
            extras.append("support_resistance")

    if "random_walk" in (
        payload.get("micro_5m", {}).get("directionality", ""),
        payload.get("meso_1h", {}).get("directionality", ""),
        payload.get("macro_1d", {}).get("directionality", ""),
    ):
        extras.append("random_walk")

    pos = payload.get("macro_1d", {}).get("indicators", {}).get("position_in_range_30d")
    if pos is not None and (pos > 0.85 or pos < 0.15):
        extras.append("position_in_range_30d")

    return base + extras


def _build_glossary_markdown(payload: dict[str, Any]) -> str:
    selected = _select_glossary_terms(payload)
    lines = ["## Glosario"]
    for key in selected:
        if key in _GLOSSARY_TERMS:
            lines.append(f"- {_GLOSSARY_TERMS[key]}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Report (HTML, persisted) — L3
# ---------------------------------------------------------------------------


def _save_report(payload: dict[str, Any]) -> str | None:
    """Persist a full HTML report — what shows up in the web UI's
    routines page (LEARNINGS.md L3: the new screen reads from
    ReportBuilder, not from RoutineResult.sections)."""
    try:
        from condor.reports import ReportBuilder
    except Exception as e:
        logger.warning("ReportBuilder import failed: %s", e)
        return None

    s = payload["summary"]
    fav = s["favorability"]
    fav_trend = {"optimal": "up", "suboptimal": "neutral", "adverse": "down"}.get(fav, "neutral")
    pair = payload["trading_pair"]

    builder = ReportBuilder(f"Market Regime — {pair} — {payload['ts']}")
    builder.source("routine", "market_regime").tags(["adaptive_framework", "regime", pair])

    persistence = s.get("persistence_minutes")
    persistence_value = f"{persistence}m" if persistence is not None else "n/a"

    builder.kpi("Regime", s["canonical_regime"], delta=s["directionality"], trend="neutral")
    builder.kpi(
        "Favorability",
        f"{_FAV_EMOJI.get(fav, '?')} {fav}",
        delta=None,
        trend=fav_trend,
    )
    builder.kpi("Confidence", s["confidence"], delta=None, trend="neutral")
    builder.kpi(
        "Persistence",
        persistence_value,
        delta="since transition" if persistence is not None else "(agent tracks)",
        trend="neutral",
    )

    # Narrative
    builder.markdown("## Resumen\n\n" + _build_narrative(payload))

    # Timeframe table
    columns, rows = _build_timeframe_table(payload)
    if rows:
        builder.markdown("## Por timeframe")
        builder.table(rows, columns=columns)

    # S/R block (if any)
    sr = (payload.get("meso_1h", {}) or {}).get("support_resistance") or {}
    sup, res = sr.get("support"), sr.get("resistance")
    if sup or res:
        lines = ["## Niveles cercanos (1h)"]
        if sup:
            lines.append(
                f"- **Soporte**: {sup['price']:.4f} "
                f"({sup['distance_pct']*100:.2f}%, {sup['touches']} toques)"
            )
        if res:
            lines.append(
                f"- **Resistencia**: {res['price']:.4f} "
                f"({res['distance_pct']*100:.2f}%, {res['touches']} toques)"
            )
        builder.markdown("\n".join(lines))

    # Macro levels (always shown — context)
    macro = payload.get("macro_1d", {}).get("levels_macro", {})
    if macro:
        lines = ["## Niveles macro"]
        for label, key in (
            ("Máx 7d", "high_7d"), ("Mín 7d", "low_7d"),
            ("Máx 30d", "high_30d"), ("Mín 30d", "low_30d"),
            ("Máx 90d", "high_90d"), ("Mín 90d", "low_90d"),
        ):
            v = macro.get(key)
            if v is not None and not (isinstance(v, float) and math.isnan(v)):
                lines.append(f"- **{label}**: {v:.4f}")
        if len(lines) > 1:
            builder.markdown("\n".join(lines))

    # Glossary
    builder.markdown(_build_glossary_markdown(payload))

    return builder.save()


# ---------------------------------------------------------------------------
# Multi-pair: autodetect, compose, render
# ---------------------------------------------------------------------------


async def _autodetect_pairs(client, connector_filter: str) -> list[str]:
    """Discover trading pairs traded by active controllers on the server.

    Returns a sorted, deduplicated list of pairs. Filters to controllers
    whose ``connector_name`` matches ``connector_filter`` (spot only —
    pmm_mister does not run on perps).

    Returns an empty list if no controllers are active or the API errors
    out. The caller decides how to handle that (the entrypoint emits a
    "No pairs" RoutineResult).
    """
    try:
        bots_raw = await client.bot_orchestration.get_active_bots_status()
    except Exception as e:
        logger.warning("market_regime: get_active_bots_status failed: %s", e)
        return []
    bots_data = bots_raw.get("data", {}) if isinstance(bots_raw, dict) else {}

    pairs: set[str] = set()
    for bot_name in bots_data.keys():
        try:
            cfgs = await client.controllers.get_bot_controller_configs(bot_name)
        except Exception as e:
            logger.warning("market_regime: configs for %s failed: %s", bot_name, e)
            continue
        for cfg in cfgs or []:
            if not isinstance(cfg, dict):
                continue
            if cfg.get("connector_name") != connector_filter:
                continue
            pair = cfg.get("trading_pair")
            if pair:
                pairs.add(pair)
    return sorted(pairs)


async def _process_one_pair(
    client, cfg: Config, pair: str
) -> dict[str, Any]:
    """Fetch candles + compute payload for one pair. Empty dict on hard failure.

    Concurrency at the timeframe level (3 fetches in parallel per pair);
    the caller can fan-out at the pair level if it wants extra parallelism.
    """
    micro_raw, meso_raw, macro_raw = await asyncio.gather(
        _fetch_candles(client, cfg.connector_name, pair,
                       cfg.micro_interval, cfg.micro_lookback_candles),
        _fetch_candles(client, cfg.connector_name, pair,
                       cfg.meso_interval, cfg.meso_lookback_candles),
        _fetch_candles(client, cfg.connector_name, pair,
                       cfg.macro_interval, cfg.macro_lookback_candles),
    )
    if not micro_raw and not meso_raw and not macro_raw:
        return {}
    micro = _compute_micro_block(micro_raw, cfg)
    meso = _compute_meso_block(meso_raw, cfg)
    macro = _compute_macro_block(macro_raw, cfg)
    return _compose_payload(cfg, micro, meso, macro, pair=pair)


def _compose_aggregate(payloads_by_pair: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Aggregate view across pairs: counts, worst, per-pair summaries.

    Filtered to non-empty per-pair payloads (failed pairs are skipped).
    """
    counts = {"optimal": 0, "suboptimal": 0, "adverse": 0}
    by_pair = {}
    worst_rank = {"adverse": 0, "suboptimal": 1, "optimal": 2}
    worst_pair = None
    worst_fav = None

    for pair, p in payloads_by_pair.items():
        if not p:
            continue
        s = p.get("summary", {})
        fav = s.get("favorability", "suboptimal")
        if fav in counts:
            counts[fav] += 1
        by_pair[pair] = {
            "regime": s.get("canonical_regime"),
            "directionality": s.get("directionality"),
            "volatility": s.get("volatility"),
            "favorability": fav,
            "confidence": s.get("confidence"),
            "bias": s.get("bias"),
        }
        if worst_fav is None or worst_rank.get(fav, 99) < worst_rank.get(worst_fav, 99):
            worst_fav, worst_pair = fav, pair

    return {
        "ts": _utc_iso(),
        "pairs": sorted(by_pair.keys()),
        "by_pair": by_pair,
        "counts": counts,
        "worst": (
            {"pair": worst_pair, "favorability": worst_fav}
            if worst_pair else None
        ),
    }


def _aggregate_text(aggregate: dict[str, Any], payloads_by_pair: dict) -> str:
    """One-line ASCII summary across all pairs.

    Examples:
      "5 pairs · 1🟢 2🟡 2🔴 · worst: SOL-USDT adverse"
      "BTC-USDT · trending_down_low_vol · 🔴 adverse · conf=low"  (single pair)
    """
    pairs = aggregate["pairs"]
    if not pairs:
        return "market_regime: no pairs."
    if len(pairs) == 1:
        # Fall back to per-pair text for the single-pair case.
        only = payloads_by_pair.get(pairs[0])
        if only:
            return _render_compact_summary(only)
    c = aggregate["counts"]
    worst = aggregate.get("worst") or {}
    worst_str = (
        f" · worst: {worst.get('pair')} {worst.get('favorability')}"
        if worst.get("pair") else ""
    )
    return (
        f"{len(pairs)} pairs · "
        f"{c['optimal']}🟢 {c['suboptimal']}🟡 {c['adverse']}🔴"
        f"{worst_str}"
    )


def _aggregate_kpi_sections(aggregate: dict[str, Any]) -> list[dict[str, Any]]:
    pairs = aggregate["pairs"]
    total = len(pairs)
    c = aggregate["counts"]

    def _delta(count: int) -> str | None:
        if total == 0:
            return None
        return f"{count * 100 // total}% of total"

    return [
        {"type": "kpi", "label": "Pairs",      "value": str(total),       "delta": None, "trend": "neutral"},
        {"type": "kpi", "label": "Optimal",    "value": f"🟢 {c['optimal']}",    "delta": _delta(c['optimal']),    "trend": "up"     if c['optimal']    else "neutral"},
        {"type": "kpi", "label": "Suboptimal", "value": f"🟡 {c['suboptimal']}", "delta": _delta(c['suboptimal']), "trend": "neutral"},
        {"type": "kpi", "label": "Adverse",    "value": f"🔴 {c['adverse']}",    "delta": _delta(c['adverse']),    "trend": "down"   if c['adverse']    else "neutral"},
    ]


def _aggregate_table(payloads_by_pair: dict[str, dict[str, Any]]) -> tuple[list[str], list[dict[str, Any]]]:
    """Summary matrix: 1 row per pair. Sorted adverse → suboptimal → optimal."""
    columns = ["pair", "regime", "favorability", "confidence", "bias", "support_dist", "resistance_dist"]
    rank = {"adverse": 0, "suboptimal": 1, "optimal": 2}

    rows = []
    for pair, p in payloads_by_pair.items():
        if not p:
            continue
        s = p.get("summary", {})
        meso = p.get("meso_1h", {}) or {}
        sr = meso.get("support_resistance") or {}
        sup = sr.get("support") or {}
        res = sr.get("resistance") or {}

        def _pct(v):
            return f"{v * 100:.2f}%" if isinstance(v, (int, float)) else "—"

        rows.append({
            "pair": pair,
            "regime": s.get("canonical_regime", "?"),
            "favorability": f"{_FAV_EMOJI.get(s.get('favorability', ''), '')} {s.get('favorability', '?')}".strip(),
            "confidence": s.get("confidence", "?"),
            "bias": s.get("bias") or "—",
            "support_dist": _pct(sup.get("distance_pct")),
            "resistance_dist": _pct(res.get("distance_pct")),
        })

    rows.sort(key=lambda r: (rank.get(r["favorability"].split()[-1] if " " in r["favorability"] else r["favorability"], 99), r["pair"]))
    return columns, rows


def _build_agent_summary(aggregate: dict[str, Any]) -> dict[str, Any]:
    """Minimal cross-pair view for the agent — sits next to the per-pair
    sections. Lets the LLM see the overall landscape without having to
    re-aggregate the per-pair payloads itself."""
    return {
        "ts": aggregate["ts"],
        "pairs": aggregate["pairs"],
        "by_pair": aggregate["by_pair"],
        "counts": aggregate["counts"],
        "worst": aggregate["worst"],
    }


def _save_multi_pair_report(
    aggregate: dict[str, Any],
    payloads_by_pair: dict[str, dict[str, Any]],
) -> str | None:
    """Single HTML report with summary matrix on top, sub-sections per pair,
    and one glossary at the very bottom. No JS tabs (ReportBuilder does
    not support them) — markdown anchors give the same UX.
    """
    try:
        from condor.reports import ReportBuilder
    except Exception as e:
        logger.warning("ReportBuilder import failed: %s", e)
        return None

    pairs = aggregate["pairs"]
    if not pairs:
        return None

    title_pairs = ", ".join(pairs[:3]) + (f" +{len(pairs) - 3}" if len(pairs) > 3 else "")
    builder = ReportBuilder(f"Market Regime — {title_pairs} — {aggregate['ts']}")
    builder.source("routine", "market_regime").tags(["adaptive_framework", "regime", "multi_pair"])

    # 4 aggregate KPIs.
    for k in _aggregate_kpi_sections(aggregate):
        builder.kpi(k["label"], k["value"], delta=k.get("delta"), trend=k.get("trend", "neutral"))

    # Resumen narrativo agregado (1-2 frases).
    worst = aggregate.get("worst") or {}
    if worst.get("pair"):
        builder.markdown(
            "## Resumen\n\n"
            f"De **{len(pairs)} pares** procesados, "
            f"{aggregate['counts']['optimal']} están en zona óptima, "
            f"{aggregate['counts']['suboptimal']} en zona subóptima y "
            f"{aggregate['counts']['adverse']} en zona adversa para PMM. "
            f"El más crítico es **{worst['pair']}** ({worst['favorability']})."
        )

    # Tabla resumen.
    cols, rows = _aggregate_table(payloads_by_pair)
    if rows:
        builder.markdown("## Por par")
        builder.table(rows, columns=cols)

    # TOC con anchors (markdown anchors usan slug del header).
    toc = " · ".join(f"[{p}](#{p.lower().replace('-', '-')})" for p in pairs)
    builder.markdown(f"**Ir a:** {toc}")

    # Sub-sección por par.
    for pair in pairs:
        p = payloads_by_pair.get(pair)
        if not p:
            continue
        s = p["summary"]
        fav = s["favorability"]
        emoji = _FAV_EMOJI.get(fav, "")
        persistence = s.get("persistence_minutes")
        persistence_value = f"{persistence}m" if persistence is not None else "n/a"

        body = [
            f"## {pair}",
            "",
            f"**Régimen**: `{s['canonical_regime']}` · **Favorability**: {emoji} {fav} · "
            f"**Confidence**: {s['confidence']} · **Persistence**: {persistence_value}",
            "",
            _build_narrative(p),
        ]
        builder.markdown("\n".join(body))

        cols_tf, rows_tf = _build_timeframe_table(p)
        if rows_tf:
            builder.table(rows_tf, columns=cols_tf)

        # S/R block (if any)
        sr = (p.get("meso_1h", {}) or {}).get("support_resistance") or {}
        sup, res = sr.get("support"), sr.get("resistance")
        if sup or res:
            lines = ["### Niveles cercanos (1h)"]
            if sup:
                lines.append(
                    f"- **Soporte**: {sup['price']:.4f} "
                    f"({sup['distance_pct']*100:.2f}%, {sup['touches']} toques)"
                )
            if res:
                lines.append(
                    f"- **Resistencia**: {res['price']:.4f} "
                    f"({res['distance_pct']*100:.2f}%, {res['touches']} toques)"
                )
            builder.markdown("\n".join(lines))

        # Macro levels
        macro = p.get("macro_1d", {}).get("levels_macro", {})
        if macro:
            lines = ["### Niveles macro"]
            for label, key in (
                ("Máx 7d", "high_7d"), ("Mín 7d", "low_7d"),
                ("Máx 30d", "high_30d"), ("Mín 30d", "low_30d"),
                ("Máx 90d", "high_90d"), ("Mín 90d", "low_90d"),
            ):
                v = macro.get(key)
                if v is not None and not (isinstance(v, float) and math.isnan(v)):
                    lines.append(f"- **{label}**: {v:.4f}")
            if len(lines) > 1:
                builder.markdown("\n".join(lines))

    # Glosario único al final — usa el primer payload para selección
    # contextual; los términos base están siempre.
    first_payload = next((p for p in payloads_by_pair.values() if p), None)
    if first_payload is not None:
        builder.markdown(_build_glossary_markdown(first_payload))

    return builder.save()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


async def run(config: Config, context: ContextTypes.DEFAULT_TYPE) -> RoutineResult:
    chat_id = context._chat_id if hasattr(context, "_chat_id") else None
    client = await get_client(chat_id or 0, context=context)
    if not client:
        return RoutineResult(text="Could not connect to API server.")

    # Resolve the pair list.
    pairs: list[str] = list(config.trading_pairs or [])
    if not pairs and config.trading_pair:
        pairs = [config.trading_pair]  # legacy single-pair fallback
    if not pairs:
        pairs = await _autodetect_pairs(client, config.connector_name)
    if not pairs:
        return RoutineResult(
            text=(
                f"No pairs to classify (no active controllers on "
                f"{config.connector_name}, and no `trading_pairs` supplied)."
            )
        )

    # Fan-out: process all pairs in parallel.
    results = await asyncio.gather(
        *(_process_one_pair(client, config, p) for p in pairs),
        return_exceptions=True,
    )
    payloads_by_pair: dict[str, dict[str, Any]] = {}
    for pair, res in zip(pairs, results):
        if isinstance(res, Exception):
            logger.error("market_regime: pair %s failed: %s", pair, res)
            continue
        if res:
            payloads_by_pair[pair] = res

    if not payloads_by_pair:
        return RoutineResult(
            text=f"No candle data for any of {len(pairs)} pairs on {config.connector_name}."
        )

    aggregate = _compose_aggregate(payloads_by_pair)

    # Persist a single multi-pair HTML report — L3.
    try:
        _save_multi_pair_report(aggregate, payloads_by_pair)
    except Exception as e:
        logger.error("market_regime: failed to save multi-pair report: %s", e)

    text = _aggregate_text(aggregate, payloads_by_pair)

    # Sections:
    # - Aggregate KPIs (4 cards: pairs / optimal / suboptimal / adverse).
    # - "payload": full multi-pair payload (archival / dashboard).
    # - "agent:summary": cross-pair view for the agent.
    # - "agent:<PAIR>" (× N): per-pair agent view, what the LLM reads
    #   when reasoning about that particular controller.
    kpi_sections = _aggregate_kpi_sections(aggregate)
    full_payload = {
        "ts": aggregate["ts"],
        "connector": config.connector_name,
        "pairs": aggregate["pairs"],
        "payloads": payloads_by_pair,
        "aggregate": aggregate,
    }
    data_sections: list[dict[str, Any]] = [
        {"type": "data", "title": "payload", "data": full_payload},
        {"type": "data", "title": "agent:summary", "data": _build_agent_summary(aggregate)},
    ]
    for pair in aggregate["pairs"]:
        data_sections.append({
            "type": "data",
            "title": f"agent:{pair}",
            "data": _build_agent_payload(payloads_by_pair[pair]),
        })

    table_columns, table_rows = _aggregate_table(payloads_by_pair)

    return RoutineResult(
        text=text,
        sections=kpi_sections + data_sections,
        table_data=table_rows,
        table_columns=table_columns,
    )
