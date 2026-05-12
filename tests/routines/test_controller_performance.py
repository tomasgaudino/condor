"""Tests for routines/controller_performance.py — pure-function building blocks.

Cover:
- close_type normalization (strip enum prefix, dominant).
- Snapshot extraction from real-shape `performance` dicts.
- Window velocity computation against synthetic history.
- Cold-start / adaptive / normal warmup status (F3).
- Time-since-last-fill detection.
- Diagnostic flags (pnl_flat / volume_dropping / stuck / suboptimal_now).
- Aggregate composition (counts, worst, total PnL).
- Autodetect with mocked client.
- Agent payload shape (per-controller + summary).
- Summary table sorting + flags.
- Compact summary respects L2 (single line, no backticks).
- Persistence round-trip via state_io.
"""

from __future__ import annotations

import asyncio
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from routines.controller_performance import (
    Config,
    _autodetect_controllers,
    _build_agent_payload,
    _build_agent_summary,
    _build_aggregate_narrative,
    _build_controller_narrative,
    _build_controller_payload,
    _build_kpi_sections,
    _build_summary_table,
    _build_windows_table,
    _compose_aggregate,
    _compute_window,
    _diagnose,
    _dominant_close_type,
    _extract_snapshot,
    _find_snapshot_before,
    _maybe_fetch_market_share,
    _normalize_close_type_counts,
    _persist_snapshot,
    _render_compact_summary,
    _resolve_perf,
    _strip_close_type,
    _time_since_last_fill_minutes,
    _warmup_status,
    _window_key,
)
from condor.trading_agent.adaptive import state_io


UTC = timezone.utc


# ---------------------------------------------------------------------------
# Close-type normalization
# ---------------------------------------------------------------------------


def test_strip_close_type_enum_prefix():
    assert _strip_close_type("CloseType.TAKE_PROFIT") == "take_profit"
    assert _strip_close_type("CloseType.EARLY_STOP") == "early_stop"


def test_strip_close_type_plain_string():
    assert _strip_close_type("custom") == "custom"


def test_strip_close_type_non_string_coerced():
    assert _strip_close_type(42) == "42"


def test_normalize_close_type_counts_strips_and_lowercases():
    raw = {"CloseType.TAKE_PROFIT": 1390, "CloseType.POSITION_HOLD": 248, "weird_key": 5}
    out = _normalize_close_type_counts(raw)
    assert out == {"take_profit": 1390, "position_hold": 248, "weird_key": 5}


def test_normalize_close_type_counts_non_dict():
    assert _normalize_close_type_counts(None) == {}
    assert _normalize_close_type_counts([]) == {}


def test_dominant_close_type():
    assert _dominant_close_type({"take_profit": 100, "early_stop": 5}) == "take_profit"


def test_dominant_close_type_empty():
    assert _dominant_close_type({}) is None


# ---------------------------------------------------------------------------
# Snapshot extraction
# ---------------------------------------------------------------------------


def test_extract_snapshot_full_shape():
    """Mirrors the exact response from brigado on 2026-05-12."""
    perf = {
        "realized_pnl_quote": 12.42,
        "unrealized_pnl_quote": -11.48,
        "volume_traded": 316717.16,
        "positions_summary": [{"side": "BUY", "amount": 0.03585}],
        "close_type_counts": {
            "CloseType.TAKE_PROFIT": 1390,
            "CloseType.EARLY_STOP": 745,
            "CloseType.POSITION_HOLD": 248,
        },
    }
    snap = _extract_snapshot(perf)
    assert snap["realized_pnl_quote"] == 12.42
    assert snap["unrealized_pnl_quote"] == -11.48
    assert abs(snap["net_pnl_quote"] - 0.94) < 0.001
    assert snap["volume_traded"] == 316717.16
    assert snap["positions_open"] == 1
    assert snap["positions_closed"] == 2383
    assert snap["close_types_dominant"] == "take_profit"


