---
name: condor-express
description: Query a live Condor server (bots, controllers, balances, prices) without spinning up a Telegram session. Use this whenever you need an operational data point about the user's running setup — controller config, current positions, wallet balance, prices — to validate a hypothesis or unstick a debugging conversation. The user has servers configured locally in `config_manager` and we reuse those credentials directly.
---

# Condor Express

Lightweight bridge to a live Condor server. Use this when you need to
**check operational data in real time** without going through the
back-and-forth of asking the user to run a Telegram routine and paste
the output.

## When to use this skill

- The user asks "why is X showing Y?" and you need to inspect the
  controller config or live state to answer.
- You're about to make a code change and want to verify a data shape
  against a real response (instead of reading source and guessing).
- You want to confirm a fix end-to-end after editing a routine — pull
  the live state and compare with expected.
- You spot a discrepancy in a number reported by a routine (e.g.
  `Capital: $0` despite active positions) and want to drill down
  without stopping the conversation.

## When NOT to use this skill

- For implementation logic that lives inside Condor — write the routine
  or handler, don't pull state from outside.
- For one-time exploration of a brand-new API surface — read the
  source first.
- If the user explicitly wants the answer to come from *their* Telegram
  session (e.g. a perspective check, a UI render).

## Pre-conditions

- The user must have the target server registered locally in
  ``config_manager`` (the local file Condor reads). Check with::

      .venv/bin/python -c "from condor.tools.condor_inspect import list_known_servers; print(list_known_servers())"

  If the server isn't there, the user hasn't configured it locally and
  this skill can't reach it.
- The Hummingbot API on the server must be reachable from the agent's
  network (no special VPN dance — same as the user's Telegram bot).

## How to invoke

Build a one-shot Python script with the helpers from
``condor.tools.condor_inspect`` and run it via the project's venv. Always
finish with ``await cleanup()`` to avoid noisy aiohttp warnings.

### Recipe 1: list active bots and their controllers

```bash
.venv/bin/python -c "
import asyncio
from condor.tools.condor_inspect import list_active_bots, list_controllers, cleanup

async def main():
    print('bots:', await list_active_bots('brigado'))
    ctrls = await list_controllers('brigado')
    for c in ctrls:
        print(f'  {c[\"bot_name\"]} :: {c[\"config_name\"]} '
              f'({c[\"trading_pair\"]} on {c[\"connector_name\"]})')
    await cleanup()

asyncio.run(main())
"
```

### Recipe 2: deep dive on a specific controller

```bash
.venv/bin/python -c "
import asyncio
from condor.tools.condor_inspect import get_controller_state, format_controller_summary, cleanup

async def main():
    state = await get_controller_state(
        'brigado',
        'experiment-legacy-20_v2_rebalanced-20260509-005920',
        'pmm_mister_binance_btcusdt-1-5_exp_a_legacy',
    )
    print(format_controller_summary(state))
    await cleanup()

asyncio.run(main())
"
```

The ``get_controller_state`` output includes:
- ``config``: full YAML config of the controller.
- ``performance``: inner performance dict from the bot's MQTT snapshot.
- ``positions_summary``: list of open positions with ``amount``,
  ``breakeven_price``, ``side``, ``unrealized_pnl_quote``.
- ``computed``: nominal_budget_usd, worst_case_usd, committed_now_usd,
  utilization_now, active_executors.

### Recipe 3: wallet snapshot

```bash
.venv/bin/python -c "
import asyncio, json
from condor.tools.condor_inspect import get_wallet_state, flatten_balances, cleanup

async def main():
    raw = await get_wallet_state('brigado')
    flat = flatten_balances(raw)
    for token, info in sorted(flat.items(), key=lambda kv: -kv[1]['value_usd']):
        print(f'  {token:<6} {info[\"balance\"]:>12.4f}  ${info[\"value_usd\"]:>10,.2f}')
    await cleanup()

asyncio.run(main())
"
```

### Recipe 4: current prices for a few pairs

```bash
.venv/bin/python -c "
import asyncio
from condor.tools.condor_inspect import get_prices, cleanup

async def main():
    prices = await get_prices('brigado', 'binance', ['BTC-USDT', 'ETH-USDT'])
    for pair, price in prices.items():
        print(f'  {pair}: {price}')
    await cleanup()

asyncio.run(main())
"
```

### Recipe 5: arbitrary query via low-level context manager

When the helpers don't cover what you need, use ``with_client``:

```bash
.venv/bin/python -c "
import asyncio, json
from condor.tools.condor_inspect import with_client, cleanup

async def main():
    async with with_client('brigado') as client:
        # call any client method directly — see hummingbot_api_client docs
        # for the full surface (accounts, controllers, executors, market_data,
        # portfolio, trading, gateway, gateway_swap, gateway_clmm, etc.)
        accts = await client.accounts.list_accounts()
        print(json.dumps(accts, indent=2, default=str))
    await cleanup()

asyncio.run(main())
"
```

## Available helpers

In ``condor/tools/condor_inspect.py``:

- ``list_known_servers()`` — local-only, no network. Returns names.
- ``list_active_bots(server_name)`` — names of running bots.
- ``list_controllers(server_name, bot_name=None)`` — flat list with
  per-controller config summary; ``_raw`` key holds the full config.
- ``get_controller_state(server_name, bot_name, config_name)`` — full
  snapshot (config + performance + positions + computed metrics).
- ``get_wallet_state(server_name, account_name=None)`` — raw balances
  by account/connector.
- ``flatten_balances(raw)`` — pure helper, aggregates balances by token.
- ``get_prices(server_name, connector_name, trading_pairs)`` — current
  mid prices.
- ``with_client(server_name)`` — async context manager yielding the raw
  hummingbot-api client for arbitrary calls.
- ``cleanup()`` — close cached HTTP clients (call once at script end).
- ``format_controller_summary(state)`` — pretty-print
  ``get_controller_state`` output as a one-page text block.

## Conventions

- **Always `await cleanup()` at the end** of the script. Otherwise you
  get noisy ``Unclosed client session`` warnings (cosmetic, not a bug).
- The default server in production is whatever the user has set as
  active. If you're not sure which to use, list with
  ``list_known_servers()`` and ask the user. **Don't guess.**
- Errors are NOT swallowed — exceptions bubble up with full
  tracebacks. If a server is unreachable or the API errors, you'll see
  the failure clearly.
- The helpers return plain dicts/lists. Convert to JSON for verbose
  dumps with ``json.dumps(out, indent=2, default=str)`` (the
  ``default=str`` covers Decimal and datetime).

## Reporting back to the user

When you use this skill to answer a question or validate a hypothesis,
**show your work**: include the relevant numbers/keys you saw, not just
your conclusion. The user can then decide whether to dig deeper or trust
the read. Example:

> I checked `get_controller_state` for that controller and found:
> nominal=$210, committed=$1565 (1 position of 0.019 BTC at $80545
> breakeven). So the utilization of 745% is real, not a bug.

Avoid sharing the full JSON dump unless the user asks — extract only
the fields that matter for the question at hand.
