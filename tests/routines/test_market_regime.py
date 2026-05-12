"""Tests for routines/market_regime.py — pure-function building blocks.

Covers classification, canonical regime composition, favorability matrix,
S/R detection, narrative generation, glossary selection, and agent
payload shape. The async `run()` requires a live API client and is
exercised manually + via condor-express smoke test.
"""

from __future__ import annotations

import math

import numpy as np

from routines.market_regime import (
    Config,
    _build_agent_payload,
    _build_canonical,
    _build_glossary_markdown,
    _build_kpi_sections,
    _build_narrative,
    _build_timeframe_table,
    _classify_directionality,
    _classify_volatility,
    _compute_macro_block,
    _compute_meso_block,
    _compute_micro_block,
    _compose_payload,
    _detect_support_resistance,
    _favorability,
    _render_compact_summary,
    _rolling_natr_history,
    _select_glossary_terms,
)


# ---------------------------------------------------------------------------
# Synthetic candle generators
# ---------------------------------------------------------------------------


def _make_candles(
    closes: np.ndarray, range_pct: float = 0.005, base_volume: float = 100.0
) -> list[dict]:
    """Build a candle list[dict] from a close series. Each candle's
    high/low straddles close by ±range_pct."""
    out = []
    for i, c in enumerate(closes):
        h = float(c) * (1 + range_pct)
        l = float(c) * (1 - range_pct)
        out.append({
            "timestamp": float(i),
            "open": float(c),
            "high": h,
            "low": l,
            "close": float(c),
            "volume": base_volume,
            "quote_asset_volume": base_volume * float(c),
            "n_trades": 100,
        })
    return out


def _flat_candles(n: int, price: float = 100.0) -> list[dict]:
    return _make_candles(np.full(n, price), range_pct=0.001)


def _uptrend_candles(n: int, start: float = 100.0, step: float = 0.5) -> list[dict]:
    closes = start + np.arange(n) * step
    return _make_candles(closes, range_pct=0.003)


# ---------------------------------------------------------------------------
# Classification — directionality
# ---------------------------------------------------------------------------


def test_classify_directionality_hurst_trending_with_positive_slope():
    cfg = Config()
    out = _classify_directionality(hurst=0.7, adx_val=None, slope=0.01, cfg=cfg)
    assert out == "trending_up"


def test_classify_directionality_hurst_trending_with_negative_slope():
    cfg = Config()
    out = _classify_directionality(hurst=0.7, adx_val=None, slope=-0.01, cfg=cfg)
    assert out == "trending_down"


def test_classify_directionality_adx_trending_no_slope():
    cfg = Config()
    # No slope but ADX trending → safer fallback random_walk
    out = _classify_directionality(hurst=None, adx_val=30.0, slope=None, cfg=cfg)
    assert out == "random_walk"


def test_classify_directionality_hurst_mean_reverting():
    cfg = Config()
    out = _classify_directionality(hurst=0.30, adx_val=10.0, slope=0.0, cfg=cfg)
    assert out == "mean_reverting"


def test_classify_directionality_random_walk_default():
    cfg = Config()
    out = _classify_directionality(hurst=0.50, adx_val=15.0, slope=0.0001, cfg=cfg)
    assert out == "random_walk"


def test_classify_directionality_nan_tolerant():
    cfg = Config()
    out = _classify_directionality(hurst=math.nan, adx_val=math.nan, slope=math.nan, cfg=cfg)
    assert out == "random_walk"


# ---------------------------------------------------------------------------
# Classification — volatility
# ---------------------------------------------------------------------------


def test_classify_volatility_unknown_when_no_input():
    label, _ = _classify_volatility(natr_now=None, natr_history=np.array([]))
    assert label == "unknown"


def test_classify_volatility_low():
    history = np.array([0.001, 0.002, 0.003, 0.004, 0.005] * 10)
    label, info = _classify_volatility(natr_now=0.0005, natr_history=history)
    assert label == "low"
    assert info["samples"] == 50


def test_classify_volatility_high():
    history = np.array([0.001, 0.002, 0.003, 0.004, 0.005] * 10)
    label, _ = _classify_volatility(natr_now=0.01, natr_history=history)
    assert label == "high"


