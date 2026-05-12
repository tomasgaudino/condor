"""Tests for handlers.adaptive (the callback handler + send_proposal).

The handler is async and integrates with telegram-bot-python + the MCP
tool. We mock everything external:
- ``query`` / ``update`` / ``context`` — minimal fakes with the methods
  the handler calls.
- The MCP tool function — monkeypatched on the import path.
- Agent dir resolution — pointed at a tmp_path.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import yaml

import handlers.adaptive as ad
from condor.trading_agent.adaptive import audit, state_io


UTC = timezone.utc


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _adaptive_agent_dir(tmp_path: Path) -> Path:
    """Build a minimal agent dir layout the handler will recognize."""
    d = tmp_path / "myagent"
    (d / "state").mkdir(parents=True)
    # Minimal valid invariants.yaml (matches the loader's expectations).
    inv = {
        "version": "1.0",
        "allowed_fields": ["take_profit"],
        "forbidden_fields": ["manual_kill_switch"],
        "default_max_delta": {
            "percentage_type": {"max_increase": 0.5, "max_decrease": 0.5},
            "absolute_type":   {"max_increase": 0.1, "max_decrease": 0.1},
        },
        "field_type_map": {
            "percentage_type": ["take_profit"],
            "absolute_type": [],
        },
        "field_overrides": {},
        "cooldowns": {
            "per_controller_minutes": 30,
            "per_controller_per_field_minutes": 60,
            "global_minutes": 5,
        },
        "capital": {"safety_margin": 0.95, "block_increases_above_oversub": 0.85},
        "circuit_breaker": {"enabled": False},
        "backtest": {"unsupported_connectors": [], "on_backtest_failure": "block"},
        "auto_mode": {"delta_multiplier": 0.5, "require_backtest_evidence": True, "cooldown_multiplier": 2.0},
    }
    (d / "invariants.yaml").write_text(yaml.safe_dump(inv))
    return d


@pytest.fixture
def patched_agents_root(tmp_path, monkeypatch):
    """Repoint the handler's agents root at tmp_path."""
    monkeypatch.setattr(ad, "_AGENTS_ROOT", tmp_path)
    return tmp_path


def _proposal_entry(patch_id: str = "p_20260512T120000Z_001") -> dict:
    return {
        "patch_id": patch_id,
        "ts": "2026-05-12T12:00:00Z",
        "bot_name": "bot1",
        "config_name": "c1",
        "controller_id": "bot1::c1",
        "trading_pair": "BTC-USDT",
        "connector_name": "binance",
        "regime_observed": "mean_reverting_high_vol",
        "suboptimal_minutes": 47,
        "warmup_status": "normal",
        "llm_proposal": {
            "dimension": "take_profit",
            "direction": "increase",
            "magnitude_qualitative": "medium",
            "reasoning": "test",
            "caveats": [],
        },
        "expanded_candidates": [
            {"id": "c1", "value": 0.00045},
            {"id": "c2", "value": 0.00060},
        ],
        "backtests": {
            "baseline":  {"net_pnl_quote": 0.1, "volume": 1000, "score": 1.0},
            "candidate1": {"net_pnl_quote": 0.4, "volume": 800, "score": 1.4},
            "candidate2": {"net_pnl_quote": 0.7, "volume": 700, "score": 1.3},
        },
        "selected": "c1",
        "selected_value": 0.00045,
        "old_value": 0.0003,
        "human_verdict": "pending",
        "verdict_at": None,
        "verdict_chose_candidate": None,
        "verdict_by": None,
        "applied": False,
        "applied_at": None,
        "apply_result": None,
        "apply_error": None,
    }


class _FakeQuery:
    """Minimal CallbackQuery stand-in."""

    def __init__(self, data: str):
        self.data = data
        self.answer = AsyncMock(return_value=None)
        self.edit_message_text = AsyncMock(return_value=None)


class _FakeUser:
    def __init__(self, user_id: int = 42):
        self.id = user_id


class _FakeUpdate:
    def __init__(self, query: _FakeQuery, user_id: int = 42):
        self.callback_query = query
        self.effective_user = _FakeUser(user_id)


class _FakeContext:
    def __init__(self):
        self._chat_id = 0


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# _candidate_agent_dirs + _resolve_agent_for_patch
# ---------------------------------------------------------------------------


