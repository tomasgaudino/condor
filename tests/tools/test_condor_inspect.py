"""Tests for condor.tools.condor_inspect — pure helpers only.

The async helpers (with_client, list_active_bots, get_controller_state,
get_wallet_state, get_prices) require a live Hummingbot server and are
exercised manually with the script snippets shown in the SKILL.md of the
``condor-express`` skill.

This file covers only the pure helpers that don't touch the network:
- flatten_balances
- format_controller_summary
"""

from __future__ import annotations

from condor.tools.condor_inspect import (
    flatten_balances,
    format_controller_summary,
)


# ---------------------------------------------------------------------------
# flatten_balances
# ---------------------------------------------------------------------------


def test_flatten_balances_aggregates_across_accounts_and_connectors():
    raw = {
        "master_account": {
            "binance": [
                {"token": "BTC", "units": 0.1, "value": 6500.0},
                {"token": "USDT", "units": 5000.0, "value": 5000.0},
            ],
            "kraken": [
                {"token": "BTC", "units": 0.05, "value": 3250.0},
            ],
        },
        "subaccount_a": {
            "binance": [
                {"token": "BTC", "units": 0.02, "value": 1300.0},
            ],
        },
    }
    out = flatten_balances(raw)
    # BTC aggregates: 0.1 + 0.05 + 0.02 = 0.17, value 6500 + 3250 + 1300 = 11050
    assert abs(out["BTC"]["balance"] - 0.17) < 1e-9
    assert out["BTC"]["value_usd"] == 11050.0
    assert out["USDT"]["balance"] == 5000.0


def test_flatten_balances_empty_input():
    assert flatten_balances({}) == {}
    assert flatten_balances(None) == {}


def test_flatten_balances_skips_malformed_holdings():
    raw = {
        "acc": {
            "binance": [
                {"token": "BTC", "units": 0.1, "value": 6500.0},
                "garbage_string",
                {"no_token_field": True},
                None,
            ],
        },
    }
    out = flatten_balances(raw)
    assert list(out.keys()) == ["BTC"]


def test_flatten_balances_falls_back_to_amount_if_units_missing():
    # Some shapes use 'amount' instead of 'units'.
    raw = {
        "acc": {
            "binance": [
                {"token": "BTC", "amount": 0.5, "value": 32500.0},
            ],
        },
    }
    out = flatten_balances(raw)
    assert out["BTC"]["balance"] == 0.5


def test_flatten_balances_supports_symbol_field():
    # Some shapes use 'symbol' instead of 'token'.
    raw = {
        "acc": {
            "binance": [
                {"symbol": "ETH", "units": 1.0, "value": 3500.0},
            ],
        },
    }
    out = flatten_balances(raw)
    assert "ETH" in out


# ---------------------------------------------------------------------------
# format_controller_summary
# ---------------------------------------------------------------------------


def _sample_state(**overrides) -> dict:
    base = {
        "bot_name": "pmm-btc-1",
        "config_name": "001_pmm_binance_BTC-USDT",
        "config": {
            "trading_pair": "BTC-USDT",
            "connector_name": "binance",
            "controller_name": "pmm_mister",
            "controller_type": "generic",
            "total_amount_quote": 1000,
            "portfolio_allocation": 0.03,
            "max_active_executors_by_level": 10,
            "target_base_pct": 0.5,
            "min_base_pct": 0.3,
            "max_base_pct": 0.7,
            "take_profit": 0.0003,
            "buy_spreads": "0.0002,0.0006",
            "sell_spreads": "0.0002,0.0006",
        },
        "performance": {"realized_pnl_quote": 1.0},
        "positions_summary": [
            {"side": "BUY", "amount": 0.001, "breakeven_price": 80000.0,
             "unrealized_pnl_quote": 0.5},
        ],
        "computed": {
            "nominal_budget_usd": 30.0,
            "worst_case_usd": 300.0,
            "committed_now_usd": 80.0,
            "active_executors": 1,
            "utilization_now": 80.0 / 30.0,
        },
    }
    base.update(overrides)
    return base


def test_format_controller_summary_includes_canonical_fields():
    out = format_controller_summary(_sample_state())
    assert "001_pmm_binance_BTC-USDT" in out
    assert "BTC-USDT on binance" in out
    assert "pmm_mister (generic)" in out
    assert "total_amount_quote:" in out
    assert "portfolio_allocation:" in out


def test_format_controller_summary_renders_computed_metrics():
    out = format_controller_summary(_sample_state())
    assert "nominal_budget_usd: 30.00" in out
    assert "worst_case_usd:     300.00" in out
    assert "committed_now_usd:  80.00" in out
    assert "266.7%" in out  # utilization_now * 100 = 266.67


def test_format_controller_summary_lists_positions():
    out = format_controller_summary(_sample_state())
    assert "BUY" in out
    assert "amount=0.001" in out
    assert "breakeven=80000.0" in out
    assert "unrealized_pnl=0.5" in out


def test_format_controller_summary_no_positions():
    state = _sample_state(positions_summary=[],
                          computed={"nominal_budget_usd": 30.0,
                                    "worst_case_usd": 300.0,
                                    "committed_now_usd": 0.0,
                                    "active_executors": 0,
                                    "utilization_now": 0.0})
    out = format_controller_summary(state)
    assert "POSITIONS" in out
    assert "(none)" in out


def test_format_controller_summary_handles_error_state():
    state = {"error": "config not found", "available_configs": ["a", "b", "c"]}
    out = format_controller_summary(state)
    assert "ERROR" in out
    assert "config not found" in out
    assert "['a', 'b', 'c']" in out
