"""Tests for condor.tools.dashboard.

Cover the rendering and the JSON extraction. The full async path requires
a live Condor client and is exercised manually.
"""

from __future__ import annotations

from condor.tools.dashboard import _extract_payload, render_dashboard


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------


def test_extract_payload_pulls_json_from_routine_output():
    raw = (
        "📊 PORTFOLIO UTILIZATION\n"
        "_2026-05-10T14:32:18Z_\n"
        "\n"
        "*WALLET HEADROOM*\n"
        "  ...\n"
        "\n"
        "```json\n"
        '{"ts": "2026-05-10T14:32:18Z", "summary": {"total_controllers": 2}}\n'
        "```"
    )
    payload = _extract_payload(raw)
    assert payload["ts"] == "2026-05-10T14:32:18Z"
    assert payload["summary"]["total_controllers"] == 2


def test_extract_payload_returns_error_envelope_when_no_json():
    raw = "Could not connect to API server"
    payload = _extract_payload(raw)
    assert payload["_error"] == raw
    assert payload["summary"]["total_controllers"] == 0


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def _empty_payload() -> dict:
    return {
        "ts": "2026-05-10T14:32:18Z",
        "account": "master_account",
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


def test_render_handles_empty_state():
    out = render_dashboard(_empty_payload())
    assert "no controllers in scope" in out
    assert "ADAPTIVE STRATEGY FRAMEWORK" in out


def test_render_includes_wallet_assets():
    payload = _empty_payload()
    payload["global"]["wallet"] = {
        "BTC": {
            "balance": 0.1,
            "value_usd": 6500.0,
            "used_now_usd": 3000.0,
            "headroom_usd": 3500.0,
            "headroom_pct": 0.54,
            "controllers_using": [],
        },
    }
    payload["controllers"] = [
        {
            "id": "bot::ctrl",
            "config_name": "ctrl",
            "capital": {
                "utilization_now": 0.5,
                "nominal_budget_usd": 30.0,
                "committed_now_usd": 15.0,
                "worst_case_usd": 300.0,
                "utilization_vs_worst": 0.05,
                "active_executors_count": 2,
            },
            "config": {
                "portfolio_allocation": 0.03,
                "max_active_executors_by_level": 10,
                "buy_levels": 2,
                "sell_levels": 2,
            },
        }
    ]
    payload["summary"]["total_controllers"] = 1
    payload["summary"]["wallet_total_value_usd"] = 6500.0

    out = render_dashboard(payload)
    assert "BTC" in out
    assert "WALLET HEADROOM" in out
    assert "ctrl" in out


def test_render_flags_overutilized_controllers_with_warn_marker():
    payload = _empty_payload()
    payload["controllers"] = [
        {
            "id": "bot::overheated",
            "config_name": "overheated",
            "capital": {
                "utilization_now": 1.5,  # > 1.0
                "nominal_budget_usd": 30.0,
                "committed_now_usd": 45.0,
                "worst_case_usd": 300.0,
                "utilization_vs_worst": 0.15,
                "active_executors_count": 5,
            },
            "config": {
                "portfolio_allocation": 0.03,
                "max_active_executors_by_level": 10,
                "buy_levels": 2,
                "sell_levels": 2,
            },
        }
    ]
    payload["summary"]["total_controllers"] = 1

    out = render_dashboard(payload)
    # The line for the overheated controller carries the warn glyph
    overheated_line = next(line for line in out.splitlines() if "overheated" in line)
    assert "⚠" in overheated_line


def test_render_shows_oversub_alerts():
    payload = _empty_payload()
    payload["controllers"] = [
        {
            "id": "bot::c",
            "config_name": "c",
            "capital": {
                "utilization_now": 0.5,
                "nominal_budget_usd": 30.0,
                "committed_now_usd": 15.0,
                "worst_case_usd": 300.0,
                "utilization_vs_worst": 0.05,
                "active_executors_count": 2,
            },
            "config": {
                "portfolio_allocation": 0.03,
                "max_active_executors_by_level": 10,
                "buy_levels": 2,
                "sell_levels": 2,
            },
        }
    ]
    payload["summary"]["total_controllers"] = 1
    payload["global"]["oversub_alerts"] = [
        {
            "asset": "BTC",
            "demand_potential_usd": 11500.0,
            "supply_usd": 8200.0,
            "ratio": 1.40,
            "severity": "warn",
            "controllers": ["bot::c"],
        }
    ]

    out = render_dashboard(payload)
    assert "OVERSUBSCRIPTION" in out
    assert "BTC" in out
    assert "1.40" in out
