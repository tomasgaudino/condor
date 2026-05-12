"""Tests for condor.trading_agent.adaptive.indicators.

Cover:
- Each indicator on a known synthetic series (deterministic expected values).
- Boundary conditions: empty input, exactly-at-minimum, one-below-minimum.
- Mathematical properties: NATR ≥ 0, ADX in [0, 100], Hurst around 0.5
  for white noise.
- `to_arrays` shape contract.
"""

from __future__ import annotations

import math

import numpy as np

from condor.trading_agent.adaptive import indicators as ind


# ---------------------------------------------------------------------------
# True range / ATR / NATR
# ---------------------------------------------------------------------------


def test_true_range_simple():
    # 3 candles. TR is length 2.
    high = np.array([10.0, 12.0, 11.0])
    low = np.array([9.0, 10.0, 9.5])
    close = np.array([9.5, 11.5, 10.0])
    tr = ind.true_range(high, low, close)
    # i=0 → max(12-10, |12-9.5|, |10-9.5|) = max(2, 2.5, 0.5) = 2.5
    # i=1 → max(11-9.5, |11-11.5|, |9.5-11.5|) = max(1.5, 0.5, 2.0) = 2.0
    assert tr.tolist() == [2.5, 2.0]


def test_true_range_too_short():
    assert len(ind.true_range(np.array([1.0]), np.array([1.0]), np.array([1.0]))) == 0
    assert len(ind.true_range(np.array([]), np.array([]), np.array([]))) == 0


def test_atr_insufficient_data():
    high = np.array([10.0, 11.0])
    low = np.array([9.0, 10.0])
    close = np.array([9.5, 10.5])
    # period=14 needs 15 candles total. We have 2.
    assert math.isnan(ind.atr(high, low, close, period=14))


def test_atr_exact_window():
    # 15 identical candles (range = 1.0 each, close = mid). TR is
    # constant at 1.0 because the only contribution is high-low.
    n = 15
    high = np.full(n, 11.0)
    low = np.full(n, 10.0)
    close = np.full(n, 10.5)
    a = ind.atr(high, low, close, period=14)
    assert a == 1.0


def test_natr_normalized():
    n = 15
    high = np.full(n, 101.0)
    low = np.full(n, 100.0)
    close = np.full(n, 100.5)
    # TR each step: max(1, |101-100.5|, |100-100.5|) = 1.0
    # ATR(14) = 1.0; NATR = 1.0 / 100.5
    nat = ind.natr(high, low, close, period=14)
    assert abs(nat - (1.0 / 100.5)) < 1e-9


def test_natr_nonneg_property():
    # NATR must always be >= 0 (TR is non-negative, close > 0).
    rng = np.random.default_rng(42)
    close = 100 + np.cumsum(rng.normal(0, 0.5, 50))
    high = close + rng.uniform(0.1, 1.0, 50)
    low = close - rng.uniform(0.1, 1.0, 50)
    nat = ind.natr(high, low, close, period=14)
    assert nat >= 0


# ---------------------------------------------------------------------------
# Bollinger width
# ---------------------------------------------------------------------------


def test_bollinger_width_zero_for_constant():
    close = np.full(25, 100.0)
    assert ind.bollinger_width(close, period=20) == 0.0


def test_bollinger_width_positive_for_varying():
    rng = np.random.default_rng(7)
    close = 100 + rng.normal(0, 2, 25)
    w = ind.bollinger_width(close, period=20)
    assert w > 0


def test_bollinger_width_insufficient_data():
    assert math.isnan(ind.bollinger_width(np.array([1.0, 2.0]), period=20))


# ---------------------------------------------------------------------------
# EMA + slope
# ---------------------------------------------------------------------------


def test_ema_seed_is_sma():
    values = np.arange(1, 11, dtype=float)  # 1..10
    e = ind.ema(values, period=5)
    # First valid index is period-1 = 4
    assert math.isnan(e[3])
    assert e[4] == np.mean(values[:5])  # SMA seed = 3.0


def test_ema_recurrence():
    values = np.arange(1, 11, dtype=float)
    e = ind.ema(values, period=5)
    alpha = 2.0 / 6
    expected = e[4]
    for i in range(5, 10):
        expected = alpha * values[i] + (1 - alpha) * expected
        assert abs(e[i] - expected) < 1e-12


def test_ema_too_short():
    assert len(ind.ema(np.array([1.0, 2.0]), period=5)) == 0


def test_ema_slope_positive_for_uptrend():
    # Linear uptrend → positive slope.
    close = np.arange(1, 51, dtype=float)
    s = ind.ema_slope(close, period=20, lookback=10)
    assert s > 0


def test_ema_slope_negative_for_downtrend():
    close = np.arange(50, 0, -1, dtype=float)
    s = ind.ema_slope(close, period=20, lookback=10)
    assert s < 0


def test_ema_slope_nan_insufficient():
    assert math.isnan(ind.ema_slope(np.arange(5, dtype=float), period=20, lookback=10))


# ---------------------------------------------------------------------------
# ADX
# ---------------------------------------------------------------------------


def test_adx_insufficient_data():
    high = np.arange(10, dtype=float)
    low = high - 1
    close = high - 0.5
    assert math.isnan(ind.adx(high, low, close, period=14))


def test_adx_strong_uptrend_high():
    # Pure uptrend: ADX should be high (well above 25).
    n = 50
    high = np.arange(n, dtype=float) * 2 + 100
    low = high - 1
    close = high - 0.5
    a = ind.adx(high, low, close, period=14)
    assert not math.isnan(a)
    assert a > 25  # trending threshold from spec