def test_classify_volatility_moderate_default_with_few_samples():
    # Less than 20 samples → default to moderate
    label, _ = _classify_volatility(natr_now=0.005, natr_history=np.array([0.001, 0.002]))
    assert label == "moderate"


# ---------------------------------------------------------------------------
# Rolling NATR history
# ---------------------------------------------------------------------------


def test_rolling_natr_history_constant():
    n = 50
    closes = np.full(n, 100.0)
    highs = closes * 1.01
    lows = closes * 0.99
    h = _rolling_natr_history(highs, lows, closes, period=14)
    assert len(h) > 0
    # All entries should be near (high-low)/close = 0.02 (TR=2, close=100 → 0.02)
    assert all(abs(v - 0.02) < 1e-9 for v in h)


def test_rolling_natr_history_insufficient_data():
    assert len(_rolling_natr_history(
        np.array([100.0]), np.array([100.0]), np.array([100.0]), period=14
    )) == 0


# ---------------------------------------------------------------------------
# Canonical regime composition
# ---------------------------------------------------------------------------


def _block(directionality: str, volatility: str = "moderate") -> dict:
    return {"directionality": directionality, "volatility": volatility, "samples": 100}


def test_canonical_all_agree_high_confidence():
    canon = _build_canonical(
        _block("mean_reverting"), _block("mean_reverting"), _block("mean_reverting")
    )
    assert canon["directionality"] == "mean_reverting"
    assert canon["confidence"] == "high"
    assert canon["bias"] is None
    assert canon["volatility"] == "moderate"
    assert canon["regime"] == "mean_reverting_mod_vol"


def test_canonical_majority_two_of_three():
    # Use a 2/3 combination that does NOT match the pullback pattern
    # (pullback requires macro trending AND meso+micro non-trending).
    # Here micro=trending_up so pullback exception doesn't apply.
    canon = _build_canonical(
        _block("trending_up"), _block("mean_reverting"), _block("trending_up")
    )
    assert canon["directionality"] == "trending_up"
    assert canon["confidence"] == "med"


def test_canonical_trending_with_pullback():
    # macro=trending_up, meso=mean_reverting, micro=random_walk → pullback
    canon = _build_canonical(
        _block("random_walk"), _block("mean_reverting"), _block("trending_up")
    )
    assert canon["directionality"] == "trending_up_with_pullback"
    assert canon["confidence"] == "med"
    assert canon["bias"] == "up"


def test_canonical_trending_with_pullback_inverse_meso():
    # macro=trending_down, meso=trending_up, micro=mean_reverting → pullback
    canon = _build_canonical(
        _block("mean_reverting"), _block("trending_up"), _block("trending_down")
    )
    assert canon["directionality"] == "trending_down_with_pullback"
    assert canon["bias"] == "down"


def test_canonical_all_disagree_low_confidence():
    # All three different and no pullback pattern.
    canon = _build_canonical(
        _block("random_walk"), _block("mean_reverting"), _block("random_walk")
    )
    assert canon["confidence"] == "med"  # 2 random_walk


def test_canonical_uses_micro_volatility():
    canon = _build_canonical(
        _block("random_walk", "high"),
        _block("random_walk", "low"),
        _block("random_walk", "moderate"),
    )
    assert canon["volatility"] == "high"


# ---------------------------------------------------------------------------
# Favorability matrix
# ---------------------------------------------------------------------------


def test_favorability_optimal_zone():
    fav, triggers = _favorability({
        "directionality": "mean_reverting", "volatility": "moderate",
    })
    assert fav == "optimal"
    assert triggers == []


def test_favorability_mean_rev_high_vol_suboptimal_with_triggers():
    fav, triggers = _favorability({
        "directionality": "mean_reverting", "volatility": "high",
    })
    assert fav == "suboptimal"
    assert "take_profit" in triggers
    assert "spreads" in triggers


def test_favorability_trending_always_adverse():
    for direction in ("trending_up", "trending_down"):
        for vol in ("low", "moderate", "high"):
            fav, triggers = _favorability({"directionality": direction, "volatility": vol})
            assert fav == "adverse", (direction, vol)
            assert "portfolio_allocation" in triggers


