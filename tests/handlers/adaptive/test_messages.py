"""Tests for handlers.adaptive.messages.

Pure-function builders only. Targets:
- callback data round-trips (apply, reject, snooze)
- propose text contains the key fields, no raw markdown that would
  break MarkdownV2 (testing the escape is applied to dynamic data)
- keyboard layout: winner first + alternatives + reject/snooze row
- the post-verdict edit texts mention the right state
"""

from __future__ import annotations

from telegram import InlineKeyboardMarkup

from handlers.adaptive import messages as msg


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


def _entry() -> dict:
    return {
        "patch_id": "p_20260512T120000Z_001",
        "ts": "2026-05-12T12:00:00Z",
        "bot_name": "experiment-legacy-20",
        "config_name": "btcusdt-1-5",
        "controller_id": "experiment-legacy-20::btcusdt-1-5",
        "trading_pair": "BTC-USDT",
        "connector_name": "binance",
        "regime_observed": "mean_reverting_high_vol",
        "suboptimal_minutes": 47,
        "warmup_status": "normal",
        "llm_proposal": {
            "dimension": "take_profit",
            "direction": "increase",
            "magnitude_qualitative": "medium",
            "reasoning": "NATR alto (0.014 vs p67 0.010).",
            "caveats": ["only affects new executors"],
        },
        "expanded_candidates": [
            {"id": "c1", "value": 0.00045, "delta_relative": "×1.5"},
            {"id": "c2", "value": 0.00060, "delta_relative": "×2.0"},
            {"id": "c3", "value": 0.00090, "delta_relative": "×3.0"},
        ],
        "backtests": {
            "baseline":  {"net_pnl_quote": 0.12, "volume": 12400, "score": 1.3},
            "candidate1": {"net_pnl_quote": 0.45, "volume": 9800, "score": 1.42},
            "candidate2": {"net_pnl_quote": 0.78, "volume": 8200, "score": 1.35},
            "candidate3": {"net_pnl_quote": 1.12, "volume": 5400, "score": 1.12},
        },
        "selected": "c1",
        "selected_value": 0.00045,
        "old_value": 0.0003,
        "human_verdict": "pending",
    }


# ---------------------------------------------------------------------------
# Callback data
# ---------------------------------------------------------------------------


def test_cb_apply_format():
    assert msg.cb_apply("p_xxx_001", "c1") == "adaptive:apply:p_xxx_001:c1"


def test_cb_reject_format():
    assert msg.cb_reject("p_xxx_001") == "adaptive:reject:p_xxx_001"


def test_cb_snooze_format():
    assert msg.cb_snooze("p_xxx_001", "1h") == "adaptive:snooze:p_xxx_001:1h"


def test_cb_data_under_telegram_64byte_limit():
    """Worst case for MVP — long patch_id + candidate."""
    pid = "p_20260512T120000Z_001"
    for cb in (msg.cb_apply(pid, "c3"), msg.cb_reject(pid), msg.cb_snooze(pid, "12h")):
        assert len(cb.encode("utf-8")) <= 64, f"{cb!r} exceeds 64 bytes"


# ---------------------------------------------------------------------------
# Propose text
# ---------------------------------------------------------------------------


def test_propose_text_mentions_bot_and_controller():
    text = msg.build_propose_text(_entry())
    assert "experiment\\-legacy\\-20" in text  # escaped dashes
    assert "btcusdt\\-1\\-5" in text


def test_propose_text_mentions_dimension_and_direction():
    text = msg.build_propose_text(_entry())
    assert "take\\_profit" in text  # underscore escaped
    assert "increase" in text
    assert "medium" in text


def test_propose_text_includes_reasoning_blockquote():
    text = msg.build_propose_text(_entry())
    # blockquote lines start with `>`
    assert ">" in text
    assert "NATR alto" in text


def test_propose_text_marks_winner_with_star():
    text = msg.build_propose_text(_entry())
    assert "★" in text


def test_propose_text_no_unescaped_special_chars_in_data():
    """The escape helper must run on every dynamic substring so the
    MarkdownV2 parser doesn't choke."""
    entry = _entry()
    # An entry whose controller_id contains markdown specials
    entry["controller_id"] = "bot.x::config_v1.2"
    text = msg.build_propose_text(entry)
    # Dots and underscores must be escaped where they appear from data
    assert "bot\\.x" in text
    assert "config\\_v1\\.2" in text