def test_adx_in_bounds():
    rng = np.random.default_rng(99)
    n = 60
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    high = close + rng.uniform(0.1, 0.5, n)
    low = close - rng.uniform(0.1, 0.5, n)
    a = ind.adx(high, low, close, period=14)
    assert 0 <= a <= 100


# ---------------------------------------------------------------------------
# Realized vol
# ---------------------------------------------------------------------------


def test_realized_vol_zero_for_constant():
    close = np.full(70, 100.0)
    assert ind.realized_vol_annualized(close, period=60, periods_per_year=105_120) == 0.0


def test_realized_vol_scales_with_periods_per_year():
    rng = np.random.default_rng(5)
    close = 100 + np.cumsum(rng.normal(0, 0.1, 100))
    v1 = ind.realized_vol_annualized(close, period=60, periods_per_year=365)
    v2 = ind.realized_vol_annualized(close, period=60, periods_per_year=365 * 4)
    # 4x periods_per_year → 2x vol
    assert abs((v2 / v1) - 2.0) < 1e-9


def test_realized_vol_insufficient_data():
    assert math.isnan(
        ind.realized_vol_annualized(np.array([100.0, 101.0]), period=60, periods_per_year=365)
    )


# ---------------------------------------------------------------------------
# Hurst
# ---------------------------------------------------------------------------


def test_hurst_insufficient_data():
    assert math.isnan(ind.hurst_rs(np.arange(10, dtype=float), max_lag=20))


def test_hurst_random_walk_near_half():
    # A pure random walk has iid increments → H of the returns near 0.5.
    rng = np.random.default_rng(123)
    n = 500
    increments = rng.normal(0, 1, n)
    close = 100 + np.cumsum(increments)
    h = ind.hurst_rs(close, max_lag=20)
    assert not math.isnan(h)
    assert 0.3 < h < 0.7  # loose; estimator is noisy on 500 pts


def test_hurst_momentum_above_half():
    # AR(1) returns with positive coefficient → momentum, H > 0.5.
    rng = np.random.default_rng(456)
    n = 1000
    returns = np.zeros(n)
    phi = 0.6  # positive autocorrelation
    for i in range(1, n):
        returns[i] = phi * returns[i - 1] + rng.normal(0, 0.001)
    close = 100 * np.exp(np.cumsum(returns))
    h = ind.hurst_rs(close, max_lag=20)
    assert not math.isnan(h)
    assert h > 0.55


def test_hurst_mean_reverting_below_half():
    # Ornstein-Uhlenbeck price (mean-reverting price level).
    # Equivalent: log-returns are negatively autocorrelated.
    rng = np.random.default_rng(789)
    n = 1000
    theta = 0.9  # strong pull to mean (close to 1.0)
    mu = math.log(100)
    sigma = 0.005
    log_price = np.zeros(n)
    log_price[0] = mu
    for i in range(1, n):
        log_price[i] = log_price[i - 1] + theta * (mu - log_price[i - 1]) + rng.normal(0, sigma)
    close = np.exp(log_price)
    h = ind.hurst_rs(close, max_lag=20)
    assert not math.isnan(h)
    assert h < 0.45


# ---------------------------------------------------------------------------
# Linear regression slope
# ---------------------------------------------------------------------------


def test_linreg_slope_perfect_line():
    values = np.arange(10, dtype=float) * 3 + 5  # slope = 3
    assert abs(ind.linreg_slope(values) - 3.0) < 1e-9


def test_linreg_slope_flat():
    values = np.full(10, 7.0)
    assert ind.linreg_slope(values) == 0.0


def test_linreg_slope_nan_too_short():
    assert math.isnan(ind.linreg_slope(np.array([1.0])))


def test_linreg_slope_relative_scale_free():
    values = np.arange(10, dtype=float) * 3 + 5  # mean = 5+13.5=18.5; slope=3
    r = ind.linreg_slope_relative(values)
    expected = 3.0 / float(np.mean(values))
    assert abs(r - expected) < 1e-9


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_percentile_safe_empty_returns_none():
    assert ind.percentile_safe(np.array([]), 50) is None


def test_percentile_safe_value():
    arr = np.arange(100, dtype=float)
    assert ind.percentile_safe(arr, 33) == np.percentile(arr, 33)


def test_to_arrays_empty():
    out = ind.to_arrays([])
    for key in ("ts", "open", "high", "low", "close", "volume", "qvol", "ntrades"):
        assert key in out
        assert len(out[key]) == 0


def test_to_arrays_shape():
    candles = [
        {
            "timestamp": 1.0, "open": 10.0, "high": 11.0, "low": 9.0,
            "close": 10.5, "volume": 100.0, "quote_asset_volume": 1000.0,
            "n_trades": 50.0,
        },
        {
            "timestamp": 2.0, "open": 10.5, "high": 12.0, "low": 10.0,
            "close": 11.0, "volume": 150.0, "quote_asset_volume": 1500.0,
            "n_trades": 75.0,
        },
    ]
    out = ind.to_arrays(candles)
    assert out["close"].tolist() == [10.5, 11.0]
    assert out["qvol"].tolist() == [1000.0, 1500.0]
    assert out["ntrades"].tolist() == [50.0, 75.0]


def test_to_arrays_missing_field_defaults_to_zero():
    candles = [{"timestamp": 1.0, "close": 10.0}]  # no volume / high / low
    out = ind.to_arrays(candles)
    assert out["volume"].tolist() == [0.0]
    assert out["high"].tolist() == [0.0]