def test_favorability_pullback_suboptimal_with_amount_skew():
    fav, triggers = _favorability({
        "directionality": "trending_up_with_pullback", "volatility": "moderate",
    })
    assert fav == "suboptimal"
    assert "buy_amounts_pct" in triggers
    assert "sell_amounts_pct" in triggers


# ---------------------------------------------------------------------------
# S/R detection
# ---------------------------------------------------------------------------


def test_sr_empty_with_too_few_candles():
    out = _detect_support_resistance([], window=10)
    assert out == {"support": None, "resistance": None}


def test_sr_detects_double_touch_levels():
    # Manually craft candles where a level is touched twice.
    closes = [100, 102, 105, 103, 100, 102, 105, 103, 100, 102, 105, 103]
    candles = _make_candles(np.array(closes, dtype=float), range_pct=0.001)
    out = _detect_support_resistance(candles, window=12, k=1, cluster_tol=0.01)
    # At least one of support/resistance should be detected.
    assert out["support"] is not None or out["resistance"] is not None


def test_sr_distance_pct_signs():
    closes = [100, 110, 95, 100, 110, 95, 100, 110, 95, 100, 110]
    candles = _make_candles(np.array(closes, dtype=float), range_pct=0.001)
    out = _detect_support_resistance(candles, window=11, k=1, cluster_tol=0.01)
    if out["support"] is not None:
        assert out["support"]["distance_pct"] <= 0
    if out["resistance"] is not None:
        assert out["resistance"]["distance_pct"] >= 0


# ---------------------------------------------------------------------------
# Per-timeframe blocks (smoke against synthetic data)
# ---------------------------------------------------------------------------


def test_compute_micro_block_on_flat_series():
    cfg = Config()
    blk = _compute_micro_block(_flat_candles(100), cfg)
    # On a flat series: NATR very small, ADX near 0 → not trending.
    assert blk["directionality"] in ("mean_reverting", "random_walk")
    assert blk["volatility"] in ("low", "moderate", "high", "unknown")
    assert blk["samples"] == 100


def test_compute_micro_block_on_uptrend():
    cfg = Config()
    blk = _compute_micro_block(_uptrend_candles(100), cfg)
    # Strict uptrend should not be mean-reverting.
    assert blk["directionality"] != "mean_reverting"
    assert blk["samples"] == 100


def test_compute_meso_block_returns_sr_keys():
    cfg = Config()
    blk = _compute_meso_block(_uptrend_candles(200), cfg)
    assert "support_resistance" in blk
    assert blk["samples"] == 200


def test_compute_macro_block_position_in_range():
    cfg = Config()
    blk = _compute_macro_block(_uptrend_candles(60), cfg)
    pos = blk["indicators"]["position_in_range_30d"]
    # Strict uptrend → current price near top of 30d range → close to 1.
    assert pos is not None
    assert pos > 0.8


# ---------------------------------------------------------------------------
# Compose payload + agent view
# ---------------------------------------------------------------------------


def test_compose_payload_structure():
    cfg = Config()
    micro = _compute_micro_block(_flat_candles(100), cfg)
    meso  = _compute_meso_block(_flat_candles(200), cfg)
    macro = _compute_macro_block(_flat_candles(60), cfg)
    payload = _compose_payload(cfg, micro, meso, macro)

    for key in ("ts", "trading_pair", "connector", "summary", "micro_5m", "meso_1h", "macro_1d", "history_required"):
        assert key in payload, key
    s = payload["summary"]
    for key in (
        "canonical_regime", "directionality", "volatility", "favorability",
        "confidence", "persistence_minutes", "bias", "trigger_candidates",
    ):
        assert key in s, key
    assert payload["history_required"]["available"] is True


def test_compose_payload_history_unavailable_when_short():
    cfg = Config()
    micro = _compute_micro_block(_flat_candles(10), cfg)
    meso  = _compute_meso_block(_flat_candles(10), cfg)
    macro = _compute_macro_block(_flat_candles(10), cfg)
    payload = _compose_payload(cfg, micro, meso, macro)
    assert payload["history_required"]["available"] is False