def test_candidate_dirs_filters_to_invariants_yaml(patched_agents_root):
    # legacy agent dir without invariants.yaml — must be ignored
    legacy = patched_agents_root / "legacy"
    legacy.mkdir()
    (legacy / "agent.md").write_text("---\nname: x\n---")
    # adaptive
    adaptive = _adaptive_agent_dir(patched_agents_root)

    candidates = ad._candidate_agent_dirs()
    assert adaptive in candidates
    assert legacy not in candidates


def test_resolve_agent_for_patch_finds_entry(patched_agents_root):
    d = _adaptive_agent_dir(patched_agents_root)
    audit.append_audit_entry(d, _proposal_entry())
    out = ad._resolve_agent_for_patch("p_20260512T120000Z_001")
    assert out is not None
    agent_dir, entry = out
    assert agent_dir == d
    assert entry["patch_id"] == "p_20260512T120000Z_001"


def test_resolve_agent_for_patch_missing(patched_agents_root):
    _adaptive_agent_dir(patched_agents_root)
    assert ad._resolve_agent_for_patch("not_a_patch") is None


# ---------------------------------------------------------------------------
# _parse_duration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("s,expected", [
    ("1h", timedelta(hours=1)),
    ("30m", timedelta(minutes=30)),
    ("2d", timedelta(days=2)),
    ("1.5h", timedelta(hours=1.5)),
])
def test_parse_duration_valid(s, expected):
    assert ad._parse_duration(s) == expected


@pytest.mark.parametrize("s", ["", "h", "1x", "-1h", "abc", "1"])
def test_parse_duration_invalid(s):
    assert ad._parse_duration(s) is None


# ---------------------------------------------------------------------------
# Callback handler — patch resolution failures
# ---------------------------------------------------------------------------


def test_callback_unknown_patch_edits_message(patched_agents_root):
    _adaptive_agent_dir(patched_agents_root)
    q = _FakeQuery("adaptive:reject:p_nonexistent")
    upd = _FakeUpdate(q)
    ctx = _FakeContext()
    _run(ad.adaptive_callback_handler(upd, ctx))
    q.answer.assert_called_once()
    q.edit_message_text.assert_called_once()
    args, kwargs = q.edit_message_text.call_args
    assert "no encontrada" in args[0]


def test_callback_malformed_data_drops_silently(patched_agents_root):
    q = _FakeQuery("not_adaptive:reject:foo")
    upd = _FakeUpdate(q)
    _run(ad.adaptive_callback_handler(upd, _FakeContext()))
    q.answer.assert_called_once()
    # No message edit — the namespace didn't match.
    q.edit_message_text.assert_not_called()


def test_callback_already_resolved_shows_state(patched_agents_root):
    d = _adaptive_agent_dir(patched_agents_root)
    e = _proposal_entry()
    e["human_verdict"] = "approved"
    e["verdict_at"] = "2026-05-12T12:01:00Z"
    audit.append_audit_entry(d, e)

    q = _FakeQuery(f"adaptive:apply:{e['patch_id']}:c1")
    _run(ad.adaptive_callback_handler(_FakeUpdate(q), _FakeContext()))

    q.edit_message_text.assert_called_once()
    args, _ = q.edit_message_text.call_args
    assert "approved" in args[0]


# ---------------------------------------------------------------------------
# Apply — success path
# ---------------------------------------------------------------------------


def test_apply_success_writes_audit_and_last_changes(patched_agents_root, monkeypatch):
    d = _adaptive_agent_dir(patched_agents_root)
    e = _proposal_entry()
    audit.append_audit_entry(d, e)

    # Mock the MCP tool to return success
    async def fake_update_controller_config(**kw):
        return {
            "success": True,
            "old_value": 0.0003,
            "new_value": 0.00045,
            "caveats": ["take_profit only affects new executors"],
            "applied_at": "2026-05-12T12:00:01Z",
            "hot_reload_eta_seconds": 10,
            "raw_api_result": {"message": "ok"},
        }
    monkeypatch.setattr(
        "mcp_servers.hummingbot_api.tools.adaptive_agent.update_controller_config",
        fake_update_controller_config,
    )

    # Mock the HB client resolver
    async def fake_get_hb_client(ctx):
        return object()
    monkeypatch.setattr(ad, "_get_hb_client", fake_get_hb_client)

    q = _FakeQuery(f"adaptive:apply:{e['patch_id']}:c1")
    _run(ad.adaptive_callback_handler(_FakeUpdate(q), _FakeContext()))

    # Audit log was updated to approved + applied
    updated = audit.find_audit_entry(d, e["patch_id"])
    assert updated["human_verdict"] == "approved"
    assert updated["verdict_chose_candidate"] == "c1"
    assert updated["verdict_by"] == 42
    assert updated["applied"] is True
    assert updated["apply_result"]["new_value"] == 0.00045

    # last_changes also updated
    lc = audit.get_last_change(d, "bot1::c1", "take_profit")
    assert lc is not None
    assert lc["new"] == 0.00045
    assert lc["marker"] == "applied"

    # Confirmation message
    q.edit_message_text.assert_called_once()
    args, _ = q.edit_message_text.call_args
    assert "Aplicado" in args[0]


