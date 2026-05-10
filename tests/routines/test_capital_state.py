"""Tests for routines/capital_state.py — pure-function building blocks.

Covers the deterministic computation pieces (parsing, formatting, capital
math, oversub detection, balance flattening). The async `run()` requires a
live Condor client and is exercised manually from `/routines` in Telegram.
"""

from __future__ import annotations

from routines.capital_state import (
    GLOSSARY,
    _bar,
    _build_agent_payload,
    _build_controllers_table,
    _build_glossary_markdown,
    _build_kpi_sections,
    _build_narrative,
    _compute_controller_block,
    _compute_global_block,
    _compute_summary,
    _flatten_balances,
    _fmt_pct,
    _fmt_usd,
    _parse_pair,
    _render_compact_summary,
    _resolve_perf,
    _select_glossary_terms,
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


# ---------------------------------------------------------------------------
# Compact summary (Telegram preview safety)
# ---------------------------------------------------------------------------


def _empty_payload() -> dict:
    return {
        "ts": "2026-01-01T00:00:00Z",
        "account": "all",
        "global": {"wallet": {}, "oversub_alerts": []},
        "controllers": [],
        "summary": {
            "total_controllers": 0,
            "total_nominal_budget_usd": 0.0,
            "total_committed_now_usd": 0.0,
            "total_worst_case_usd": 0.0,
            "wallet_total_value_usd": 0.0,
            "any_oversub": False,
            "max_oversub_ratio": 0.0,
        },
    }


def _payload_with_n_controllers(n: int, **overrides) -> dict:
    """Build a payload with N synthetic controllers for summary tests."""
    p = _empty_payload()
    p["summary"]["total_controllers"] = n
    p["controllers"] = [
        {
            "config_name": f"ctrl-{i}",
            "bot_name": "bot-x",
            "trading_pair": "BTC-USDT",
            "diagnostic": {"stuck_suspect": False},
            "capital": {
                "utilization_now": 0.5,
                "nominal_budget_usd": 30.0,
                "committed_now_usd": 15.0,
                "worst_case_usd": 300.0,
                "active_executors_count": 0,
            },
            "config": {
                "portfolio_allocation": 0.03,
                "max_active_executors_by_level": 10,
                "buy_levels": 2,
                "sell_levels": 2,
                "take_profit": 0.0003,
            },
        }
        for i in range(n)
    ]
    for k, v in overrides.items():
        p["summary"][k] = v
    return p


def test_compact_summary_fits_telegram_preview():
    """Compact summary must fit inside the 250-char detail-view truncation."""
    payload = _payload_with_n_controllers(42)
    payload["global"]["wallet"] = {
        f"TKN{i}": {"value_usd": 1000.0, "headroom_usd": -500.0} for i in range(10)
    }
    payload["global"]["oversub_alerts"] = [
        {"asset": "BTC", "ratio": 2.5, "severity": "crit"}
    ]
    text = _render_compact_summary(payload)
    assert len(text) <= 220, f"summary too long: {len(text)} chars"


def test_compact_summary_has_no_triple_backticks():
    """Triple backticks would break the surrounding code fence in the UI."""
    payload = _payload_with_n_controllers(3)
    text = _render_compact_summary(payload)
    assert "```" not in text


def test_compact_summary_helpful_when_no_controllers():
    """Empty result must still convey something useful (not just headers)."""
    text = _render_compact_summary(_empty_payload())
    assert "no controllers matched" in text.lower()


def test_compact_summary_shows_controller_count_and_headroom():
    """The one-line format leads with controller count and headroom."""
    payload = _payload_with_n_controllers(14)
    payload["global"]["wallet"] = {
        "BTC": {"value_usd": 50000.0, "headroom_usd": 1000.0},
        "USDT": {"value_usd": 23000.0, "headroom_usd": -1200.0},
    }
    text = _render_compact_summary(payload)
    assert "14 ctrl" in text
    assert "headroom" in text.lower()
    # Headroom is summed across assets: 1000 + (-1200) = -200
    assert "-" in text  # negative headroom is rendered


def test_compact_summary_includes_stuck_count_when_present():
    payload = _payload_with_n_controllers(3)
    payload["controllers"][0]["diagnostic"]["stuck_suspect"] = True
    payload["controllers"][2]["diagnostic"]["stuck_suspect"] = True
    text = _render_compact_summary(payload)
    assert "2 stuck?" in text


def test_compact_summary_omits_stuck_when_zero():
    payload = _payload_with_n_controllers(3)
    text = _render_compact_summary(payload)
    assert "stuck" not in text.lower()


def test_compact_summary_includes_alert_count_when_present():
    payload = _payload_with_n_controllers(3)
    payload["global"]["oversub_alerts"] = [
        {"asset": "BTC", "ratio": 1.5, "severity": "warn"},
        {"asset": "USDT", "ratio": 2.5, "severity": "crit"},
    ]
    text = _render_compact_summary(payload)
    assert "2 alerts" in text


def test_compact_summary_omits_alerts_segment_when_zero():
    payload = _payload_with_n_controllers(3)
    text = _render_compact_summary(payload)
    assert "alert" not in text.lower()


# ---------------------------------------------------------------------------
# Performance-resolver helper (_resolve_perf)
# ---------------------------------------------------------------------------


def test_resolve_perf_matches_by_config_name():
    perf_by_id = {
        "001_pmm_binance": {"performance": {"connector_name": "binance",
                                              "trading_pair": "BTC-USDT",
                                              "positions_summary": [{"current_value": 5.0}]}},
    }
    cfg = {"_config_name": "001_pmm_binance",
           "connector_name": "binance",
           "trading_pair": "BTC-USDT"}
    out = _resolve_perf(perf_by_id, cfg)
    assert out is not None
    assert out["positions_summary"][0]["current_value"] == 5.0


def test_resolve_perf_falls_back_to_connector_pair_match():
    # Direct keys don't match — must find by (connector, pair).
    perf_by_id = {
        "some-uuid-xyz": {"performance": {"connector_name": "binance",
                                            "trading_pair": "ETH-USDT",
                                            "positions_summary": [{"current_value": 9.0}]}},
    }
    cfg = {"_config_name": "different_name.yml",
           "id": "another_id",
           "connector_name": "binance",
           "trading_pair": "ETH-USDT"}
    out = _resolve_perf(perf_by_id, cfg)
    assert out is not None
    assert out["positions_summary"][0]["current_value"] == 9.0


def test_resolve_perf_returns_none_when_no_match():
    perf_by_id = {
        "x": {"performance": {"connector_name": "kucoin",
                                "trading_pair": "SOL-USDT"}},
    }
    cfg = {"_config_name": "abc",
           "connector_name": "binance",
           "trading_pair": "BTC-USDT"}
    assert _resolve_perf(perf_by_id, cfg) is None


def test_resolve_perf_handles_unwrapped_entry():
    # Some MQTT versions don't wrap under "performance".
    perf_by_id = {
        "ctrl-1": {"connector_name": "binance",
                    "trading_pair": "BTC-USDT",
                    "positions_summary": [{"current_value": 3.0}]},
    }
    cfg = {"_config_name": "ctrl-1"}
    out = _resolve_perf(perf_by_id, cfg)
    assert out is not None
    assert out["positions_summary"][0]["current_value"] == 3.0


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


# ---------------------------------------------------------------------------
# positions_summary real shape (amount × breakeven_price)
# ---------------------------------------------------------------------------


def test_controller_block_committed_from_amount_times_breakeven():
    """Real Hummingbot shape: amount (base) × breakeven_price (quote) = notional USD."""
    cfg = _sample_cfg(total_amount_quote=7000.0, portfolio_allocation=0.03)
    perf = {
        "positions_summary": [
            # BTC long: 0.001 BTC @ 80_000 USDT → $80 committed
            {"amount": 0.001, "breakeven_price": 80_000.0, "side": "BUY",
             "unrealized_pnl_quote": 1.5},
        ]
    }
    block = _compute_controller_block("bot", "ctrl", cfg, perf)
    cap = block["capital"]
    assert abs(cap["committed_now_usd"] - 80.0) < 1e-6
    assert cap["active_executors_count"] == 1


def test_controller_block_committed_sums_multiple_positions():
    """Multiple open positions are summed correctly."""
    cfg = _sample_cfg(total_amount_quote=7000.0, portfolio_allocation=0.05)
    perf = {
        "positions_summary": [
            {"amount": 0.001, "breakeven_price": 80_000.0, "side": "BUY",
             "unrealized_pnl_quote": 1.0},
            {"amount": 0.0005, "breakeven_price": 79_500.0, "side": "SELL",
             "unrealized_pnl_quote": -0.5},
        ]
    }
    block = _compute_controller_block("bot", "ctrl", cfg, perf)
    # 0.001 × 80_000 + 0.0005 × 79_500 = 80.0 + 39.75 = 119.75
    assert abs(block["capital"]["committed_now_usd"] - 119.75) < 1e-6
    assert block["capital"]["active_executors_count"] == 2


def test_controller_block_falls_back_to_current_value_when_amount_zero():
    """Legacy shape: no amount/breakeven → fall back to current_value field."""
    cfg = _sample_cfg()
    perf = {"positions_summary": [{"current_value": 42.0}]}
    block = _compute_controller_block("bot", "ctrl", cfg, perf)
    assert abs(block["capital"]["committed_now_usd"] - 42.0) < 1e-6


# ---------------------------------------------------------------------------
# stuck_suspect heuristic
# ---------------------------------------------------------------------------


def test_stuck_suspect_true_when_tight_tp_and_position_in_profit():
    """take_profit ≤ 5bps + at least one position at/above breakeven."""
    cfg = _sample_cfg(take_profit=0.0001)  # 1bp — ultra tight
    perf = {
        "positions_summary": [
            {
                "amount": 0.01,
                "breakeven_price": 80000.0,
                "unrealized_pnl_quote": 5.0,  # in profit, hasn't closed
            }
        ]
    }
    block = _compute_controller_block("bot", "ctrl", cfg, perf)
    assert block["diagnostic"]["stuck_suspect"] is True
    assert block["diagnostic"]["positions_at_or_above_breakeven"] == 1


def test_stuck_suspect_false_when_position_underwater():
    cfg = _sample_cfg(take_profit=0.0001)
    perf = {
        "positions_summary": [
            {
                "amount": 0.01,
                "breakeven_price": 80000.0,
                "unrealized_pnl_quote": -3.0,  # underwater — TP was never hit
            }
        ]
    }
    block = _compute_controller_block("bot", "ctrl", cfg, perf)
    assert block["diagnostic"]["stuck_suspect"] is False


def test_stuck_suspect_false_when_tp_is_relaxed():
    cfg = _sample_cfg(take_profit=0.001)  # 10bp — comfortable
    perf = {
        "positions_summary": [
            {"amount": 0.01, "breakeven_price": 80000.0, "unrealized_pnl_quote": 5.0}
        ]
    }
    block = _compute_controller_block("bot", "ctrl", cfg, perf)
    assert block["diagnostic"]["stuck_suspect"] is False


def test_stuck_suspect_false_when_no_positions():
    cfg = _sample_cfg(take_profit=0.0001)
    block = _compute_controller_block("bot", "ctrl", cfg, None)
    assert block["diagnostic"]["stuck_suspect"] is False


# ---------------------------------------------------------------------------
# KPI sections — for the web UI's KpiBar component
# ---------------------------------------------------------------------------


def test_kpi_sections_contain_four_cards():
    payload = _payload_with_n_controllers(3)
    kpis = _build_kpi_sections(payload)
    labels = [k["label"] for k in kpis]
    assert labels == ["CONTROLLERS", "CAPITAL", "HEADROOM", "ALERTS"]


def test_kpi_sections_all_have_kpi_type():
    payload = _payload_with_n_controllers(3)
    for k in _build_kpi_sections(payload):
        assert k["type"] == "kpi"


def test_kpi_sections_negative_headroom_marks_trend_down():
    payload = _payload_with_n_controllers(3)
    payload["global"]["wallet"] = {
        "BTC": {"value_usd": 1000.0, "headroom_usd": -500.0},
    }
    kpis = _build_kpi_sections(payload)
    headroom_kpi = next(k for k in kpis if k["label"] == "HEADROOM")
    assert headroom_kpi["trend"] == "down"
    assert "over-committed" in headroom_kpi["delta"].lower()


def test_kpi_sections_alerts_card_shows_worst_when_present():
    payload = _payload_with_n_controllers(2)
    payload["global"]["oversub_alerts"] = [
        {"asset": "BTC", "ratio": 1.5, "severity": "warn"},
        {"asset": "USDT", "ratio": 2.7, "severity": "crit"},
    ]
    kpis = _build_kpi_sections(payload)
    alerts_kpi = next(k for k in kpis if k["label"] == "ALERTS")
    assert alerts_kpi["value"] == "2 oversub"
    assert "USDT" in alerts_kpi["delta"]  # the worst one
    assert alerts_kpi["trend"] == "down"  # has crit


def test_kpi_sections_alerts_card_clean_when_no_alerts():
    payload = _payload_with_n_controllers(2)
    kpis = _build_kpi_sections(payload)
    alerts_kpi = next(k for k in kpis if k["label"] == "ALERTS")
    assert alerts_kpi["value"] == "0"
    assert alerts_kpi["trend"] == "up"


def test_kpi_sections_capital_card_marks_trend_down_above_nominal():
    payload = _payload_with_n_controllers(1)
    payload["summary"]["total_committed_now_usd"] = 200.0
    payload["summary"]["total_nominal_budget_usd"] = 100.0  # 200% of nominal
    kpis = _build_kpi_sections(payload)
    cap_kpi = next(k for k in kpis if k["label"] == "CAPITAL")
    assert cap_kpi["trend"] == "down"


# ---------------------------------------------------------------------------
# Controllers table — for the web UI's table renderer
# ---------------------------------------------------------------------------


def test_controllers_table_columns_are_stable():
    payload = _payload_with_n_controllers(2)
    cols, _rows = _build_controllers_table(payload)
    expected = ["controller", "bot", "pair", "committed", "nominal",
                "util%", "tp_bps", "pos", "flag"]
    assert cols == expected


def test_controllers_table_one_row_per_controller():
    payload = _payload_with_n_controllers(5)
    _cols, rows = _build_controllers_table(payload)
    assert len(rows) == 5


def test_controllers_table_sorted_by_utilization_descending():
    payload = _payload_with_n_controllers(3)
    payload["controllers"][0]["capital"]["utilization_now"] = 0.2
    payload["controllers"][1]["capital"]["utilization_now"] = 0.9
    payload["controllers"][2]["capital"]["utilization_now"] = 0.5
    _cols, rows = _build_controllers_table(payload)
    utils = [r["util%"] for r in rows]
    assert utils == sorted(utils, reverse=True)


def test_controllers_table_renders_tp_in_bps():
    """take_profit is a fraction (0.0003 = 3bp). Table column shows bps."""
    payload = _payload_with_n_controllers(1)
    payload["controllers"][0]["config"]["take_profit"] = 0.0003
    _cols, rows = _build_controllers_table(payload)
    assert rows[0]["tp_bps"] == 3.0


def test_controllers_table_flag_set_for_stuck_suspect():
    payload = _payload_with_n_controllers(2)
    payload["controllers"][0]["diagnostic"]["stuck_suspect"] = True
    payload["controllers"][1]["diagnostic"]["stuck_suspect"] = False
    _cols, rows = _build_controllers_table(payload)
    # Sorted by util% — both 50% by default — order may not match input.
    flags = sorted(r["flag"] for r in rows)
    assert flags == ["", "stuck?"]


def test_controllers_table_handles_empty():
    payload = _empty_payload()
    cols, rows = _build_controllers_table(payload)
    assert rows == []
    assert len(cols) > 0  # columns are stable even with no rows


# ---------------------------------------------------------------------------
# Narrative summary — deterministic prose
# ---------------------------------------------------------------------------


def test_narrative_handles_no_controllers():
    """Empty scope must say so explicitly, not produce a misleading green status."""
    text = _build_narrative(_empty_payload())
    assert "Sin controllers" in text or "sin controllers" in text.lower()


def test_narrative_green_when_healthy():
    """No alerts + ample headroom + no stuck → 🟢."""
    payload = _payload_with_n_controllers(4)
    payload["summary"]["wallet_total_value_usd"] = 10_000.0
    payload["global"]["wallet"] = {
        "USDT": {"value_usd": 10_000.0, "headroom_usd": 9_000.0}
    }
    text = _build_narrative(payload)
    assert "🟢" in text
    assert "estable" in text.lower()


def test_narrative_red_when_negative_headroom():
    """Negative headroom → 🔴 + recomendación de reducir exposición."""
    payload = _payload_with_n_controllers(3)
    payload["summary"]["wallet_total_value_usd"] = 1_000.0
    payload["global"]["wallet"] = {
        "USDT": {"value_usd": 1_000.0, "headroom_usd": -200.0}
    }
    text = _build_narrative(payload)
    assert "🔴" in text
    assert "reducir exposición" in text.lower() or "reducir exposicion" in text.lower()


def test_narrative_red_when_critical_alert_present():
    """A crit-severity alert escalates the status to 🔴 even with positive headroom."""
    payload = _payload_with_n_controllers(3)
    payload["summary"]["wallet_total_value_usd"] = 10_000.0
    payload["global"]["wallet"] = {
        "USDT": {"value_usd": 10_000.0, "headroom_usd": 5_000.0}
    }
    payload["global"]["oversub_alerts"] = [
        {"asset": "BTC", "ratio": 2.5, "severity": "crit", "controllers": []}
    ]
    text = _build_narrative(payload)
    assert "🔴" in text
    assert "BTC" in text
    assert "2.5" in text


def test_narrative_yellow_when_warn_alert():
    """warn but no crit → 🟡 (atención)."""
    payload = _payload_with_n_controllers(3)
    payload["summary"]["wallet_total_value_usd"] = 10_000.0
    payload["global"]["wallet"] = {
        "USDT": {"value_usd": 10_000.0, "headroom_usd": 5_000.0}
    }
    payload["global"]["oversub_alerts"] = [
        {"asset": "BTC", "ratio": 1.2, "severity": "warn", "controllers": []}
    ]
    text = _build_narrative(payload)
    assert "🟡" in text


def test_narrative_mentions_stuck_controller_by_name_when_only_one():
    payload = _payload_with_n_controllers(3)
    payload["summary"]["wallet_total_value_usd"] = 10_000.0
    payload["global"]["wallet"] = {
        "USDT": {"value_usd": 10_000.0, "headroom_usd": 5_000.0}
    }
    payload["controllers"][2]["diagnostic"]["stuck_suspect"] = True
    text = _build_narrative(payload)
    assert "atascado" in text.lower()
    assert "ctrl-2" in text  # the specific controller name


def test_narrative_aggregates_when_multiple_stuck():
    payload = _payload_with_n_controllers(5)
    payload["summary"]["wallet_total_value_usd"] = 10_000.0
    payload["global"]["wallet"] = {
        "USDT": {"value_usd": 10_000.0, "headroom_usd": 5_000.0}
    }
    for i in (0, 2, 4):
        payload["controllers"][i]["diagnostic"]["stuck_suspect"] = True
    text = _build_narrative(payload)
    assert "3 controllers" in text and "atascados" in text.lower()


# ---------------------------------------------------------------------------
# Glossary selection + rendering
# ---------------------------------------------------------------------------


def test_glossary_basic_terms_always_included():
    """Even on a healthy system, the four basics are present."""
    payload = _payload_with_n_controllers(2)
    terms = _select_glossary_terms(payload)
    for required in ("controller", "nominal", "committed", "headroom"):
        assert required in terms


def test_glossary_omits_oversub_when_no_alerts():
    payload = _payload_with_n_controllers(2)
    terms = _select_glossary_terms(payload)
    assert "oversubscription" not in terms
    assert "severity" not in terms


def test_glossary_includes_oversub_when_alerts_present():
    payload = _payload_with_n_controllers(2)
    payload["global"]["oversub_alerts"] = [
        {"asset": "BTC", "ratio": 1.2, "severity": "warn", "controllers": []}
    ]
    terms = _select_glossary_terms(payload)
    assert "oversubscription" in terms
    assert "severity" in terms


def test_glossary_omits_stuck_when_no_stuck_controllers():
    payload = _payload_with_n_controllers(3)
    terms = _select_glossary_terms(payload)
    assert "stuck" not in terms
    assert "take_profit" not in terms


def test_glossary_includes_stuck_when_any_controller_flagged():
    payload = _payload_with_n_controllers(3)
    payload["controllers"][1]["diagnostic"]["stuck_suspect"] = True
    terms = _select_glossary_terms(payload)
    assert "stuck" in terms
    assert "take_profit" in terms


def test_glossary_includes_worst_case_when_any_above_nominal():
    payload = _payload_with_n_controllers(2)
    # default _payload_with_n_controllers gives worst=300, nominal=30 → trigger
    terms = _select_glossary_terms(payload)
    assert "worst_case" in terms


def test_glossary_omits_worst_case_when_nominal_equals_worst():
    payload = _payload_with_n_controllers(1)
    payload["controllers"][0]["capital"]["worst_case_usd"] = 30.0  # = nominal
    terms = _select_glossary_terms(payload)
    assert "worst_case" not in terms


def test_glossary_markdown_renders_only_selected_terms():
    payload = _payload_with_n_controllers(2)  # healthy → minimal glossary
    md = _build_glossary_markdown(payload)
    # Should include basics
    assert "**Controller**" in md
    assert "**Nominal**" in md
    # Should NOT include conditional ones
    assert "**Oversubscription**" not in md
    assert "**Stuck**" not in md


def test_glossary_keys_all_have_definitions():
    """Sanity: every key referenced by _select_glossary_terms exists in GLOSSARY."""
    # Force selection of every possible term
    payload = _payload_with_n_controllers(1)
    payload["controllers"][0]["diagnostic"]["stuck_suspect"] = True
    payload["global"]["oversub_alerts"] = [
        {"asset": "X", "ratio": 1.0, "severity": "warn", "controllers": []}
    ]
    terms = _select_glossary_terms(payload)
    for t in terms:
        assert t in GLOSSARY, f"glossary missing definition for {t!r}"


# ---------------------------------------------------------------------------
# Agent-facing payload — the contract the adaptive agent's LLM consumes
# ---------------------------------------------------------------------------


def _full_payload_with_one_stuck_oversub() -> dict:
    """Realistic payload covering all branches the agent payload exercises."""
    return {
        "ts": "2026-05-10T05:00:00Z",
        "summary": {
            "total_controllers": 2,
            "total_committed_now_usd": 1600.0,
            "wallet_total_value_usd": 10_000.0,
            "any_oversub": True,
            "max_oversub_ratio": 1.4,
        },
        "global": {
            "wallet": {
                "BTC": {
                    "balance": 0.1,
                    "value_usd": 6500.0,
                    "used_now_usd": 100.0,
                    "headroom_usd": 6400.0,
                    "controllers_using": [],
                },
                "USDT": {
                    "balance": 3500.0,
                    "value_usd": 3500.0,
                    "used_now_usd": 100.0,
                    "headroom_usd": 3400.0,
                    "controllers_using": [],
                },
            },
            "oversub_alerts": [
                {
                    "asset": "BTC",
                    "ratio": 1.4,
                    "severity": "warn",
                    "controllers": ["bot::c1"],
                },
            ],
        },
        "controllers": [
            {
                "id": "bot::c1",
                "config_name": "c1",
                "bot_name": "bot",
                "trading_pair": "BTC-USDT",
                "base_asset": "BTC",
                "quote_asset": "USDT",
                "connector_name": "binance",
                "capital": {
                    "nominal_budget_usd": 30.0,
                    "committed_now_usd": 1500.0,
                    "worst_case_usd": 300.0,
                    "utilization_now": 50.0,
                    "utilization_vs_worst": 5.0,
                    "active_executors_count": 1,
                },
                "config": {
                    "total_amount_quote": 1000.0,
                    "portfolio_allocation": 0.03,
                    "max_active_executors_by_level": 10,
                    "buy_levels": 2,
                    "sell_levels": 2,
                    "take_profit": 0.0001,
                },
                "inventory": {
                    "current_base_pct": 0.5,
                    "target_base_pct": 0.5,
                    "min_base_pct": 0.3,
                    "max_base_pct": 0.7,
                    "in_range": True,
                    "drift_from_target": 0.0,
                },
                "diagnostic": {
                    "stuck_suspect": True,
                    "positions_at_or_above_breakeven": 1,
                },
                "history": {"lookback_hours": 24, "available": False, "samples": 0},
            },
            {
                "id": "bot::c2",
                "config_name": "c2",
                "bot_name": "bot",
                "trading_pair": "ETH-USDT",
                "base_asset": "ETH",
                "quote_asset": "USDT",
                "connector_name": "binance",
                "capital": {
                    "nominal_budget_usd": 50.0,
                    "committed_now_usd": 100.0,
                    "worst_case_usd": 500.0,
                    "utilization_now": 2.0,
                    "utilization_vs_worst": 0.2,
                    "active_executors_count": 0,
                },
                "config": {
                    "total_amount_quote": 1000.0,
                    "portfolio_allocation": 0.05,
                    "max_active_executors_by_level": 10,
                    "buy_levels": 2,
                    "sell_levels": 2,
                    "take_profit": 0.001,
                },
                "inventory": {
                    "current_base_pct": 0.3,
                    "target_base_pct": 0.5,
                    "min_base_pct": 0.3,
                    "max_base_pct": 0.7,
                    "in_range": True,
                    "drift_from_target": -0.2,
                },
                "diagnostic": {
                    "stuck_suspect": False,
                    "positions_at_or_above_breakeven": 0,
                },
                "history": {"lookback_hours": 24, "available": False, "samples": 0},
            },
        ],
    }


def test_agent_payload_has_top_level_keys():
    """Agent contract: ts, controllers, wallet, oversub_alerts, totals."""
    out = _build_agent_payload(_full_payload_with_one_stuck_oversub())
    assert set(out.keys()) == {"ts", "controllers", "wallet", "oversub_alerts", "totals"}


def test_agent_payload_drops_human_decorations():
    """Inventory plane, history, connector_name, etc. are NOT in the agent view."""
    out = _build_agent_payload(_full_payload_with_one_stuck_oversub())
    c = out["controllers"][0]
    # Per-controller agent fields
    assert set(c.keys()) == {"id", "trading_pair", "base_asset", "quote_asset",
                             "capital", "config", "diagnostic"}
    # No inventory or history
    assert "inventory" not in c
    assert "history" not in c
    assert "connector_name" not in c
    assert "config_name" not in c
    assert "bot_name" not in c


def test_agent_payload_capital_fields_minimal():
    out = _build_agent_payload(_full_payload_with_one_stuck_oversub())
    cap = out["controllers"][0]["capital"]
    assert set(cap.keys()) == {"nominal_budget_usd", "committed_now_usd",
                                "worst_case_usd", "utilization_now"}
    # No utilization_vs_worst (redundant for the agent)
    assert "utilization_vs_worst" not in cap


def test_agent_payload_config_only_actionable_fields():
    """The agent only needs fields it can reason about modifying."""
    out = _build_agent_payload(_full_payload_with_one_stuck_oversub())
    cfg = out["controllers"][0]["config"]
    assert set(cfg.keys()) == {"take_profit", "max_active_executors_by_level",
                                "portfolio_allocation"}


def test_agent_payload_diagnostic_is_compact():
    out = _build_agent_payload(_full_payload_with_one_stuck_oversub())
    diag = out["controllers"][0]["diagnostic"]
    assert set(diag.keys()) == {"stuck_suspect", "active_executors"}
    assert diag["stuck_suspect"] is True
    assert diag["active_executors"] == 1


def test_agent_payload_wallet_only_value_and_headroom():
    """No `balance`, no `used_now_usd`, no `controllers_using` in the agent view."""
    out = _build_agent_payload(_full_payload_with_one_stuck_oversub())
    btc = out["wallet"]["BTC"]
    assert set(btc.keys()) == {"value_usd", "headroom_usd"}


def test_agent_payload_preserves_oversub_alerts_with_controller_list():
    """Cross-references between alerts and controllers MUST be preserved —
    they're how the agent knows which controllers a given asset oversub
    affects."""
    out = _build_agent_payload(_full_payload_with_one_stuck_oversub())
    assert len(out["oversub_alerts"]) == 1
    a = out["oversub_alerts"][0]
    assert a["asset"] == "BTC"
    assert a["ratio"] == 1.4
    assert a["severity"] == "warn"
    assert a["controllers"] == ["bot::c1"]


def test_agent_payload_totals_aggregated_once():
    """Totals are pre-computed so the agent doesn't re-aggregate."""
    out = _build_agent_payload(_full_payload_with_one_stuck_oversub())
    t = out["totals"]
    assert t["controllers"] == 2
    assert t["committed_usd"] == 1600.0
    assert t["wallet_usd"] == 10_000.0
    # 6400 + 3400 = 9800 (sum of headroom_usd across wallet entries)
    assert t["headroom_usd"] == 9800.0
    assert t["stuck_count"] == 1


def test_agent_payload_handles_empty_payload_gracefully():
    out = _build_agent_payload({})
    assert out["controllers"] == []
    assert out["wallet"] == {}
    assert out["oversub_alerts"] == []
    assert out["totals"]["controllers"] == 0
    assert out["totals"]["headroom_usd"] == 0.0
    assert out["totals"]["stuck_count"] == 0


def test_agent_payload_size_fits_llm_context():
    """Sanity check: the agent view stays small even at scale."""
    import json
    # Synthesize 50 controllers — way more than realistic
    base = _full_payload_with_one_stuck_oversub()
    base["controllers"] = base["controllers"] * 25  # 50 controllers
    out = _build_agent_payload(base)
    serialized = json.dumps(out)
    # Should comfortably fit in any modern LLM context
    assert len(serialized) < 50_000, f"agent payload too large: {len(serialized)}"
