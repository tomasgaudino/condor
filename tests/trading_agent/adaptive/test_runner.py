"""Tests for condor.trading_agent.adaptive.runner.

Focus: pure functions and the CLI parser. The full ``tick_once``
integration is exercised by the smoke test against brigado, not here —
mocking ``with_client`` + all three routines + the orchestrator would
duplicate every test from the routine and orchestrator suites.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from condor.trading_agent.adaptive import runner


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# CLI parser
# ---------------------------------------------------------------------------


def test_parser_requires_agent_slug():
    p = runner._build_parser()
    with pytest.raises(SystemExit):
        p.parse_args([])


def test_parser_defaults():
    args = runner._build_parser().parse_args(["adaptive_pmm"])
    assert args.agent_slug == "adaptive_pmm"
    assert args.mode is None              # falls back to agent config
    assert args.server is None
    assert args.llm == "mock-no-action"
    assert args.once is False             # not set; --once is implicit
    assert args.loop is False
    assert args.max_ticks == 0
    assert args.tick_id == 1


def test_parser_explicit_flags():
    args = runner._build_parser().parse_args([
        "adaptive_pmm",
        "--server", "brigado",
        "--mode", "shadow",
        "--llm", "mock-propose",
        "--loop",
        "--max-ticks", "5",
        "-vv",
    ])
    assert args.server == "brigado"
    assert args.mode == "shadow"
    assert args.llm == "mock-propose"
    assert args.loop is True
    assert args.max_ticks == 5
    assert args.verbose == 2


def test_parser_rejects_invalid_mode():
    with pytest.raises(SystemExit):
        runner._build_parser().parse_args(["adaptive_pmm", "--mode", "yolo"])


def test_parser_rejects_invalid_llm_kind():
    with pytest.raises(SystemExit):
        runner._build_parser().parse_args(["adaptive_pmm", "--llm", "gpt"])


# ---------------------------------------------------------------------------
# load_agent
# ---------------------------------------------------------------------------


def test_load_agent_real_adaptive_pmm():
    """The repo includes the real adaptive_pmm agent; load it."""
    strategy, body, adaptive_cfg, inv = runner.load_agent("adaptive_pmm")
    assert strategy.name == "Adaptive PMM Supervisor"
    assert "Adaptive PMM Supervisor" in body or "adaptive" in body.lower()
    assert adaptive_cfg.get("mode") == "propose"
    assert "take_profit" in inv.allowed_fields


def test_load_agent_missing_raises():
    with pytest.raises(ValueError, match="not found"):
        runner.load_agent("does_not_exist_xyz")


# ---------------------------------------------------------------------------
# Mock LLMs
# ---------------------------------------------------------------------------


def test_mock_no_action_returns_no_action():
    llm = runner.build_llm_client(kind="mock-no-action")
    raw = _run(llm("anything"))
    parsed = json.loads(raw)
    assert parsed["action"] == "no_action"


def test_mock_propose_extracts_controller_from_prompt():
    llm = runner.build_llm_client(kind="mock-propose")
    prompt = (
        "Some preamble\n"
        "{\n"
        '    "controllers": ["bot1::ctrl_a"]\n'
        "}\n"
        "more text\n"
    )
    raw = _run(llm(prompt))
    parsed = json.loads(raw)
    assert parsed["action"] == "propose"
    assert parsed["proposals"][0]["controller_id"] == "bot1::ctrl_a"
    assert parsed["proposals"][0]["dimension"] == "take_profit"
    assert parsed["proposals"][0]["direction"] == "increase"
    assert parsed["proposals"][0]["magnitude_qualitative"] == "medium"


def test_mock_propose_falls_back_when_no_controller_in_prompt():
    llm = runner.build_llm_client(kind="mock-propose")
    raw = _run(llm("no ids here"))
    parsed = json.loads(raw)
    assert parsed["action"] == "no_action"


def test_real_llm_stub_raises_with_clear_message():
    llm = runner.build_llm_client(kind="real", agent_key="claude-code")
    with pytest.raises(NotImplementedError, match="not implemented"):
        _run(llm("any"))


def test_unknown_llm_kind_raises():
    with pytest.raises(ValueError, match="unknown"):
        runner.build_llm_client(kind="banana")


# ---------------------------------------------------------------------------
# _extract_first_controller
# ---------------------------------------------------------------------------


def test_extract_controller_skips_agent_md_placeholder():
    """The agent.md example output mentions ``<bot_name>::<config_name>``
    as a placeholder. We must NOT pick that up."""
    prompt = (
        '"controller_id": "<bot_name>::<config_name>",\n'
        '"reasoning": "..."\n'
        '{"controllers": ["real_bot::real_ctrl"]}'
    )
    found = runner._extract_first_controller(prompt)
    assert found == "real_bot::real_ctrl"


def test_extract_controller_skips_code_fenced_lines():
    """Lines inside markdown code fences contain backticks — skip them."""
    prompt = (
        '`example_bot::example_ctrl`\n'  # markdown inline code
        '{"controllers": ["live_bot::live_ctrl"]}'
    )
    found = runner._extract_first_controller(prompt)
    assert found == "live_bot::live_ctrl"


def test_extract_controller_real_brigado_id():
    """Confirm the regex accepts the real ID format from brigado."""
    real = ("experiment-legacy-20_v2_rebalanced-20260509-005920::"
            "pmm_mister_binance_btcusdt-1-5_exp_a_legacy")
    prompt = f'{{"controller_id": "{real}"}}'
    assert runner._extract_first_controller(prompt) == real


def test_extract_controller_returns_none_when_absent():
    assert runner._extract_first_controller("nothing here") is None


# ---------------------------------------------------------------------------
# Main entry — argument validation
# ---------------------------------------------------------------------------


def test_main_rejects_once_and_loop_together(capsys):
    rc = runner.main(["adaptive_pmm", "--once", "--loop"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "mutually exclusive" in captured.err
