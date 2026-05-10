# Adaptive Strategy Framework — Design

> Estado: **borrador inicial** — iterando con el usuario.
> Última actualización: 2026-05-09

## Problema

Los controllers `pmm_mister` arrancan con buenas configs y rinden bien las primeras horas (volumen alto, PnL positivo). Con el tiempo:

- El mercado se consolida, las posiciones se acumulan en una dirección.
- El controller queda "atrapado": holdea inventario, tradea menos, los movimientos de precio lo desfavorecen.
- Hoy hay que: detectar el problema → frenar el bot → analizar manualmente → decidir nueva config → relanzar.

Queremos que un **agente** detecte estas situaciones y **modifique la config del controller en vivo** para mantenerlo en zonas favorables del mercado.

## Visión

Tres capas que se alimentan entre sí:

```
┌──────────────────────────────────────────────────────────────┐
│  ROUTINES (data sources estandarizados)                       │
│  - market_regime, inventory_drift, pnl_velocity, etc.         │
└────────────────────────┬─────────────────────────────────────┘
                         │ outputs estructurados
                         ▼
┌──────────────────────────────────────────────────────────────┐
│  AGENTE (cada tick: lee routines + reglas + decide)          │
│  - reglas del usuario (config)                                │
│  - LLM razona sobre evidencia                                 │
│  - propone modificación de controller config                  │
└────────────────────────┬─────────────────────────────────────┘
                         │ patches de config
                         ▼
┌──────────────────────────────────────────────────────────────┐
│  CONTROLLER (pmm_mister corriendo en Hummingbot)             │
│  - aplica nueva config sin reiniciar                          │
└──────────────────────────────────────────────────────────────┘
```

## Estado actual del repo (lo que ya existe)

- **`pmm_mister`** — controller en `handlers/bots/controllers/pmm_mister/config.py` con 30+ campos de config.
- **MCP tools** disponibles desde el agente para leer estado de bots, controllers, market data, ejecutar swaps, etc.

### `pmm_mister_supervisor` — descartado del scope, retenido como aprendizaje

Existe hoy un agente `pmm_mister_supervisor` + routine homónima. **No es la base del nuevo framework** y queda fuera del scope: solo detecta "fuera de rango" y manda alertas, sin contexto ni acción, y arrastra varios bugs de integración.

Sus fallas son una **lista de requisitos negativos** para el nuevo diseño. Ver [`LESSONS_FROM_SUPERVISOR.md`](LESSONS_FROM_SUPERVISOR.md) para el detalle. Resumen de lo que el nuevo framework debe evitar:
- Routines que acoplan data fetching con delivery (rompen fuera de su contexto original).
- Detección sin diagnóstico ni acción → genera ruido perpetuo.
- Falta de memoria entre ticks → no se puede distinguir "primera vez" de "vez #20".
- Falta de cooldowns / hysteresis → thrashing y alertas duplicadas.
- Reglas en prosa sin guardrails de código duro → el LLM puede pasarse.

El nuevo framework arranca de cero, pero respeta esas lecciones como invariantes de diseño.

## Decisiones tomadas

Ver [`DECISIONS.md`](DECISIONS.md) para el log completo. Resumen:

| ID | Tema | Decisión |
|---|---|---|
| D1 | Hot-reload | ✅ Soportado nativamente via Pydantic `is_updatable` |
| D2 | Campos modificables | Diferido (shortlist en CONTROLLER_DEEP_DIVE.md) |
| D3 | Forma de reglas | Híbrido: prosa (LLM) + invariants (código) |
| D4 | Modos de operación | Implementar los 3 (`propose`/`shadow`/`auto`); MVP en `propose` |
| D5 | MVP rule | "Lateral + std dev alta + NATR > threshold → subir take_profit" |
| D6 | Multi-controller | 1 agente para todos |
| D7 | Kill switch | Siempre humano. Circuit breaker = booleano simple |
| D8 | Auditabilidad | Append-only, inmutable, persistente entre sesiones |

## Capas en detalle

### 1. Routines (data sources)

Una routine = una "lente" sobre el mercado o el bot. Output JSON estructurado, sin side effects de delivery (lección L1 del supervisor).