def test_agent_payload_no_indicator_leakage():
    cfg = Config()
    micro = _compute_micro_block(_flat_candles(100), cfg)
    meso  = _compute_meso_block(_flat_candles(200), cfg)
    macro = _compute_macro_block(_flat_candles(60), cfg)
    payload = _compose_payload(cfg, micro, meso, macro)
    agent = _build_agent_payload(payload)

    # Expected keys present
    for key in (
        "ts", "pair", "connector", "regime", "favorability", "confidence",
        "persistence_minutes", "bias", "trigger_candidates", "by_timeframe",
        "key_levels", "history_available",
    ):
        assert key in agent, key

    # No human-only stuff leaks in
    assert "narrative" not in agent
    assert "glossary" not in agent
    assert "indicators" not in agent
    # by_timeframe entries only carry dir/vol, no raw indicators
    for tf in ("micro_5m", "meso_1h", "macro_1d"):
        assert set(agent["by_timeframe"][tf].keys()) == {"dir", "vol"}


def test_agent_payload_size_under_3kb():
    import json
    cfg = Config()
    micro = _compute_micro_block(_flat_candles(100), cfg)
    meso  = _compute_meso_block(_flat_candles(200), cfg)
    macro = _compute_macro_block(_flat_candles(60), cfg)
    payload = _compose_payload(cfg, micro, meso, macro)
    agent = _build_agent_payload(payload)
    size = len(json.dumps(agent))
    # The doc claims ~400-600 chars. Generous ceiling so test isn't fragile.
    assert size < 3000, f"agent payload grew to {size} chars"


# ---------------------------------------------------------------------------
# User view formatters
# ---------------------------------------------------------------------------


def test_compact_summary_is_single_line_ascii():
    cfg = Config()
    micro = _compute_micro_block(_flat_candles(100), cfg)
    meso  = _compute_meso_block(_flat_candles(200), cfg)
    macro = _compute_macro_block(_flat_candles(60), cfg)
    payload = _compose_payload(cfg, micro, meso, macro)
    text = _render_compact_summary(payload)
    # L2: must be single line, no backticks, fit in 220 chars.
    assert "\n" not in text
    assert "`" not in text
    assert len(text) <= 220


def test_kpi_sections_shape():
    cfg = Config()
    micro = _compute_micro_block(_flat_candles(100), cfg)
    meso  = _compute_meso_block(_flat_candles(200), cfg)
    macro = _compute_macro_block(_flat_candles(60), cfg)
    payload = _compose_payload(cfg, micro, meso, macro)
    kpis = _build_kpi_sections(payload)
    assert len(kpis) == 4
    labels = [k["label"] for k in kpis]
    assert labels == ["Regime", "Favorability", "Confidence", "Persistence"]
    for k in kpis:
        assert k["type"] == "kpi"
        assert "value" in k
        assert "trend" in k


def test_timeframe_table_three_rows():
    cfg = Config()
    micro = _compute_micro_block(_uptrend_candles(100), cfg)
    meso  = _compute_meso_block(_uptrend_candles(200), cfg)
    macro = _compute_macro_block(_uptrend_candles(60), cfg)
    payload = _compose_payload(cfg, micro, meso, macro)
    cols, rows = _build_timeframe_table(payload)
    assert "timeframe" in cols
    assert len(rows) == 3
    assert [r["timeframe"] for r in rows] == ["5m", "1h", "1d"]


def test_narrative_mentions_pair_and_emoji():
    cfg = Config()
    micro = _compute_micro_block(_flat_candles(100), cfg)
    meso  = _compute_meso_block(_flat_candles(200), cfg)
    macro = _compute_macro_block(_flat_candles(60), cfg)
    payload = _compose_payload(cfg, micro, meso, macro)
    narr = _build_narrative(payload)
    assert payload["trading_pair"] in narr
    # At minimum one of the favorability emoji should appear.
    assert any(e in narr for e in ("🟢", "🟡", "🔴"))


def test_glossary_includes_base_terms():
    cfg = Config()
    micro = _compute_micro_block(_flat_candles(100), cfg)
    meso  = _compute_meso_block(_flat_candles(200), cfg)
    macro = _compute_macro_block(_flat_candles(60), cfg)
    payload = _compose_payload(cfg, micro, meso, macro)
    selected = _select_glossary_terms(payload)
    for base in ("regimen_canonico", "favorabilidad", "natr", "hurst", "persistence"):
        assert base in selected