def test_extract_snapshot_missing_fields_default_to_zero():
    snap = _extract_snapshot({})
    assert snap["realized_pnl_quote"] == 0
    assert snap["unrealized_pnl_quote"] == 0
    assert snap["positions_closed"] == 0
    assert snap["close_types_dominant"] is None


# ---------------------------------------------------------------------------
# Window math
# ---------------------------------------------------------------------------


def test_window_key_format():
    assert _window_key(1.0) == "last_1h"
    assert _window_key(6.0) == "last_6h"
    assert _window_key(24.0) == "last_24h"
    assert _window_key(0.5) == "last_0.5h"


def test_find_snapshot_before_returns_closest():
    history = [
        {"ts": "2026-05-12T10:00:00Z", "r_pnl": 1.0},
        {"ts": "2026-05-12T11:00:00Z", "r_pnl": 2.0},
        {"ts": "2026-05-12T12:00:00Z", "r_pnl": 3.0},
    ]
    cutoff = datetime(2026, 5, 12, 11, 30, tzinfo=UTC)
    found = _find_snapshot_before(history, cutoff)
    assert found["r_pnl"] == 2.0


def test_find_snapshot_before_none_when_all_after():
    history = [{"ts": "2026-05-12T12:00:00Z", "r_pnl": 1.0}]
    cutoff = datetime(2026, 5, 12, 10, 0, tzinfo=UTC)
    assert _find_snapshot_before(history, cutoff) is None


def test_compute_window_velocities():
    """Build a known history and verify the derivatives are correct."""
    now = datetime(2026, 5, 12, 12, 0, tzinfo=UTC)
    # 1h ago snapshot
    history = [
        {"ts": "2026-05-12T11:00:00Z", "r_pnl": 10.0, "u_pnl": 0.0, "vol": 1000.0, "pos_closed": 100},
    ]
    snap = {
        "realized_pnl_quote": 13.0, "unrealized_pnl_quote": 0.0,
        "net_pnl_quote": 13.0, "volume_traded": 1300.0, "positions_closed": 110,
    }
    w = _compute_window(snap, history, now, window_hours=1.0)
    assert w["available"]
    assert abs(w["pnl_velocity"] - 3.0) < 1e-9   # 3 USD in 1h
    assert abs(w["volume_velocity"] - 300.0) < 1e-9
    assert abs(w["fills_per_hour"] - 10.0) < 1e-9


def test_compute_window_unavailable_when_no_history():
    now = datetime(2026, 5, 12, 12, 0, tzinfo=UTC)
    snap = {"realized_pnl_quote": 0, "unrealized_pnl_quote": 0,
            "net_pnl_quote": 0, "volume_traded": 0, "positions_closed": 0}
    w = _compute_window(snap, [], now, 24.0)
    assert w["available"] is False
    assert w["samples"] == 0


# ---------------------------------------------------------------------------
# Cold-start (F3)
# ---------------------------------------------------------------------------


def test_warmup_cold_start_when_too_few_samples():
    now = datetime(2026, 5, 12, 12, 0, tzinfo=UTC)
    h, status, key = _warmup_status([{"ts": "2026-05-12T11:50:00Z"}], now)
    assert status == "cold_start"
    assert h == 0.0


def test_warmup_cold_start_when_too_young():
    now = datetime(2026, 5, 12, 12, 0, tzinfo=UTC)
    history = [
        {"ts": "2026-05-12T11:40:00Z"},
        {"ts": "2026-05-12T11:45:00Z"},
        {"ts": "2026-05-12T11:50:00Z"},
    ]
    # Oldest is 20 minutes ago → <30 min → cold_start.
    _, status, _ = _warmup_status(history, now)
    assert status == "cold_start"


def test_warmup_adaptive_between_30min_and_4h():
    now = datetime(2026, 5, 12, 12, 0, tzinfo=UTC)
    history = [
        {"ts": "2026-05-12T10:30:00Z"},
        {"ts": "2026-05-12T11:00:00Z"},
        {"ts": "2026-05-12T11:30:00Z"},
    ]
    # Oldest 1.5h ago.
    h, status, key = _warmup_status(history, now)
    assert status == "adaptive"
    assert 1.4 < h < 1.6


