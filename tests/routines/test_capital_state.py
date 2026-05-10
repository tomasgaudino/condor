"""Tests for routines/capital_state.py — pure-function building blocks.

Covers the deterministic computation pieces (parsing, formatting, capital
math, oversub detection, balance flattening). The async `run()` requires a
live Condor client and is exercised manually from `/routines` in Telegram.
"""

from __future__ import annotations

from routines.capital_state import (
    _bar,
    _compute_controller_block,
    _compute_global_block,
    _compute_summary,
    _flatten_balances,
    _fmt_pct,
    _fmt_usd,
    _parse_pair,
    _severity,
    _split_csv,
)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parse_pair_dash():
    assert _parse_pair("BTC-USDT") == ("BTC", "USDT")


def test_parse_pair_slash():
    assert _parse_pair("BTC/USDT") == ("BTC", "USDT")


def test_parse_pair_no_separator():
    # Defensive fallback — invalid pair, but don't crash
    assert _parse_pair("BTCUSDT") == ("BTCUSDT", "")


def test_split_csv_string():
    assert _split_csv("0.0002,0.0006,0.001") == ["0.0002", "0.0006", "0.001"]


def test_split_csv_strips_whitespace():
    assert _split_csv(" 0.001 , 0.002 ") == ["0.001", "0.002"]


def test_split_csv_list():
    assert _split_csv([0.001, 0.002]) == ["0.001", "0.002"]


def test_split_csv_empty():
    assert _split_csv("") == []
    assert _split_csv(None) == []


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def test_fmt_usd_small():
    assert _fmt_usd(123.45) == "$123.45"


def test_fmt_usd_thousands():
    assert _fmt_usd(8234) == "$8.2k"


def test_fmt_usd_millions():
    assert _fmt_usd(2_500_000) == "$2.5M"


def test_fmt_pct():
    assert _fmt_pct(0.873) == "87%"


def test_bar_full_when_ratio_one():
    assert _bar(1.0, width=5) == "▓" * 5


def test_bar_empty_when_ratio_zero():
    assert _bar(0.0, width=5) == "░" * 5


def test_bar_clamps_above_one():
    # Display safety: a 200% utilization still renders without overflow
    assert len(_bar(2.0, width=10)) == 10


# ---------------------------------------------------------------------------
# Severity ladder (must align with CAPITAL_DYNAMICS.md spec)
# ---------------------------------------------------------------------------


def test_severity_below_threshold_is_ok():
    assert _severity(0.5) == "ok"
    assert _severity(0.79) == "ok"


def test_severity_below_one_is_info():
    assert _severity(0.85) == "info"


def test_severity_above_one_is_warn():
    assert _severity(1.0) == "warn"
    assert _severity(1.4) == "warn"


def test_severity_above_one_and_a_half_is_crit():
    assert _severity(1.5) == "crit"
    assert _severity(3.0) == "crit"


# ---------------------------------------------------------------------------
# Per-controller block
# ---------------------------------------------------------------------------


def _sample_cfg(**overrides) -> dict:
    base = {
        "controller_name": "pmm_mister",
        "controller_type": "generic",
        "connector_name": "binance",
        "trading_pair": "BTC-USDT",
        "total_amount_quote": 1000.0,
        "portfolio_allocation": 0.03,
        "max_active_executors_by_level": 10,
        "buy_spreads": "0.0002,0.0006",
        "sell_spreads": "0.0002,0.0006",
        "target_base_pct": 0.5,
        "min_base_pct": 0.3,
        "max_base_pct": 0.7,
    }
    base.update(overrides)
    return base


def test_controller_block_capital_math():
    cfg = _sample_cfg()
    perf = {
        "positions_summary": [
            {"current_value": 12.0},
            {"current_value": 14.1},
        ]
    }
    block = _compute_controller_block("bot-1", "ctrl-1", cfg, perf)

    cap = block["capital"]
    # nominal = total_amount_quote * portfolio_allocation = 1000 * 0.03 = 30
    assert cap["nominal_budget_usd"] == 30.0
    # worst = nominal * max_executors = 30 * 10 = 300
    assert cap["worst_case_usd"] == 300.0
    # committed = sum(positions) = 26.1
    assert cap["committed_now_usd"] == 26.1
    # utilization_now = committed / nominal = 26.1 / 30 = 0.87
    assert abs(cap["utilization_now"] - 0.87) < 1e-9
    # utilization_vs_worst = 26.1 / 300 = 0.087
    assert abs(cap["utilization_vs_worst"] - 0.087) < 1e-9
    assert cap["active_executors_count"] == 2


