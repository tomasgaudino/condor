"""Tests for condor.trading_agent.adaptive.state_io.

Cover:
- JSONL append + read round-trip.
- Window read with timestamp filtering.
- last_n with empty / partial files.
- Truncation by cutoff timestamp.
- JSON map atomic upsert.
- Tolerance to malformed lines.
- canonical_id_to_filename sanitization.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from condor.trading_agent.adaptive import state_io


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts(offset_seconds: int) -> str:
    """Generate a deterministic ISO timestamp at +offset_seconds from a fixed base."""
    base = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)
    return (base + timedelta(seconds=offset_seconds)).isoformat()


# ---------------------------------------------------------------------------
# parse_ts / utc_now
# ---------------------------------------------------------------------------


def test_parse_ts_handles_z_suffix():
    dt = state_io.parse_ts("2026-05-09T12:00:00Z")
    assert dt == datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)


def test_parse_ts_handles_explicit_offset():
    dt = state_io.parse_ts("2026-05-09T12:00:00+00:00")
    assert dt == datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)


def test_parse_ts_assumes_utc_for_naive_input():
    dt = state_io.parse_ts("2026-05-09T12:00:00")
    assert dt.tzinfo == timezone.utc


def test_utc_now_is_timezone_aware():
    assert state_io.utc_now().tzinfo == timezone.utc


# ---------------------------------------------------------------------------
# JSONL — append / read_all
# ---------------------------------------------------------------------------


def test_append_and_read_all_roundtrip(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    state_io.append_jsonl(path, {"ts": _ts(0), "n": 1})
    state_io.append_jsonl(path, {"ts": _ts(1), "n": 2})
    state_io.append_jsonl(path, {"ts": _ts(2), "n": 3})

    entries = state_io.read_jsonl_all(path)
    assert [e["n"] for e in entries] == [1, 2, 3]


def test_read_all_returns_empty_for_missing_file(tmp_path: Path):
    assert state_io.read_jsonl_all(tmp_path / "missing.jsonl") == []


def test_append_creates_parent_directory(tmp_path: Path):
    path = tmp_path / "deep" / "nested" / "events.jsonl"
    state_io.append_jsonl(path, {"ts": _ts(0), "n": 1})
    assert path.exists()


def test_read_skips_malformed_lines(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    path.write_text(
        '{"ts":"2026-05-09T12:00:00Z","n":1}\n'
        "this is not json\n"
        '{"ts":"2026-05-09T12:00:01Z","n":2}\n'
    )
    entries = state_io.read_jsonl_all(path)
    assert [e["n"] for e in entries] == [1, 2]


# ---------------------------------------------------------------------------
# JSONL — window / last_n / filter
# ---------------------------------------------------------------------------


def test_read_jsonl_window_filters_by_timestamp(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    for i in range(5):
        state_io.append_jsonl(path, {"ts": _ts(i * 60), "i": i})

    base = state_io.parse_ts(_ts(0))
    window = state_io.read_jsonl_window(
        path,
        since=base + timedelta(seconds=60),
        until=base + timedelta(seconds=180),
    )
    assert [e["i"] for e in window] == [1, 2, 3]


def test_read_jsonl_window_no_until_means_open_ended(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    for i in range(3):
        state_io.append_jsonl(path, {"ts": _ts(i * 60), "i": i})

    base = state_io.parse_ts(_ts(0))
    window = state_io.read_jsonl_window(path, since=base + timedelta(seconds=60))
    assert [e["i"] for e in window] == [1, 2]


def test_read_jsonl_window_skips_entries_without_ts(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    state_io.append_jsonl(path, {"ts": _ts(0), "i": 0})
    state_io.append_jsonl(path, {"i": 1})  # no ts
    state_io.append_jsonl(path, {"ts": _ts(60), "i": 2})

    base = state_io.parse_ts(_ts(0))
    window = state_io.read_jsonl_window(path, since=base)
    assert [e["i"] for e in window] == [0, 2]


def test_read_jsonl_last_n_returns_tail(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    for i in range(10):
        state_io.append_jsonl(path, {"i": i})

    last_3 = state_io.read_jsonl_last_n(path, 3)
    assert [e["i"] for e in last_3] == [7, 8, 9]


def test_read_jsonl_last_n_with_zero_returns_empty(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    state_io.append_jsonl(path, {"i": 0})
    assert state_io.read_jsonl_last_n(path, 0) == []


def test_read_jsonl_last_n_when_n_exceeds_size(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    state_io.append_jsonl(path, {"i": 0})
    state_io.append_jsonl(path, {"i": 1})

    # Asking for 10 from 2 entries → returns the 2 we have
    out = state_io.read_jsonl_last_n(path, 10)
    assert [e["i"] for e in out] == [0, 1]


def test_read_jsonl_filter_with_predicate(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    for i in range(10):
        state_io.append_jsonl(path, {"i": i, "kind": "even" if i % 2 == 0 else "odd"})

    evens = state_io.read_jsonl_filter(path, lambda e: e["kind"] == "even")
    assert [e["i"] for e in evens] == [0, 2, 4, 6, 8]


def test_read_jsonl_filter_respects_limit(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    for i in range(10):
        state_io.append_jsonl(path, {"i": i})

    out = state_io.read_jsonl_filter(path, lambda e: True, limit=3)
    assert [e["i"] for e in out] == [0, 1, 2]


# ---------------------------------------------------------------------------
# JSONL — truncation
# ---------------------------------------------------------------------------


def test_truncate_jsonl_drops_old_entries(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    for i in range(5):
        state_io.append_jsonl(path, {"ts": _ts(i * 60), "i": i})

    base = state_io.parse_ts(_ts(0))
    cutoff = base + timedelta(seconds=120)
    removed = state_io.truncate_jsonl_older_than(path, cutoff)

    assert removed == 2  # i=0 (t=0) and i=1 (t=60) are < cutoff
    remaining = state_io.read_jsonl_all(path)
    assert [e["i"] for e in remaining] == [2, 3, 4]


def test_truncate_jsonl_returns_zero_for_missing_file(tmp_path: Path):
    assert state_io.truncate_jsonl_older_than(tmp_path / "missing.jsonl", state_io.utc_now()) == 0


def test_truncate_jsonl_no_op_when_nothing_old(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    state_io.append_jsonl(path, {"ts": _ts(0), "i": 0})
    state_io.append_jsonl(path, {"ts": _ts(60), "i": 1})

    base = state_io.parse_ts(_ts(0))
    removed = state_io.truncate_jsonl_older_than(path, base - timedelta(seconds=60))
    assert removed == 0
    assert len(state_io.read_jsonl_all(path)) == 2


def test_truncate_jsonl_keeps_entries_with_malformed_ts(tmp_path: Path):
    """Entries with un-parseable timestamps should be preserved (not silently dropped)."""
    path = tmp_path / "events.jsonl"
    state_io.append_jsonl(path, {"ts": _ts(0), "i": 0})  # old, will drop
    state_io.append_jsonl(path, {"ts": "not-a-ts", "i": 1})  # malformed, must keep
    state_io.append_jsonl(path, {"ts": _ts(120), "i": 2})  # new, will keep

    base = state_io.parse_ts(_ts(0))
    cutoff = base + timedelta(seconds=60)
    state_io.truncate_jsonl_older_than(path, cutoff)

    remaining = state_io.read_jsonl_all(path)
    assert [e["i"] for e in remaining] == [1, 2]


# ---------------------------------------------------------------------------
# rewrite_jsonl
# ---------------------------------------------------------------------------


def test_rewrite_jsonl_replaces_full_file(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    state_io.append_jsonl(path, {"i": 1})
    state_io.append_jsonl(path, {"i": 2})

    state_io.rewrite_jsonl(path, [{"i": 99}, {"i": 100}])
    assert [e["i"] for e in state_io.read_jsonl_all(path)] == [99, 100]


# ---------------------------------------------------------------------------
# JSON map
# ---------------------------------------------------------------------------


def test_read_json_map_returns_empty_dict_for_missing_file(tmp_path: Path):
    assert state_io.read_json_map(tmp_path / "missing.json") == {}


def test_write_and_read_json_map_roundtrip(tmp_path: Path):
    path = tmp_path / "state.json"
    data = {"alpha": 1, "beta": {"nested": True}}

    state_io.write_json_map(path, data)
    assert state_io.read_json_map(path) == data


def test_upsert_json_map_creates_and_updates(tmp_path: Path):
    path = tmp_path / "state.json"
    state_io.upsert_json_map(path, "k1", {"v": 1})
    state_io.upsert_json_map(path, "k2", {"v": 2})
    state_io.upsert_json_map(path, "k1", {"v": "updated"})

    assert state_io.read_json_map(path) == {
        "k1": {"v": "updated"},
        "k2": {"v": 2},
    }


def test_read_json_map_returns_empty_for_malformed_json(tmp_path: Path):
    path = tmp_path / "state.json"
    path.write_text("not { valid json")
    assert state_io.read_json_map(path) == {}


def test_read_json_map_returns_empty_when_top_level_is_not_dict(tmp_path: Path):
    path = tmp_path / "state.json"
    path.write_text("[1, 2, 3]")  # valid JSON, but a list
    assert state_io.read_json_map(path) == {}


def test_write_json_map_creates_parent_directory(tmp_path: Path):
    path = tmp_path / "deep" / "nested" / "state.json"
    state_io.write_json_map(path, {"x": 1})
    assert path.exists()


def test_atomic_write_does_not_leave_temp_file(tmp_path: Path):
    """Tempfile-based atomic writes must clean up after themselves."""
    path = tmp_path / "state.json"
    state_io.write_json_map(path, {"x": 1})

    # No leftover .tmp files in the directory
    leftovers = [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


# ---------------------------------------------------------------------------
# canonical_id_to_filename
# ---------------------------------------------------------------------------


def test_canonical_id_to_filename_replaces_double_colon():
    assert (
        state_io.canonical_id_to_filename("pmm-btc-1::001_pmm_binance_BTC-USDT")
        == "pmm-btc-1__001_pmm_binance_BTC-USDT"
    )


def test_canonical_id_to_filename_idempotent_on_already_safe_id():
    safe = "no_colon_here"
    assert state_io.canonical_id_to_filename(safe) == safe