def test_warmup_normal_after_4h():
    now = datetime(2026, 5, 12, 12, 0, tzinfo=UTC)
    history = [
        {"ts": "2026-05-12T05:00:00Z"},
        {"ts": "2026-05-12T06:00:00Z"},
        {"ts": "2026-05-12T07:00:00Z"},
    ]
    h, status, key = _warmup_status(history, now)
    assert status == "normal"
    assert h == 4.0
    assert key == "last_4h"


# ---------------------------------------------------------------------------
# Time since last fill
# ---------------------------------------------------------------------------


def test_time_since_fill_finds_most_recent_increase():
    now = datetime(2026, 5, 12, 12, 0, tzinfo=UTC)
    history = [
        {"ts": "2026-05-12T11:00:00Z", "pos_closed": 100},
        {"ts": "2026-05-12T11:30:00Z", "pos_closed": 100},  # no fills here
        {"ts": "2026-05-12T11:50:00Z", "pos_closed": 100},  # still no fills
    ]
    # current pos = 105 → fill happened after 11:50
    out = _time_since_last_fill_minutes(105, history, now)
    # Last entry where pos_closed (100) < current (105) is the 11:50 one
    # → 10 minutes ago
    assert out is not None
    assert abs(out - 10.0) < 0.1


def test_time_since_fill_none_when_history_empty():
    assert _time_since_last_fill_minutes(100, [], datetime.now(UTC)) is None


# ---------------------------------------------------------------------------
# Diagnostic
# ---------------------------------------------------------------------------


def _baseline_diag_inputs(**overrides):
    """A reasonable input set that should yield all flags False.

    Note: with nominal=100 and pnl_velocity=5 we get pnl_v_norm=0.05,
    well above the 0.001 flat threshold.
    """
    windows = {
        "last_1h":  {"available": True, "pnl_velocity": 5.0, "volume_velocity": 1000.0, "fills_per_hour": 5},
        "last_4h":  {"available": True, "pnl_velocity": 5.0, "volume_velocity": 1000.0, "fills_per_hour": 5, "elapsed_hours": 4.0},
        "last_24h": {"available": True, "pnl_velocity": 5.0, "volume_velocity": 1000.0, "fills_per_hour": 5},
    }
    snap = {"close_types_dominant": "take_profit"}
    base = {
        "snap": snap, "windows": windows, "nominal_budget_usd": 100.0,
        "warmup_status": "normal", "effective_window_key": "last_4h",
        "time_since_last_fill_min": 5.0, "cfg": Config(),
    }
    base.update(overrides)
    return base


def test_diagnose_baseline_no_flags():
    d = _diagnose(**_baseline_diag_inputs())
    assert d["is_pnl_flat"] is False
    assert d["is_volume_dropping"] is False
    assert d["is_stuck"] is False
    assert d["suboptimal_now"] is False
    assert d["warmup_status"] == "normal"


def test_diagnose_pnl_flat_when_velocity_near_zero():
    windows = {
        "last_1h":  {"available": True, "pnl_velocity": 0.0, "volume_velocity": 1000, "fills_per_hour": 5},
        "last_4h":  {"available": True, "pnl_velocity": 0.0, "volume_velocity": 1000, "fills_per_hour": 5, "elapsed_hours": 4.0},
        "last_24h": {"available": True, "pnl_velocity": 0.0, "volume_velocity": 1000, "fills_per_hour": 5},
    }
    d = _diagnose(**_baseline_diag_inputs(windows=windows))
    assert d["is_pnl_flat"] is True


