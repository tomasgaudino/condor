"""Target Scenario Table v4 — velas S/R + grilla de escenarios NAV."""

CATEGORY = "Analysis"

import logging
import math

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field
from telegram.ext import ContextTypes

from config_manager import get_client
from routines.base import RoutineResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class Config(BaseModel):
    """Tabla de escenarios NAV por target de rebalanceo y precio futuro, con S/R resaltados."""
    trading_pair: str = Field(default="BTC-BRL", description="Par de trading")
    connector_name: str = Field(default="binance", description="Exchange")
    target_step: int = Field(default=10, description="Step de targets % (10, 25 o 50)")
    intermediate_points: int = Field(default=2, description="Puntos interpolados entre S/R consecutivos")
    sr_days: int = Field(default=365, description="Lookback días para S/R")
    sr_n_levels: int = Field(default=5, description="Niveles S/R por lado")
    numerario: str = Field(default="BRL", description="Numerario principal para la tabla (BRL o USDT)")
    highlight_threshold: float = Field(default=0.30, description="Umbral ΔNAV para marcar celda")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fmt_compact(value: float) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.3f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return f"{value:.0f}"


def fmt_brl(value: float) -> str:
    return f"{value:,.0f}"


def fmt_pct(value: float) -> str:
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.1%}"


# ---------------------------------------------------------------------------
# S/R fetching — returns (sr_levels, df, now)
# ---------------------------------------------------------------------------

async def fetch_sr_and_candles(client, connector: str, trading_pair: str, days: int, n_levels: int):
    """Returns (sr_levels, df) — df is the full candle DataFrame for charting."""
    import math as _math

    result = await client.market_data.get_candles_last_days(connector, trading_pair, days=days, interval="1d")
    records = result if isinstance(result, list) else result.get("data", result.get("candles", [])) if isinstance(result, dict) else []
    if not records:
        return [], None

    df = pd.DataFrame(records)
    df.columns = [c.lower() for c in df.columns]
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    ts = df["timestamp"]
    unit = "s" if ts.iloc[0] < 1e12 else "ms"
    df["timestamp"] = pd.to_datetime(ts, unit=unit, utc=True)
    df = df.sort_values("timestamp").reset_index(drop=True)

    if len(df) < 30:
        return [], df