def test_controller_block_canonical_id():
    cfg = _sample_cfg()
    block = _compute_controller_block("pmm-btc-1", "001_pmm_binance", cfg, None)
    assert block["id"] == "pmm-btc-1::001_pmm_binance"


def test_controller_block_buy_sell_levels():
    cfg = _sample_cfg(
        buy_spreads="0.0002,0.0004,0.0008",
        sell_spreads="0.0003,0.0006",
    )
    block = _compute_controller_block("bot", "ctrl", cfg, None)
    assert block["config"]["buy_levels"] == 3
    assert block["config"]["sell_levels"] == 2


def test_controller_block_handles_no_performance():
    cfg = _sample_cfg()
    block = _compute_controller_block("bot", "ctrl", cfg, None)
    assert block["capital"]["committed_now_usd"] == 0.0
    assert block["capital"]["active_executors_count"] == 0


def test_controller_block_inventory_in_range_true():
    cfg = _sample_cfg(total_amount_quote=100.0, portfolio_allocation=1.0)
    perf = {"positions_summary": [{"current_value": 50.0}]}  # 50% base, in [30%, 70%]
    block = _compute_controller_block("bot", "ctrl", cfg, perf)
    assert block["inventory"]["in_range"] is True
    assert abs(block["inventory"]["current_base_pct"] - 0.5) < 1e-9
    assert abs(block["inventory"]["drift_from_target"] - 0.0) < 1e-9


def test_controller_block_inventory_below_min():
    cfg = _sample_cfg(total_amount_quote=100.0, portfolio_allocation=1.0)
    perf = {"positions_summary": [{"current_value": 20.0}]}  # 20% base, below 30% min
    block = _compute_controller_block("bot", "ctrl", cfg, perf)
    assert block["inventory"]["in_range"] is False
    assert block["inventory"]["drift_from_target"] < 0


def test_controller_block_history_unavailable_in_mvp():
    cfg = _sample_cfg()
    block = _compute_controller_block("bot", "ctrl", cfg, None)
    assert block["history"]["available"] is False
    assert block["history"]["samples"] == 0


# ---------------------------------------------------------------------------
# Balances flattening
# ---------------------------------------------------------------------------


def test_flatten_balances_aggregates_across_accounts():
    raw = {
        "master_account": {
            "binance": [
                {"token": "BTC", "units": 0.1, "value": 6500.0},
                {"token": "USDT", "units": 5000.0, "value": 5000.0},
            ],
        },
        "subaccount_a": {
            "binance": [
                {"token": "BTC", "units": 0.05, "value": 3250.0},
            ],
        },
    }
    flat = _flatten_balances(raw)
    assert abs(flat["BTC"]["balance"] - 0.15) < 1e-9
    assert flat["BTC"]["value_usd"] == 9750.0
    assert flat["USDT"]["balance"] == 5000.0


def test_flatten_balances_handles_empty():
    assert _flatten_balances({}) == {}
    assert _flatten_balances(None) == {}


def test_flatten_balances_skips_malformed_entries():
    raw = {
        "master_account": {
            "binance": [
                {"token": "BTC", "units": 0.1, "value": 6500.0},
                "not a dict",  # malformed
                {"no_token": True},  # no token field
            ],
        },
    }
    flat = _flatten_balances(raw)
    assert list(flat.keys()) == ["BTC"]


# ---------------------------------------------------------------------------
# Global block — oversub detection
# ---------------------------------------------------------------------------