def test_diagnose_volume_dropping():
    windows = {
        "last_1h":  {"available": True, "pnl_velocity": 0.5, "volume_velocity": 100, "fills_per_hour": 1},
        "last_4h":  {"available": True, "pnl_velocity": 0.5, "volume_velocity": 800, "fills_per_hour": 4, "elapsed_hours": 4.0},
        "last_24h": {"available": True, "pnl_velocity": 0.5, "volume_velocity": 1000, "fills_per_hour": 5},
    }
    # vol_1h (100) < 0.5 × vol_24h (1000) → dropping
    d = _diagnose(**_baseline_diag_inputs(windows=windows))
    assert d["is_volume_dropping"] is True


def test_diagnose_stuck_when_long_no_fill_and_low_vol():
    windows = {
        "last_1h":  {"available": True, "pnl_velocity": 0.0, "volume_velocity": 50, "fills_per_hour": 0},
        "last_4h":  {"available": True, "pnl_velocity": 0.0, "volume_velocity": 100, "fills_per_hour": 1, "elapsed_hours": 4.0},
        "last_24h": {"available": True, "pnl_velocity": 0.0, "volume_velocity": 1000, "fills_per_hour": 5},
    }
    d = _diagnose(**_baseline_diag_inputs(windows=windows, time_since_last_fill_min=90.0))
    assert d["is_stuck"] is True


def test_diagnose_cold_start_disables_suboptimal_now():
    d = _diagnose(**_baseline_diag_inputs(warmup_status="cold_start"))
    assert d["suboptimal_now"] is False


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------


def _make_payload(cid: str, **diag_overrides) -> dict:
    diag = {
        "is_pnl_flat": False, "is_volume_dropping": False, "is_stuck": False,
        "suboptimal_now": False, "warmup_status": "normal",
        "pnl_velocity_normalized": 0.0,
    }
    diag.update(diag_overrides)
    return {
        "ts": "2026-05-12T12:00:00Z",
        "controller_id": cid,
        "trading_pair": "BTC-USDT",
        "windows": {"last_24h": {"available": True, "pnl_velocity": 1.0}},
        "snapshot": {"positions_closed": 100, "realized_pnl_quote": 10.0,
                     "unrealized_pnl_quote": 0.0, "net_pnl_quote": 10.0, "volume_traded": 1000.0,
                     "positions_open": 0},
        "diagnostic": diag,
        "market_cross": {},
    }


def test_aggregate_counts_flags():
    payloads = {
        "b::a": _make_payload("b::a", suboptimal_now=True),
        "b::b": _make_payload("b::b", is_stuck=True),
        "b::c": _make_payload("b::c", warmup_status="cold_start"),
        "b::d": _make_payload("b::d"),
    }
    agg = _compose_aggregate(payloads)
    assert agg["counts"]["suboptimal_now"] == 1
    assert agg["counts"]["stuck"] == 1
    assert agg["counts"]["cold_start"] == 1


def test_aggregate_worst_by_pnl_velocity_normalized():
    payloads = {
        "b::a": _make_payload("b::a", pnl_velocity_normalized=-0.01),
        "b::b": _make_payload("b::b", pnl_velocity_normalized=-0.05),
        "b::c": _make_payload("b::c", pnl_velocity_normalized=0.02),
    }
    agg = _compose_aggregate(payloads)
    assert agg["worst"]["controller_id"] == "b::b"


def test_aggregate_worst_none_when_all_positive():
    payloads = {
        "b::a": _make_payload("b::a", pnl_velocity_normalized=0.01),
        "b::b": _make_payload("b::b", pnl_velocity_normalized=0.02),
    }
    agg = _compose_aggregate(payloads)
    assert agg["worst"] is None


def test_aggregate_total_pnl_24h():
    payloads = {
        "b::a": _make_payload("b::a"),  # last_24h pnl_velocity = 1.0 → 24 USD
        "b::b": _make_payload("b::b"),
    }
    agg = _compose_aggregate(payloads)
    assert abs(agg["total_pnl_24h_usd"] - 48.0) < 1e-6


