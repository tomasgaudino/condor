# Smoke test — primer vertical slice

> Qué chequear la primera vez que probás el slice contra tu Condor local.
> Si todo pasa, validamos que la arquitectura anda end-to-end.

## Pre-condiciones

- Condor corriendo con servidor `local` configurado.
- Al menos 1 bot activo con uno o más controllers `pmm_mister`.
- Hummingbot accesible y respondiendo via la API (Condor ya lo usa).

Si todavía no hay bots PMM corriendo, los tests A2/A3/A4 van a mostrar
"no controllers in scope" y no hay nada que validar — primero levantá un bot.

## Test A1 — La routine se descubre

Desde Telegram en Condor, mandá `/routines`.

**Esperás ver**:
- Una nueva entrada `capital_state` bajo categoría "Adaptive Framework".

**Si no aparece**:
- Verificar que `routines/capital_state.py` está en el repo correcto.
- Verificar que se levantó el bot Condor en la branch `feat/pmm_mister_supervisor`.
- Logs: `tail -f` del log del bot al arrancar — debería listar la routine.

## Test A2 — La routine corre desde Telegram

Desde Telegram: `/routines` → `capital_state` → Run.

**Esperás ver**:
- Un mensaje markdown con el monitor (`📊 PORTFOLIO UTILIZATION` ...).
- Seguido de un bloque ` ```json ` con el payload completo.

**Si falla con "Could not connect to API server"**:
- El servidor Condor no tiene API client configurado para la cuenta.
- Verificar `condor/preferences.py` o equivalente.

**Si devuelve "No active bots found"**:
- No hay bots corriendo. Levantá uno y reintentá.

**Si devuelve algo pero los números se ven raros**:
- Anotá qué se ve raro y mandámelo. Casos típicos:
  - `nominal_budget_usd = 0` → `total_amount_quote` o `portfolio_allocation` están en 0.
  - `committed_now_usd = 0` con executors activos → la routine no está leyendo `positions_summary` correctamente. Posible diferencia en el shape del response del API.
  - `wallet_total_value_usd = 0` → `client.portfolio.get_state()` no devuelve nada o el shape es distinto al esperado.

## Test A3 — La CLI corre

Desde una terminal en el root del repo:

```bash
.venv/bin/python -m condor.tools.dashboard --server local
```

**Esperás ver**: el mismo dashboard ASCII que vimos en los smoke tests sintéticos, pero con datos reales.

**Si falla**:
- Anotá el traceback completo.
- Más probable: `WebRoutineContext` no inicializa el cliente igual que el ciclo de Telegram. Hay que ajustar.

## Test A4 — Filtros funcionan

Probá los flags:

```bash
# Solo un bot específico
.venv/bin/python -m condor.tools.dashboard --bot pmm-btc-1

# Todos los controllers (no solo pmm_mister)
.venv/bin/python -m condor.tools.dashboard --controller-filter ""

# Cuenta específica
.venv/bin/python -m condor.tools.dashboard --account master_account
```

**Esperás**: el output cambia coherentemente con el filtro.

## Test A5 — Send to Telegram chat

En la routine desde Telegram, configurar `target_chat_id` con un chat ID válido (puede ser el mismo del DM o un grupo) y correrla.

**Esperás**: el monitor compacto llega al chat objetivo además del JSON al chat actual.

## Cosas conocidas que NO van a andar

- **Percentiles `p50/p95` de utilización**: `history.available: false` siempre.
  Esto es por diseño en este slice — todavía no persistimos snapshots.
- **Demand cross-pair real**: el split base/quote es 50/50 fijo. Si tenés un
  controller muy skewed, el oversub_ratio reportado puede no ser exacto.
- **No hay "alerts" sobre cooldowns / régimen / etc**: solo capital. Las
  otras secciones del dashboard llegan cuando construyamos las otras
  routines.

## Cosas a observar y anotar

Mientras corre, observá:

1. **Performance**:
   - ¿Cuánto tarda la routine? Esperado: <2s en una cuenta típica.
   - Si tarda >5s, hay un problema (probablemente `client.portfolio.get_state(refresh=True)`).

2. **Coherencia de números**:
   - El `nominal_budget` por controller, ¿coincide con lo que ves en la UI de Condor?
   - El `committed_now`, ¿coincide aproximadamente con la suma de tus posiciones abiertas?
   - El `wallet_total`, ¿coincide con tu portfolio en el exchange?

3. **Qué falta o sobra**:
   - ¿Hay alguna métrica que esperabas ver y no está?
   - ¿Hay algo del output que es ruido o redundante?

## Después del smoke test

Mandame:
- Lista de tests A1-A5 con ✅/❌/⚠️ y comentarios.
- Tiempos observados.
- Tracebacks completos si hay errores.
- Cualquier "esto está raro" — incluso si no es un error técnico.

Con eso decidimos:
- Si hay bugs → fix antes de seguir construyendo encima.
- Si todo anda → próxima pieza (history persistence, market_regime, etc.).

## Referencia rápida

Specs relacionados:
- [`CAPITAL_DYNAMICS.md`](CAPITAL_DYNAMICS.md) — diseño de la routine.
- [`KPIS_AND_DASHBOARD.md`](KPIS_AND_DASHBOARD.md) — KPIs y layout del tablero.
- [`AGENT_SCHEMA_SPEC.md`](AGENT_SCHEMA_SPEC.md) — convenciones de identidad y persistence.

Código en este slice:
- `routines/capital_state.py` — la routine.
- `condor/trading_agent/adaptive/state_io.py` — helpers de IO (no usados en este slice, listos para next).
- `condor/tools/dashboard.py` — CLI.
- `tests/` — 69 tests.