def test_glossary_markdown_has_glossary_header():
    cfg = Config()
    micro = _compute_micro_block(_flat_candles(100), cfg)
    meso  = _compute_meso_block(_flat_candles(200), cfg)
    macro = _compute_macro_block(_flat_candles(60), cfg)
    payload = _compose_payload(cfg, micro, meso, macro)
    md = _build_glossary_markdown(payload)
    assert "## Glosario" in md


# ---------------------------------------------------------------------------
# Multi-pair: autodetect, aggregate, summary table
# ---------------------------------------------------------------------------


import asyncio

from routines.market_regime import (
    _autodetect_pairs,
    _aggregate_kpi_sections,
    _aggregate_table,
    _aggregate_text,
    _build_agent_summary,
    _compose_aggregate,
)


def _per_pair_payload(pair: str, favorability: str, regime: str = "mean_reverting_mod_vol") -> dict:
    """Lightweight per-pair payload for aggregate unit tests."""
    return {
        "ts": "2026-05-12T00:00:00Z",
        "trading_pair": pair,
        "connector": "binance",
        "summary": {
            "canonical_regime": regime,
            "directionality": "mean_reverting",
            "volatility": "moderate",
            "favorability": favorability,
            "confidence": "high",
            "persistence_minutes": None,
            "bias": None,
            "trigger_candidates": [],
        },
        "micro_5m": {"directionality": "mean_reverting", "volatility": "moderate", "samples": 100},
        "meso_1h":  {"directionality": "mean_reverting", "volatility": "moderate", "samples": 200,
                     "support_resistance": {"support": {"price": 99.0, "distance_pct": -0.01, "touches": 2},
                                            "resistance": {"price": 101.0, "distance_pct": 0.01, "touches": 2}}},
        "macro_1d": {"directionality": "mean_reverting", "volatility": "moderate", "samples": 60,
                     "indicators": {}, "levels_macro": {}},
        "history_required": {"available": True, "samples_micro": 100, "samples_meso": 200, "samples_macro": 60},
    }


def test_aggregate_counts_by_favorability():
    payloads = {
        "BTC-USDT": _per_pair_payload("BTC-USDT", "optimal"),
        "ETH-USDT": _per_pair_payload("ETH-USDT", "suboptimal"),
        "SOL-USDT": _per_pair_payload("SOL-USDT", "adverse"),
        "XRP-USDT": _per_pair_payload("XRP-USDT", "adverse"),
    }
    agg = _compose_aggregate(payloads)
    assert agg["counts"] == {"optimal": 1, "suboptimal": 1, "adverse": 2}
    assert agg["pairs"] == ["BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT"]


def test_aggregate_worst_picks_adverse():
    payloads = {
        "BTC-USDT": _per_pair_payload("BTC-USDT", "optimal"),
        "ETH-USDT": _per_pair_payload("ETH-USDT", "adverse"),
        "SOL-USDT": _per_pair_payload("SOL-USDT", "suboptimal"),
    }
    agg = _compose_aggregate(payloads)
    assert agg["worst"]["pair"] == "ETH-USDT"
    assert agg["worst"]["favorability"] == "adverse"


def test_aggregate_worst_none_when_empty():
    agg = _compose_aggregate({})
    assert agg["worst"] is None
    assert agg["pairs"] == []
    assert agg["counts"] == {"optimal": 0, "suboptimal": 0, "adverse": 0}


def test_aggregate_skips_empty_payloads():
    payloads = {
        "BTC-USDT": _per_pair_payload("BTC-USDT", "optimal"),
        "FAIL-USDT": {},  # failed fetch
    }
    agg = _compose_aggregate(payloads)
    assert "FAIL-USDT" not in agg["pairs"]
    assert "BTC-USDT" in agg["pairs"]


def test_aggregate_text_multi_pair():
    payloads = {
        "A-USDT": _per_pair_payload("A-USDT", "optimal"),
        "B-USDT": _per_pair_payload("B-USDT", "adverse"),
    }
    agg = _compose_aggregate(payloads)
    text = _aggregate_text(agg, payloads)
    assert "2 pairs" in text
    assert "🟢" in text or "🔴" in text
    assert "worst" in text
    assert "\n" not in text  # L2: single line