def test_global_block_no_oversub_when_supply_abundant():
    controllers = [
        _compute_controller_block(
            "bot",
            "c1",
            _sample_cfg(total_amount_quote=1000, portfolio_allocation=0.03),
            None,
        ),
    ]
    # Supply: BTC=$10k, USDT=$10k. Each controller demands 0.5 * worst (300) = $150 per leg.
    wallet = {
        "BTC": {"balance": 0.15, "value_usd": 10_000.0},
        "USDT": {"balance": 10_000.0, "value_usd": 10_000.0},
    }
    block = _compute_global_block(controllers, wallet, safety_margin=0.95)
    assert block["oversub_alerts"] == []
    assert "BTC" in block["wallet"]
    assert "USDT" in block["wallet"]


def test_global_block_detects_oversub():
    # 4 controllers × $300 worst × 0.5 split = $600 demand on BTC. Supply = $400.
    controllers = []
    for i in range(4):
        controllers.append(
            _compute_controller_block(
                f"bot-{i}",
                f"c{i}",
                _sample_cfg(total_amount_quote=1000, portfolio_allocation=0.03),
                None,
            )
        )
    wallet = {
        "BTC": {"balance": 0.006, "value_usd": 400.0},
        "USDT": {"balance": 400.0, "value_usd": 400.0},
    }
    block = _compute_global_block(controllers, wallet, safety_margin=0.95)
    assert len(block["oversub_alerts"]) == 2  # both BTC and USDT
    btc_alert = next(a for a in block["oversub_alerts"] if a["asset"] == "BTC")
    assert btc_alert["ratio"] == 1.5  # 600 / 400
    assert btc_alert["severity"] == "crit"
    assert len(btc_alert["controllers"]) == 4


def test_global_block_lists_controllers_per_asset():
    c1 = _compute_controller_block(
        "bot",
        "c1",
        _sample_cfg(trading_pair="BTC-USDT"),
        None,
    )
    c2 = _compute_controller_block(
        "bot",
        "c2",
        _sample_cfg(trading_pair="BTC-BRL"),
        None,
    )
    wallet = {
        "BTC": {"balance": 1.0, "value_usd": 100_000.0},
        "USDT": {"balance": 100_000.0, "value_usd": 100_000.0},
        "BRL": {"balance": 500_000.0, "value_usd": 100_000.0},
    }
    block = _compute_global_block([c1, c2], wallet, safety_margin=0.95)
    btc_using = block["wallet"]["BTC"]["controllers_using"]
    assert "bot::c1" in btc_using
    assert "bot::c2" in btc_using
    assert block["wallet"]["USDT"]["controllers_using"] == ["bot::c1"]
    assert block["wallet"]["BRL"]["controllers_using"] == ["bot::c2"]


# ---------------------------------------------------------------------------
# Summary aggregation
# ---------------------------------------------------------------------------


def test_summary_aggregates_totals():
    controllers = [
        _compute_controller_block(
            "bot",
            "c1",
            _sample_cfg(total_amount_quote=1000, portfolio_allocation=0.03),
            {"positions_summary": [{"current_value": 20.0}]},
        ),
        _compute_controller_block(
            "bot",
            "c2",
            _sample_cfg(total_amount_quote=2000, portfolio_allocation=0.05),
            {"positions_summary": [{"current_value": 50.0}]},
        ),
    ]
    global_block = {"oversub_alerts": []}
    summary = _compute_summary(controllers, global_block, wallet_total=10_000.0)

    assert summary["total_controllers"] == 2
    assert summary["total_nominal_budget_usd"] == 30.0 + 100.0
    assert summary["total_committed_now_usd"] == 70.0
    assert summary["total_worst_case_usd"] == 300.0 + 1000.0
    assert summary["wallet_total_value_usd"] == 10_000.0
    assert summary["any_oversub"] is False
    assert summary["max_oversub_ratio"] == 0.0


def test_summary_picks_max_oversub_ratio():
    global_block = {
        "oversub_alerts": [
            {"asset": "BTC", "ratio": 1.2, "severity": "warn", "controllers": []},
            {"asset": "USDT", "ratio": 1.8, "severity": "crit", "controllers": []},
        ]
    }
    summary = _compute_summary([], global_block, wallet_total=0.0)
    assert summary["any_oversub"] is True
    assert summary["max_oversub_ratio"] == 1.8