# ---------------------------------------------------------------------------
# Agent views — shape + size
# ---------------------------------------------------------------------------


def test_agent_payload_no_human_leakage():
    p = _make_payload("b::a")
    p["close_type_counts"] = {"take_profit": 100, "early_stop": 5}
    a = _build_agent_payload(p)
    assert "controller_id" in a
    assert "windows" in a
    assert "snapshot" in a
    assert "diagnostic" in a
    # No raw close_type_counts (the agent only sees `close_types_dominant`).
    assert "close_type_counts" not in a
    # No narrative or glossary.
    assert "narrative" not in a


def test_agent_payload_size_under_2kb():
    p = _make_payload("b::a")
    p["windows"] = {
        "last_1h":  {"available": True, "samples": 6, "pnl_velocity": 0.1, "volume_velocity": 1000, "fills_per_hour": 5,
                     "realized_pnl_quote": 10.0, "unrealized_pnl_quote": 0.0, "net_pnl_quote": 10.0,
                     "volume_traded": 1000.0, "positions_closed": 100},
        "last_6h":  {"available": True, "samples": 36, "pnl_velocity": 0.1, "volume_velocity": 1000, "fills_per_hour": 5,
                     "realized_pnl_quote": 10.0, "unrealized_pnl_quote": 0.0, "net_pnl_quote": 10.0,
                     "volume_traded": 1000.0, "positions_closed": 100},
        "last_24h": {"available": True, "samples": 144, "pnl_velocity": 0.1, "volume_velocity": 1000, "fills_per_hour": 5,
                     "realized_pnl_quote": 10.0, "unrealized_pnl_quote": 0.0, "net_pnl_quote": 10.0,
                     "volume_traded": 1000.0, "positions_closed": 100},
    }
    a = _build_agent_payload(p)
    size = len(json.dumps(a))
    assert size < 2000, f"agent payload grew to {size} chars"


def test_agent_summary_shape():
    payloads = {
        "b::a": _make_payload("b::a", suboptimal_now=True),
        "b::b": _make_payload("b::b"),
    }
    agg = _compose_aggregate(payloads)
    s = _build_agent_summary(agg, payloads)
    for key in ("ts", "controllers", "counts", "worst", "total_pnl_24h_usd"):
        assert key in s


# ---------------------------------------------------------------------------
# User view
# ---------------------------------------------------------------------------


def test_compact_summary_single_line_no_backticks():
    payloads = {"b::a": _make_payload("b::a")}
    agg = _compose_aggregate(payloads)
    text = _render_compact_summary(agg)
    assert "\n" not in text
    assert "`" not in text
    assert len(text) <= 220


def test_compact_summary_no_controllers():
    agg = _compose_aggregate({})
    text = _render_compact_summary(agg)
    assert "no controllers" in text.lower()


def test_kpi_sections_have_4_labels():
    payloads = {"b::a": _make_payload("b::a")}
    agg = _compose_aggregate(payloads)
    kpis = _build_kpi_sections(agg)
    labels = [k["label"] for k in kpis]
    assert labels == ["Controllers", "Suboptimal", "Stuck", "PnL 24h"]


def test_summary_table_sorts_problems_first():
    payloads = {
        "b::ok":   _make_payload("b::ok"),
        "b::bad":  _make_payload("b::bad", suboptimal_now=True),
    }
    cols, rows = _build_summary_table(payloads)
    assert rows[0]["controller"] == "bad"  # problem first
    assert rows[1]["controller"] == "ok"


def test_summary_table_flags_render():
    payloads = {"b::a": _make_payload("b::a", is_stuck=True, is_volume_dropping=True)}
    _, rows = _build_summary_table(payloads)
    assert "🔒" in rows[0]["flags"]
    assert "📉" in rows[0]["flags"]