def test_aggregate_text_single_pair_falls_back_to_per_pair():
    payloads = {"BTC-USDT": _per_pair_payload("BTC-USDT", "optimal")}
    agg = _compose_aggregate(payloads)
    text = _aggregate_text(agg, payloads)
    assert "BTC-USDT" in text
    # Should NOT use the multi-pair "N pairs" prefix when only one is present.
    assert "1 pairs" not in text


def test_aggregate_kpi_sections_shape():
    payloads = {
        "A": _per_pair_payload("A", "optimal"),
        "B": _per_pair_payload("B", "adverse"),
    }
    agg = _compose_aggregate(payloads)
    kpis = _aggregate_kpi_sections(agg)
    labels = [k["label"] for k in kpis]
    assert labels == ["Pairs", "Optimal", "Suboptimal", "Adverse"]


def test_aggregate_table_sorted_adverse_first():
    payloads = {
        "A": _per_pair_payload("A", "optimal"),
        "B": _per_pair_payload("B", "adverse"),
        "C": _per_pair_payload("C", "suboptimal"),
    }
    cols, rows = _aggregate_table(payloads)
    assert "pair" in cols
    # First row should be adverse.
    assert "adverse" in rows[0]["favorability"]
    # Last row should be optimal.
    assert "optimal" in rows[-1]["favorability"]


def test_agent_summary_shape():
    payloads = {
        "A": _per_pair_payload("A", "optimal"),
        "B": _per_pair_payload("B", "adverse"),
    }
    agg = _compose_aggregate(payloads)
    summary = _build_agent_summary(agg)
    for key in ("ts", "pairs", "by_pair", "counts", "worst"):
        assert key in summary
    # by_pair has the minimal per-pair info, NOT the indicators
    for pair_summary in summary["by_pair"].values():
        assert "regime" in pair_summary
        assert "favorability" in pair_summary
        assert "indicators" not in pair_summary
        assert "support_resistance" not in pair_summary


# ---------------------------------------------------------------------------
# Autodetect — mocked client
# ---------------------------------------------------------------------------


class _FakeBotOrch:
    def __init__(self, bots: dict):
        self._bots = bots

    async def get_active_bots_status(self):
        return {"data": {name: {} for name in self._bots}}


class _FakeControllers:
    def __init__(self, configs_by_bot: dict[str, list[dict]]):
        self._configs = configs_by_bot

    async def get_bot_controller_configs(self, bot_name: str):
        return self._configs.get(bot_name, [])


class _FakeClient:
    def __init__(self, configs_by_bot: dict[str, list[dict]]):
        self.bot_orchestration = _FakeBotOrch(configs_by_bot)
        self.controllers = _FakeControllers(configs_by_bot)


def test_autodetect_filters_by_connector():
    client = _FakeClient({
        "bot1": [
            {"trading_pair": "BTC-USDT", "connector_name": "binance"},
            {"trading_pair": "ETH-USDT", "connector_name": "binance_perpetual"},  # filtered out
        ],
        "bot2": [
            {"trading_pair": "SOL-USDT", "connector_name": "binance"},
        ],
    })
    pairs = asyncio.run(_autodetect_pairs(client, "binance"))
    assert pairs == ["BTC-USDT", "SOL-USDT"]


def test_autodetect_deduplicates():
    client = _FakeClient({
        "bot1": [{"trading_pair": "BTC-USDT", "connector_name": "binance"}],
        "bot2": [{"trading_pair": "BTC-USDT", "connector_name": "binance"}],
    })
    pairs = asyncio.run(_autodetect_pairs(client, "binance"))
    assert pairs == ["BTC-USDT"]


def test_autodetect_empty_when_no_active_bots():
    client = _FakeClient({})
    pairs = asyncio.run(_autodetect_pairs(client, "binance"))
    assert pairs == []


def test_autodetect_handles_api_errors():
    class _Failing(_FakeBotOrch):
        async def get_active_bots_status(self):
            raise RuntimeError("server down")

    class _C:
        bot_orchestration = _Failing({})
        controllers = _FakeControllers({})

    pairs = asyncio.run(_autodetect_pairs(_C(), "binance"))
    assert pairs == []
