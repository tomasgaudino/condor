"""Tests for condor.trading_agent.adaptive.backtest_cache."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from condor.trading_agent.adaptive import backtest_cache as bc


UTC = timezone.utc


# ---------------------------------------------------------------------------
# canonicalize_config + cache_key
# ---------------------------------------------------------------------------


def test_canonicalize_excludes_id_and_timestamps():
    config = {
        "take_profit": 0.0003,
        "id": "random-uuid",
        "_config_name": "ctrl_x",
        "created_at": "2026-05-01T00:00:00Z",
        "updated_at": "2026-05-02T00:00:00Z",
        "buy_spreads": 5e-05,
    }
    s = bc.canonicalize_config(config)
    parsed = json.loads(s)
    assert "id" not in parsed
    assert "_config_name" not in parsed
    assert "created_at" not in parsed
    assert "updated_at" not in parsed
    assert parsed == {"take_profit": 0.0003, "buy_spreads": 5e-05}


def test_canonicalize_sorted_keys():
    s = bc.canonicalize_config({"b": 1, "a": 2, "c": 3})
    assert s == '{"a":2,"b":1,"c":3}'


def test_cache_key_stable():
    now = datetime(2026, 5, 12, 12, tzinfo=UTC)
    k1 = bc.cache_key(
        controller_id="bot::c1",
        config_snapshot={"take_profit": 0.0003},
        start_ts=now, end_ts=now + timedelta(hours=1),
        resolution="1m",
    )
    k2 = bc.cache_key(
        controller_id="bot::c1",
        config_snapshot={"take_profit": 0.0003},
        start_ts=now, end_ts=now + timedelta(hours=1),
        resolution="1m",
    )
    assert k1 == k2
    assert len(k1) == 16


def test_cache_key_differs_on_value_change():
    now = datetime(2026, 5, 12, 12, tzinfo=UTC)
    k1 = bc.cache_key(
        controller_id="bot::c1", config_snapshot={"tp": 0.0003},
        start_ts=now, end_ts=now + timedelta(hours=1), resolution="1m",
    )
    k2 = bc.cache_key(
        controller_id="bot::c1", config_snapshot={"tp": 0.00045},
        start_ts=now, end_ts=now + timedelta(hours=1), resolution="1m",
    )
    assert k1 != k2


def test_cache_key_ignores_excluded_fields():
    now = datetime(2026, 5, 12, 12, tzinfo=UTC)
    a = {"id": "uuid-a", "_config_name": "x", "tp": 0.0003}
    b = {"id": "uuid-b", "_config_name": "y", "tp": 0.0003}
    k_a = bc.cache_key(controller_id="b::c", config_snapshot=a, start_ts=now,
                       end_ts=now + timedelta(hours=1), resolution="1m")
    k_b = bc.cache_key(controller_id="b::c", config_snapshot=b, start_ts=now,
                       end_ts=now + timedelta(hours=1), resolution="1m")
    assert k_a == k_b


# ---------------------------------------------------------------------------
# get / put round-trip
# ---------------------------------------------------------------------------


def test_put_get_round_trip(tmp_path):
    bc.put(tmp_path, "abc123", {"net_pnl_quote": 0.5},
           meta={"controller_id": "b::c"})
    got = bc.get(tmp_path, "abc123")
    assert got["result"] == {"net_pnl_quote": 0.5}
    assert got["meta"]["controller_id"] == "b::c"
    assert "ts_cached" in got["meta"]


def test_get_missing_returns_none(tmp_path):
    assert bc.get(tmp_path, "nope") is None


def test_put_atomic_on_overwrite(tmp_path):
    bc.put(tmp_path, "k", {"v": 1})
    bc.put(tmp_path, "k", {"v": 2})
    assert bc.get(tmp_path, "k")["result"]["v"] == 2


# ---------------------------------------------------------------------------
# Index + cleanup
# ---------------------------------------------------------------------------


def test_index_updated_on_put(tmp_path):
    bc.put(tmp_path, "k1", {"x": 1})
    bc.put(tmp_path, "k2", {"x": 2})
    idx_path = bc._index_path(tmp_path)
    assert idx_path.exists()
    idx = json.loads(idx_path.read_text())
    assert "k1" in idx
    assert "k2" in idx


def test_purge_older_than_removes_old(tmp_path):
    # k1 cached "back in time", k2 cached "now"
    bc.put(tmp_path, "k1", {"v": 1},
           now=datetime(2026, 4, 1, tzinfo=UTC))
    bc.put(tmp_path, "k2", {"v": 2},
           now=datetime(2026, 5, 12, tzinfo=UTC))
    removed = bc.purge_older_than(tmp_path, datetime(2026, 5, 1, tzinfo=UTC))
    assert removed == 1
    assert bc.get(tmp_path, "k1") is None
    assert bc.get(tmp_path, "k2") is not None


def test_purge_no_index_returns_zero(tmp_path):
    assert bc.purge_older_than(tmp_path, datetime(2026, 1, 1, tzinfo=UTC)) == 0


# ---------------------------------------------------------------------------
# Daily cleanup marker
# ---------------------------------------------------------------------------


def test_should_run_when_marker_missing(tmp_path):
    (tmp_path / "state").mkdir()
    assert bc.should_run_daily_cleanup(tmp_path) is True


def test_should_not_run_within_24h(tmp_path):
    (tmp_path / "state").mkdir()
    bc.stamp_daily_cleanup(tmp_path)
    assert bc.should_run_daily_cleanup(tmp_path) is False


def test_should_run_when_marker_old(tmp_path):
    """Touch the marker into the past."""
    import os
    (tmp_path / "state").mkdir()
    bc.stamp_daily_cleanup(tmp_path)
    # Backdate the marker 25h
    old_time = (datetime.now(UTC) - timedelta(hours=25)).timestamp()
    os.utime(bc.cleanup_marker_path(tmp_path), (old_time, old_time))
    assert bc.should_run_daily_cleanup(tmp_path) is True