def test_windows_table_three_rows():
    p = _make_payload("b::a")
    p["windows"] = {
        "last_1h":  {"available": True, "samples": 6, "pnl_velocity": 0.1,
                     "volume_velocity": 100, "fills_per_hour": 5,
                     "realized_pnl_quote": 1.0, "unrealized_pnl_quote": 0.0,
                     "net_pnl_quote": 1.0, "volume_traded": 100.0, "positions_closed": 10},
        "last_6h":  {"available": True, "samples": 36, "pnl_velocity": 0.1,
                     "volume_velocity": 100, "fills_per_hour": 5,
                     "realized_pnl_quote": 1.0, "unrealized_pnl_quote": 0.0,
                     "net_pnl_quote": 1.0, "volume_traded": 100.0, "positions_closed": 10},
        "last_24h": {"available": True, "samples": 144, "pnl_velocity": 0.1,
                     "volume_velocity": 100, "fills_per_hour": 5,
                     "realized_pnl_quote": 1.0, "unrealized_pnl_quote": 0.0,
                     "net_pnl_quote": 1.0, "volume_traded": 100.0, "positions_closed": 10},
    }
    cols, rows = _build_windows_table(p)
    assert len(rows) == 3
    assert {r["window"] for r in rows} == {"last_1h", "last_6h", "last_24h"}


def test_aggregate_narrative_mentions_counts():
    payloads = {
        "b::a": _make_payload("b::a", suboptimal_now=True),
        "b::b": _make_payload("b::b", is_stuck=True),
    }
    agg = _compose_aggregate(payloads)
    n = _build_aggregate_narrative(agg, payloads)
    assert "2 controllers" in n
    assert "subóptima" in n
    assert "atrapados" in n


def test_controller_narrative_cold_start():
    p = _make_payload("b::a", warmup_status="cold_start")
    n = _build_controller_narrative(p)
    assert "cold_start" in n
    assert "Sin historia" in n


# ---------------------------------------------------------------------------
# Autodetect (mocked client)
# ---------------------------------------------------------------------------


class _FakeBotOrch:
    def __init__(self, bots):
        self._bots = bots

    async def get_active_bots_status(self):
        return {"data": {name: {} for name in self._bots}}


class _FakeControllers:
    def __init__(self, configs_by_bot):
        self._configs = configs_by_bot

    async def get_bot_controller_configs(self, bot_name):
        return self._configs.get(bot_name, [])


class _FakeClient:
    def __init__(self, configs_by_bot):
        self.bot_orchestration = _FakeBotOrch(configs_by_bot)
        self.controllers = _FakeControllers(configs_by_bot)


def test_autodetect_returns_all_controllers():
    client = _FakeClient({
        "bot1": [
            {"_config_name": "c1", "trading_pair": "BTC-USDT", "connector_name": "binance"},
            {"_config_name": "c2", "trading_pair": "ETH-USDT", "connector_name": "binance"},
        ],
        "bot2": [{"_config_name": "c3", "trading_pair": "SOL-USDT", "connector_name": "binance"}],
    })
    out = asyncio.run(_autodetect_controllers(client, None))
    cids = [(b, c) for b, c, _ in out]
    assert ("bot1", "c1") in cids
    assert ("bot1", "c2") in cids
    assert ("bot2", "c3") in cids


def test_autodetect_filters_by_bot_names():
    client = _FakeClient({
        "bot1": [{"_config_name": "c1", "connector_name": "binance"}],
        "bot2": [{"_config_name": "c3", "connector_name": "binance"}],
    })
    out = asyncio.run(_autodetect_controllers(client, ["bot1"]))
    assert [b for b, _, _ in out] == ["bot1"]


def test_autodetect_empty_when_no_bots():
    client = _FakeClient({})
    out = asyncio.run(_autodetect_controllers(client, None))
    assert out == []


# ---------------------------------------------------------------------------
# Persistence round-trip (real JSONL via tmp_path)
# ---------------------------------------------------------------------------