def test_propose_text_handles_missing_optional_fields():
    """A barebones entry shouldn't crash the builder."""
    minimal = {
        "patch_id": "p1",
        "bot_name": "b1",
        "controller_id": "b1::c1",
        "llm_proposal": {
            "dimension": "take_profit",
            "direction": "increase",
            "magnitude_qualitative": "small",
        },
        "expanded_candidates": [],
        "backtests": {},
        "old_value": None,
    }
    text = msg.build_propose_text(minimal)
    assert "take\\_profit" in text


# ---------------------------------------------------------------------------
# Keyboard
# ---------------------------------------------------------------------------


def test_keyboard_two_rows():
    kb = msg.build_propose_keyboard(_entry())
    assert isinstance(kb, InlineKeyboardMarkup)
    rows = kb.inline_keyboard
    assert len(rows) == 2


def test_keyboard_first_row_winner_then_alts():
    kb = msg.build_propose_keyboard(_entry())
    row1 = kb.inline_keyboard[0]
    # 3 candidates: winner + 2 alts
    assert len(row1) == 3
    # Winner first, with "Apply" label
    assert "Apply" in row1[0].text
    assert "c1" in row1[0].text
    # Alts only show the id
    assert row1[1].text == "c2"
    assert row1[2].text == "c3"


def test_keyboard_second_row_reject_snooze():
    kb = msg.build_propose_keyboard(_entry())
    row2 = kb.inline_keyboard[1]
    assert len(row2) == 2
    assert "Reject" in row2[0].text
    assert "Snooze" in row2[1].text
    assert row2[0].callback_data == "adaptive:reject:p_20260512T120000Z_001"
    assert row2[1].callback_data == "adaptive:snooze:p_20260512T120000Z_001:1h"


def test_keyboard_winner_apply_callback():
    kb = msg.build_propose_keyboard(_entry())
    winner_btn = kb.inline_keyboard[0][0]
    assert winner_btn.callback_data == "adaptive:apply:p_20260512T120000Z_001:c1"


def test_keyboard_handles_selected_not_in_candidates():
    """Defensive: selected may point to a candidate that's not in the list."""
    e = _entry()
    e["selected"] = "c_nonexistent"
    kb = msg.build_propose_keyboard(e)
    # Falls back: no winner-prefix, all 3 candidates as alts
    row1 = kb.inline_keyboard[0]
    assert len(row1) == 3
    assert "Apply" not in row1[0].text


# ---------------------------------------------------------------------------
# Verdict edit texts
# ---------------------------------------------------------------------------


def test_applied_text_mentions_field_and_values():
    text = msg.build_applied_text(
        _entry(),
        candidate_id="c1",
        new_value=0.00045,
        apply_result={"caveats": ["take_profit only affects new executors"]},
        cooldown_until="2026-05-12T13:00:00Z",
    )
    assert "Aplicado" in text
    assert "take\\_profit" in text
    assert "0\\.00045" in text
    assert "new executors" in text


def test_rejected_text_mentions_cooldown():
    text = msg.build_rejected_text(_entry(), cooldown_minutes=60)
    assert "rechazada" in text.lower()
    assert "60 min" in text


def test_rejected_text_handles_missing_cooldown():
    text = msg.build_rejected_text(_entry(), cooldown_minutes=None)
    assert "rechazada" in text.lower()


def test_snoozed_text_includes_deadline():
    text = msg.build_snoozed_text(_entry(), snoozed_until_iso="2026-05-12T13:00:00Z")
    assert "Pospuesto" in text
    assert "13:00:00Z" in text


def test_failed_apply_text_with_dict_error():
    text = msg.build_failed_apply_text(
        _entry(),
        error={"error_code": "api_error", "message": "boom"},
    )
    assert "boom" in text
    assert "Apply falló" in text


def test_failed_apply_text_with_str_error():
    text = msg.build_failed_apply_text(_entry(), error="connection refused")
    assert "connection refused" in text


def test_already_resolved_text_shows_verdict():
    entry = _entry()
    entry["human_verdict"] = "approved"
    entry["verdict_at"] = "2026-05-12T12:01:00Z"
    text = msg.build_already_resolved_text(entry)
    assert "approved" in text
    assert "12:01:00Z" in text