def test_apply_unknown_candidate(patched_agents_root, monkeypatch):
    d = _adaptive_agent_dir(patched_agents_root)
    e = _proposal_entry()
    audit.append_audit_entry(d, e)

    q = _FakeQuery(f"adaptive:apply:{e['patch_id']}:c99")
    _run(ad.adaptive_callback_handler(_FakeUpdate(q), _FakeContext()))

    # No audit transition to approved — the entry stays pending
    entry = audit.find_audit_entry(d, e["patch_id"])
    assert entry["human_verdict"] == "pending"

    q.edit_message_text.assert_called_once()
    args, _ = q.edit_message_text.call_args
    assert "c99" in args[0]


def test_apply_mcp_domain_failure_marks_apply_error(patched_agents_root, monkeypatch):
    d = _adaptive_agent_dir(patched_agents_root)
    e = _proposal_entry()
    audit.append_audit_entry(d, e)

    async def failing_tool(**kw):
        return {"success": False, "error_code": "value_changed_externally", "message": "race"}

    monkeypatch.setattr(
        "mcp_servers.hummingbot_api.tools.adaptive_agent.update_controller_config",
        failing_tool,
    )
    async def fake_get_hb_client(ctx):
        return object()
    monkeypatch.setattr(ad, "_get_hb_client", fake_get_hb_client)

    q = _FakeQuery(f"adaptive:apply:{e['patch_id']}:c1")
    _run(ad.adaptive_callback_handler(_FakeUpdate(q), _FakeContext()))

    entry = audit.find_audit_entry(d, e["patch_id"])
    # verdict was approved (the human DID click apply) but applied=False
    assert entry["human_verdict"] == "approved"
    assert entry["applied"] is False
    assert isinstance(entry["apply_error"], dict)
    assert entry["apply_error"]["error_code"] == "value_changed_externally"

    args, _ = q.edit_message_text.call_args
    assert "Apply falló" in args[0]
    assert "race" in args[0]


def test_apply_mcp_raises_uncaught(patched_agents_root, monkeypatch):
    """If the MCP tool raises (a bug, not a domain error), the handler
    must catch it and surface in the audit log + message."""
    d = _adaptive_agent_dir(patched_agents_root)
    e = _proposal_entry()
    audit.append_audit_entry(d, e)

    async def buggy_tool(**kw):
        raise RuntimeError("simulated bug")

    monkeypatch.setattr(
        "mcp_servers.hummingbot_api.tools.adaptive_agent.update_controller_config",
        buggy_tool,
    )
    async def fake_get_hb_client(ctx):
        return object()
    monkeypatch.setattr(ad, "_get_hb_client", fake_get_hb_client)

    q = _FakeQuery(f"adaptive:apply:{e['patch_id']}:c1")
    _run(ad.adaptive_callback_handler(_FakeUpdate(q), _FakeContext()))

    entry = audit.find_audit_entry(d, e["patch_id"])
    assert entry["applied"] is False
    assert "simulated bug" in str(entry["apply_error"])


def test_apply_no_hb_client_marks_error(patched_agents_root, monkeypatch):
    d = _adaptive_agent_dir(patched_agents_root)
    e = _proposal_entry()
    audit.append_audit_entry(d, e)

    async def no_client(ctx):
        return None
    monkeypatch.setattr(ad, "_get_hb_client", no_client)

    q = _FakeQuery(f"adaptive:apply:{e['patch_id']}:c1")
    _run(ad.adaptive_callback_handler(_FakeUpdate(q), _FakeContext()))

    entry = audit.find_audit_entry(d, e["patch_id"])
    assert entry["applied"] is False
    assert "client" in entry["apply_error"].lower()