def test_persist_snapshot_appends_and_truncates(tmp_path: Path, monkeypatch):
    """Persist snapshots and confirm they're readable back via state_io."""
    monkeypatch.setattr(
        "routines.controller_performance.STATE_DIR",
        tmp_path / "controller_performance",
    )
    cid = "bot1::c1"
    snap = {
        "realized_pnl_quote": 10.0, "unrealized_pnl_quote": -1.0,
        "net_pnl_quote": 9.0, "volume_traded": 1000.0, "positions_closed": 50,
    }
    now = datetime(2026, 5, 12, 12, 0, tzinfo=UTC)
    _persist_snapshot(cid, now, snap)
    _persist_snapshot(cid, now + timedelta(hours=1), snap)

    path = tmp_path / "controller_performance" / "bot1__c1.jsonl"
    assert path.exists()
    entries = state_io.read_jsonl_all(path)
    assert len(entries) == 2
    assert entries[0]["r_pnl"] == 10.0
    assert entries[0]["pos_closed"] == 50


# ---------------------------------------------------------------------------
# Resolve perf — robustness against key variation
# ---------------------------------------------------------------------------


def test_resolve_perf_by_config_name():
    cfg_raw = {"_config_name": "c1", "connector_name": "binance", "trading_pair": "BTC-USDT"}
    perfs = {"c1": {"performance": {"realized_pnl_quote": 5.0}}}
    perf = _resolve_perf(perfs, cfg_raw)
    assert perf == {"realized_pnl_quote": 5.0}


def test_resolve_perf_fallback_by_pair():
    cfg_raw = {"_config_name": "missing", "connector_name": "binance", "trading_pair": "BTC-USDT"}
    perfs = {
        "weird_key": {"performance": {
            "realized_pnl_quote": 7.0,
            "positions_summary": [{"connector_name": "binance", "trading_pair": "BTC-USDT"}],
        }}
    }
    perf = _resolve_perf(perfs, cfg_raw)
    assert perf == {
        "realized_pnl_quote": 7.0,
        "positions_summary": [{"connector_name": "binance", "trading_pair": "BTC-USDT"}],
    }


def test_resolve_perf_none_when_no_match():
    cfg_raw = {"_config_name": "x", "connector_name": "binance", "trading_pair": "BTC-USDT"}
    assert _resolve_perf({}, cfg_raw) is None


# ---------------------------------------------------------------------------
# Build controller payload — full integration on synthetic data
# ---------------------------------------------------------------------------


def test_build_controller_payload_end_to_end():
    now = datetime(2026, 5, 12, 12, 0, tzinfo=UTC)
    cfg = Config()
    history = [
        {"ts": "2026-05-12T11:00:00Z", "r_pnl": 5.0, "u_pnl": 0.0, "vol": 500.0, "pos_closed": 90},
        {"ts": "2026-05-12T06:00:00Z", "r_pnl": 1.0, "u_pnl": 0.0, "vol": 100.0, "pos_closed": 50},
    ]
    # state_io.append_jsonl writes ascending — sort
    history.sort(key=lambda e: e["ts"])
    perf = {
        "realized_pnl_quote": 10.0, "unrealized_pnl_quote": 0.0,
        "volume_traded": 1500.0, "positions_summary": [],
        "close_type_counts": {"CloseType.TAKE_PROFIT": 100},
    }
    cfg_raw = {
        "_config_name": "c1", "connector_name": "binance", "trading_pair": "BTC-USDT",
        "total_amount_quote": 100.0, "portfolio_allocation": 0.1,
    }
    p = _build_controller_payload(
        canonical_id="bot1::c1", bot_name="bot1", config_name="c1",
        cfg_raw=cfg_raw, perf=perf, history=history, now=now, cfg=cfg, market_cross={},
    )
    assert p["controller_id"] == "bot1::c1"
    assert "last_1h" in p["windows"]
    assert "last_24h" in p["windows"]
    assert p["windows"]["last_1h"]["available"] is True
    assert p["diagnostic"]["warmup_status"] in ("normal", "adaptive", "cold_start")
    assert p["snapshot"]["positions_closed"] == 100
