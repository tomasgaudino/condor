"""Technical indicators for the adaptive strategy framework.

Pure-numpy implementations of the indicators that `market_regime` (and
future routines) need. No external TA libraries — the codebase pattern
(`routines/market_scanner.py`) is numpy-by-hand.

Design choices:

- All functions take **numpy arrays** of floats (not pandas, not lists).
  Caller converts.
- All functions return **plain floats** or numpy arrays — never NaN
  unless the input is genuinely undefined (insufficient data). Callers
  check `math.isnan(...)` or use the `safe_*` wrappers.
- Period N requires at least N+1 samples for derivative-style
  indicators (TR, slopes) and N samples for stat-style (mean, std).
  Functions return NaN when input is too short — never raise.
- No state, no caching, no IO. Re-entrant. Cheap to test.

Indicator inventory (see MARKET_REGIME_SPEC.md for what each one is
used for):

- `true_range`, `atr`, `natr`
- `bollinger_width`
- `ema`, `ema_slope`
- `adx`
- `realized_vol_annualized`
- `hurst_rs` (rescaled-range method)
- `linreg_slope`

Conventions:

- `close`, `high`, `low` are 1D arrays of equal length, oldest first.
- "period" or "window" is always an int > 0.
- Returns are decimal fractions (0.0142 for 1.42%) unless noted.
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np
from scipy import stats


# ---------------------------------------------------------------------------
# True Range family
# ---------------------------------------------------------------------------


def true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    """Wilder's True Range — array of length len(high)-1.

    TR[i] = max(
        high[i+1] - low[i+1],
        abs(high[i+1] - close[i]),
        abs(low[i+1] - close[i]),
    )

    Returns empty array if inputs have fewer than 2 points.
    """
    if len(high) < 2 or len(high) != len(low) or len(high) != len(close):
        return np.array([], dtype=float)
    h1, l1, c0 = high[1:], low[1:], close[:-1]
    return np.maximum.reduce([h1 - l1, np.abs(h1 - c0), np.abs(l1 - c0)])


def atr(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14
) -> float:
    """Average True Range — simple mean of the last `period` TR values.

    Returns NaN if there is not enough data (need at least period+1
    candles total). Uses simple mean, not Wilder's smoothing — for the
    multi-timeframe regime classifier this is enough and easier to
    reason about. If Wilder smoothing is needed later, add `atr_wilder`.
    """
    tr = true_range(high, low, close)
    if len(tr) < period:
        return math.nan
    return float(np.mean(tr[-period:]))


def natr(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14
) -> float:
    """Normalized ATR — ATR(period) / current close.

    Decimal fraction (0.0142 = 1.42%). NaN if ATR is NaN or close is 0.
    """
    a = atr(high, low, close, period)
    if math.isnan(a) or len(close) == 0 or close[-1] == 0:
        return math.nan
    return float(a / close[-1])


# ---------------------------------------------------------------------------
# Bollinger
# ---------------------------------------------------------------------------


def bollinger_width(close: np.ndarray, period: int = 20, num_std: float = 2.0) -> float:
    """(upper - lower) / SMA = 2 * num_std * std / mean.

    Width is dimensionless, normalized by the SMA so it's comparable
    across price scales. NaN if insufficient data.
    """
    if len(close) < period:
        return math.nan
    window = close[-period:]
    mean = float(np.mean(window))
    if mean == 0:
        return math.nan
    std = float(np.std(window))
    return (2 * num_std * std) / mean


# ---------------------------------------------------------------------------
# EMA + slope
# ---------------------------------------------------------------------------


def ema(values: np.ndarray, period: int) -> np.ndarray:
    """Exponential Moving Average — same length as input.

    First `period-1` entries are NaN (insufficient seed). Entry at
    index `period-1` is the SMA of the first `period` values (seed).
    From there forward, standard EMA with α = 2/(period+1).

    Returns empty array if `values` is shorter than `period`.
    """
    n = len(values)
    if n < period or period <= 0:
        return np.array([], dtype=float)
    out = np.full(n, math.nan, dtype=float)
    alpha = 2.0 / (period + 1)
    seed = float(np.mean(values[:period]))
    out[period - 1] = seed
    for i in range(period, n):
        out[i] = alpha * values[i] + (1 - alpha) * out[i - 1]
    return out


def ema_slope(values: np.ndarray, period: int, lookback: int) -> float:
    """Relative slope of the EMA over the last `lookback` ticks.

    (ema[-1] - ema[-1-lookback]) / ema[-1-lookback]

    Returns a decimal fraction (positive = up). NaN if insufficient.
    """
    e = ema(values, period)
    if len(e) < lookback + 1:
        return math.nan
    a, b = e[-1 - lookback], e[-1]
    if math.isnan(a) or math.isnan(b) or a == 0:
        return math.nan
    return float((b - a) / a)


# ---------------------------------------------------------------------------
# ADX
# ---------------------------------------------------------------------------


def adx(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14
) -> float:
    """Average Directional Index — Wilder's classic, scaled 0-100.

    Uses simple averaging (not Wilder smoothing) for consistency with
    `atr` above. Returns NaN if insufficient data (needs at least
    2*period+1 candles).
    """
    n = len(high)
    if n < 2 * period + 1 or n != len(low) or n != len(close):
        return math.nan

    up_move = high[1:] - high[:-1]
    down_move = low[:-1] - low[1:]

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    tr = true_range(high, low, close)

    # Smooth via simple moving sum over `period`
    def _smooth(arr: np.ndarray) -> np.ndarray:
        # Returns array of length (len(arr) - period + 1), each entry
        # is the sum over the trailing `period` values.
        if len(arr) < period:
            return np.array([], dtype=float)
        c = np.cumsum(np.insert(arr, 0, 0.0))
        return c[period:] - c[:-period]

    tr_s = _smooth(tr)
    plus_s = _smooth(plus_dm)
    minus_s = _smooth(minus_dm)

    if len(tr_s) == 0:
        return math.nan

    plus_di = np.where(tr_s > 0, 100.0 * plus_s / tr_s, 0.0)
    minus_di = np.where(tr_s > 0, 100.0 * minus_s / tr_s, 0.0)

    di_sum = plus_di + minus_di
    dx = np.where(di_sum > 0, 100.0 * np.abs(plus_di - minus_di) / di_sum, 0.0)

    if len(dx) < period:
        return math.nan
    return float(np.mean(dx[-period:]))


# ---------------------------------------------------------------------------
# Realized vol
# ---------------------------------------------------------------------------


def realized_vol_annualized(
    close: np.ndarray, period: int, periods_per_year: float
) -> float:
    """Annualized realized volatility of log returns.

    σ_annual = std(log_returns_last_N) * sqrt(periods_per_year)

    `periods_per_year` for common candles:
    - 5m: 105_120 (288 candles/day × 365)
    - 1h: 8_760
    - 1d: 365

    Returns NaN if insufficient.
    """
    if len(close) < period + 1 or periods_per_year <= 0:
        return math.nan
    window = close[-(period + 1):]
    log_returns = np.log(window[1:] / window[:-1])
    if len(log_returns) == 0:
        return math.nan
    return float(np.std(log_returns) * math.sqrt(periods_per_year))


# ---------------------------------------------------------------------------
# Hurst exponent (rescaled-range)
# ---------------------------------------------------------------------------


def hurst_rs(close: np.ndarray, max_lag: int = 20) -> float:
    """Hurst exponent via rescaled-range, applied to log returns.

    Interpretation (acting on returns, not prices):
    - H ≈ 0.5: returns are uncorrelated → random walk in price, or
      a deterministic drift plus iid noise (linear trend ≈ here too).
    - H > 0.5: returns are positively autocorrelated → momentum.
      Up-moves tend to follow up-moves; price exhibits sustained
      runs beyond what pure drift would produce.
    - H < 0.5: returns are negatively autocorrelated →
      mean-reversion. Up-moves tend to be followed by down-moves.

    NaN if insufficient data (need at least max_lag * 4 samples).

    Note: applying R/S directly to the price (or to log price) inflates
    H by ~0.5 because the integrated series is non-stationary —
    Mandelbrot's original derivation requires a stationary input. We
    therefore work on log returns, which is the convention used by
    most R/S implementations (e.g., the `hurst` PyPI package).
    """
    if len(close) < max_lag * 4 or max_lag < 2:
        return math.nan

    series = np.log(close[1:] / close[:-1])
    n = len(series)
    if n < max_lag * 4:
        return math.nan

    lags = list(range(2, max_lag + 1))
    rs_values = []
    for lag in lags:
        # Split into non-overlapping chunks of length `lag`
        num_chunks = n // lag
        if num_chunks < 1:
            continue
        chunks = series[: num_chunks * lag].reshape(num_chunks, lag)
        rs_per_chunk = []
        for chunk in chunks:
            mean = chunk.mean()
            dev = chunk - mean
            cum = np.cumsum(dev)
            r = cum.max() - cum.min()
            s = chunk.std()
            if s > 0:
                rs_per_chunk.append(r / s)
        if rs_per_chunk:
            rs_values.append((lag, float(np.mean(rs_per_chunk))))

    if len(rs_values) < 4:
        return math.nan

    xs = np.log([lag for lag, _ in rs_values])
    ys = np.log([rs for _, rs in rs_values])
    slope, _intercept, _r, _p, _se = stats.linregress(xs, ys)
    return float(slope)


# ---------------------------------------------------------------------------
# Linear regression slope
# ---------------------------------------------------------------------------


def linreg_slope(values: np.ndarray) -> float:
    """Slope of a linear regression price ~ time index.

    Time index is 0..len(values)-1. Returns the raw slope (price units
    per tick). To compare across pairs, normalize by mean — see
    `linreg_slope_relative`.

    NaN if fewer than 2 points.
    """
    n = len(values)
    if n < 2:
        return math.nan
    xs = np.arange(n, dtype=float)
    slope, _intercept, _r, _p, _se = stats.linregress(xs, values)
    return float(slope)


def linreg_slope_relative(values: np.ndarray) -> float:
    """`linreg_slope` divided by mean of values. Scale-free.

    NaN if mean is 0 or insufficient data.
    """
    s = linreg_slope(values)
    if math.isnan(s):
        return math.nan
    mean = float(np.mean(values))
    if mean == 0:
        return math.nan
    return s / mean


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def percentile_safe(arr: np.ndarray, q: float) -> Optional[float]:
    """Percentile that returns None on empty input (numpy raises).

    `q` in [0, 100].
    """
    if len(arr) == 0:
        return None
    return float(np.percentile(arr, q))


def to_arrays(candles: list[dict]) -> dict[str, np.ndarray]:
    """Convert a list of candle dicts to numpy arrays.

    Shape established in MARKET_REGIME_SPEC.md: each candle has
    `timestamp/open/high/low/close/volume/quote_asset_volume/n_trades/...`.
    Returns:

        {"ts", "open", "high", "low", "close", "volume", "qvol", "ntrades"}

    Missing fields become empty arrays. Order preserved (caller is
    responsible for ensuring ascending timestamp).
    """
    if not candles:
        empty = np.array([], dtype=float)
        return {k: empty for k in
                ("ts", "open", "high", "low", "close", "volume", "qvol", "ntrades")}
    return {
        "ts":      np.array([float(c.get("timestamp", 0))         for c in candles]),
        "open":    np.array([float(c.get("open", 0) or 0)         for c in candles]),
        "high":    np.array([float(c.get("high", 0) or 0)         for c in candles]),
        "low":     np.array([float(c.get("low", 0) or 0)          for c in candles]),
        "close":   np.array([float(c.get("close", 0) or 0)        for c in candles]),
        "volume":  np.array([float(c.get("volume", 0) or 0)       for c in candles]),
        "qvol":    np.array([float(c.get("quote_asset_volume", 0) or 0) for c in candles]),
        "ntrades": np.array([float(c.get("n_trades", 0) or 0)     for c in candles]),
    }