# ---------------------------------------------------------------------------
# Reject
# ---------------------------------------------------------------------------


def test_reject_marks_audit_and_writes_cooldown(patched_agents_root):
    d = _adaptive_agent_dir(patched_agents_root)
    e = _proposal_entry()
    audit.append_audit_entry(d, e)

    q = _FakeQuery(f"adaptive:reject:{e['patch_id']}")
    _run(ad.adaptive_callback_handler(_FakeUpdate(q), _FakeContext()))

    entry = audit.find_audit_entry(d, e["patch_id"])
    assert entry["human_verdict"] == "rejected"
    assert entry["verdict_by"] == 42

    lc = audit.get_last_change(d, "bot1::c1", "take_profit")
    assert lc is not None
    assert lc["marker"] == "rejected"
    assert lc["old"] == lc["new"] == 0.0003

    args, _ = q.edit_message_text.call_args
    assert "rechazada" in args[0].lower()


# ---------------------------------------------------------------------------
# Snooze
# ---------------------------------------------------------------------------


def test_snooze_marks_audit_with_deadline(patched_agents_root):
    d = _adaptive_agent_dir(patched_agents_root)
    e = _proposal_entry()
    audit.append_audit_entry(d, e)

    q = _FakeQuery(f"adaptive:snooze:{e['patch_id']}:1h")
    _run(ad.adaptive_callback_handler(_FakeUpdate(q), _FakeContext()))

    entry = audit.find_audit_entry(d, e["patch_id"])
    assert entry["human_verdict"] == "snoozed"
    assert entry["snoozed_until_ts"] is not None

    lc = audit.get_last_change(d, "bot1::c1", "take_profit")
    assert lc["marker"] == "snoozed"
    assert "cooldown_until" in lc


def test_snooze_invalid_duration(patched_agents_root):
    d = _adaptive_agent_dir(patched_agents_root)
    e = _proposal_entry()
    audit.append_audit_entry(d, e)

    q = _FakeQuery(f"adaptive:snooze:{e['patch_id']}:xyz")
    _run(ad.adaptive_callback_handler(_FakeUpdate(q), _FakeContext()))

    # Audit should NOT have transitioned
    entry = audit.find_audit_entry(d, e["patch_id"])
    assert entry["human_verdict"] == "pending"

    args, _ = q.edit_message_text.call_args
    assert "inválida" in args[0]


# ---------------------------------------------------------------------------
# Unknown action
# ---------------------------------------------------------------------------


def test_unknown_action(patched_agents_root):
    d = _adaptive_agent_dir(patched_agents_root)
    e = _proposal_entry()
    audit.append_audit_entry(d, e)

    q = _FakeQuery(f"adaptive:nuke:{e['patch_id']}")
    _run(ad.adaptive_callback_handler(_FakeUpdate(q), _FakeContext()))

    entry = audit.find_audit_entry(d, e["patch_id"])
    assert entry["human_verdict"] == "pending"
    args, _ = q.edit_message_text.call_args
    assert "desconocida" in args[0]


# ---------------------------------------------------------------------------
# send_proposal
# ---------------------------------------------------------------------------


def test_send_proposal_appends_and_records_message_id(patched_agents_root):
    d = _adaptive_agent_dir(patched_agents_root)

    class _FakeBot:
        def __init__(self):
            self.send_message = AsyncMock()
            self.send_message.return_value = type("M", (), {"message_id": 18234})()

    bot = _FakeBot()
    entry = _proposal_entry()
    # remove fields send_proposal is supposed to populate
    for k in ("human_verdict", "verdict_at", "verdict_by", "verdict_chose_candidate",
              "applied", "applied_at", "apply_result", "apply_error"):
        entry.pop(k, None)

    sent = _run(ad.send_proposal(bot, d, entry, chat_id=-3055388363))

    bot.send_message.assert_called_once()
    # Entry on disk has the message_id + pending verdict
    stored = audit.find_audit_entry(d, entry["patch_id"])
    assert stored["telegram_message_id"] == 18234
    assert stored["telegram_chat_id"] == -3055388363
    assert stored["human_verdict"] == "pending"
    # Returned dict also has them
    assert sent["telegram_message_id"] == 18234