**Routines requeridas para el MVP** (D5):
- **`capital_state`** — estado de capital en 3 planos (controller-local, inter-controller por activo, cross-pair). Ver [`CAPITAL_DYNAMICS.md`](CAPITAL_DYNAMICS.md). Es **prerequisito de toda rule que toque sizing o timing**, y alimenta el invariant duro de "no sobre-suscripción".
- **`market_regime`** — multi-timeframe (5m/1h/1d). Clasifica régimen + favorabilidad. Incluye todos los indicadores de volatilidad necesarios (NATR, BB width, ATR, realized vol). Ver [`MARKET_REGIME_SPEC.md`](MARKET_REGIME_SPEC.md).
- **`controller_performance`** — performance del controller cruzada con datos del exchange. Incluye: market share (volumen propio / volumen total del par), fill rate, PnL velocity, executor turnover, gross spread captured. Es **input para detectar período subóptimo** (D9) y para evaluar el agente en modo `shadow`.

**Routines reutilizables del catálogo (no MVP)**:
- `inventory_drift` — desbalance de inventario respecto al target, duración del estado.
- `pnl_velocity` — derivada del PnL.
- `spread_efficiency` — relación fills/spreads.
- `price_zone` — precio actual vs avg entry del inventory.
- `controller_health` — wrapper sobre las métricas internas del controller (las 12+ que expone `processed_data`, ver CONTROLLER_DEEP_DIVE.md).

Cada routine debe ser:
- **Determinística**: mismos inputs → mismo output.
- **Cacheable**: tick T y tick T+1 con mismo timeframe pueden reusar cómputo.
- **Económica**: nada de inferencia LLM acá.
- **Sin side effects de delivery**: solo retorna datos.

### 2. Agente (decision layer)

**1 agente, N controllers** (D6). Por tick:

1. Para cada controller supervisado, lee outputs de las routines configuradas.
2. Combina con la **policy** del usuario (prosa en `policy.md` o sección de `agent.md`).
3. Le pasa al LLM:
   - Snapshot estructurado de todas las routines (por controller).
   - Config actual de cada controller.
   - Policy en prosa.
   - Historial reciente de decisiones (last N ticks).
   - **Estado persistente** del agente (último cambio aplicado por controller, cooldown remaining, etc.) — lección L3 del supervisor.
4. LLM responde con una **lista de propuestas de patch** (1 por controller) o `"no_action"`.
5. **Invariants check** (D3): cada propuesta pasa por código duro:
   - Cooldown respetado.
   - Delta dentro del max permitido por campo.
   - Campo está en whitelist (no `manual_kill_switch`, etc.).
   - `validate_config()` del controller.
6. **Dispatch según modo** (D4):
   - `propose` → manda a Telegram con botones (lección L5: separar decisión de delivery).
   - `shadow` → solo loggea.
   - `auto` → aplica via MCP tool.
7. **Audit log inmutable** (D8): registra evidencia, propuesta, decisión humana (si aplica), resultado.

### 3. Controller (execution layer)

**Hot-reload confirmado** (D1, ver CONTROLLER_DEEP_DIVE.md):
- 25 campos `is_updatable` se mutan in-place.
- Los cambios surten efecto en el próximo tick sin restart.
- Caveat: `take_profit` y similares solo afectan executors **nuevos** — los activos retienen el TP con el que nacieron.

**MCP tool a construir**: `update_controller_config(bot_name, controller_id, patch)` que:
- Valida que cada campo esté en la lista de `is_updatable`.
- Aplica `setattr` al config object del controller corriendo.
- Retorna confirmación + lista de campos efectivamente cambiados.

## Restricciones / no-objetivos

- **No es backtesting**: corre live, dinero real.
- **No es estrategia nueva**: seguimos con `pmm_mister`, lo *adaptamos*.
- **No es full-auto sin red en MVP**: arrancamos en modo `propose` (D4).
- **No tocamos kill switch ni leverage automáticamente** (D7).

## Próximos pasos

Ver [`PROGRESS.md`](PROGRESS.md).
