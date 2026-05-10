# Catchup ejecutivo — Adaptive Strategy Framework

> Para compartir con el equipo. Lectura: 2 minutos.
> Snapshot: 2026-05-10 · branch `feat/pmm_mister_supervisor`

## El problema que estamos resolviendo

Los controllers `pmm_mister` arrancan con configs que rinden bien las primeras horas, pero cuando el mercado cambia se quedan atascados (inventory atrapado, take_profit que no se ejecuta, fills asimétricos). Hoy lo detectamos a mano, frenamos bots, analizamos, relanzamos con configs nuevas.

**La idea**: un agente que monitorea esos controllers en vivo, detecta cuándo están en zona desfavorable, y propone cambios de config respaldados por backtests del período problemático. El humano aprueba o rechaza desde Telegram.

## Lo que construimos hasta ahora

### 1. Diseño completo (15 specs, ~270 KB)

Cubre toda la cadena: routines de observación → razonamiento del LLM → expansor determinístico de candidatos → backtest evidence loop → invariants duros → modo `propose` con botones inline → MCP tool de aplicación → memoria persistente → KPIs y dashboard.

Decisiones críticas tomadas (D1–D11), revisión crítica del corpus, y 7 fixes aplicados antes de implementar.

### 2. Primera capa funcional implementada

- **Routine `capital_state`**: monitoriza el capital de los controllers en 3 planos (controller-local, inter-controller por activo, cross-pair). Detecta sobre-suscripción del wallet. Corre desde Telegram, devuelve summary compacto + payload estructurado.
- **CLI dashboard** (`python -m condor.tools.dashboard`): tablero textual con el estado actual.
- **`condor_inspect`**: librería de helpers para que el agente (yo) consulte el servidor en vivo desde scripts ad-hoc, sin pasar por Telegram. Expuesta como skill `condor-express`.
- **Helpers de IO** (`state_io`): JSONL append-only y JSON map atómico, listos para cuando agreguemos persistencia histórica.

**93 tests pasando**, branch limpia, 26 commits atómicos.

### 3. Validación en producción

Probamos `capital_state` contra el servidor `brigado` con 14 controllers `pmm_mister` activos. Detectó:
- Wallet de $72.9k con $70.8k comprometidos → **$2k de headroom**.
- Múltiples controllers operando 7× por encima de su nominal (configs muy conservadoras vs realidad).
- Asset BRL con oversub_ratio crítico (>13×).

Esto es exactamente la información que motivó el framework: **antes no la veíamos**. Es la primera muestra concreta de que el monitor agrega valor incluso sin el agente todavía construido.

## Qué falta

| Pieza | Estado |
|---|---|
| Routine `market_regime` (multi-timeframe) | spec'd, no implementado |
| Routine `controller_performance` (PnL, volume, market share) | spec'd, no implementado |
| MCP tool `update_controller_config` (mutar configs en vivo) | spec'd, no implementado |
| Engine del agente adaptativo (lazo LLM → backtest → propose) | spec'd, no implementado |
| Handler de modo `propose` (Telegram inline buttons) | spec'd, no implementado |
| Persistencia histórica para percentiles (p50/p95) | spec'd, no implementado |
| Modo `shadow` y `auto` | spec'd, post-MVP |

## Dos fases futuras que tenemos identificadas pero fuera del MVP

- **F1 — Pantallas de UI** (web): panel del Supervisor + pantalla detallada por controller.
- **F2 — Análisis histórico empírico**: pipeline offline que cruza datos de bots ya corridos × régimen retroactivo × performance real → priors empíricos para el agente. *Es el corazón de la mejora continua con datos reales, no solo backtest sintético.*

## Cómo trabajamos

- **Branch dedicada**: `feat/pmm_mister_supervisor` (no toca main).
- **Commits atómicos por intención** (un commit = un cambio lógico).
- **Tests sobre cada pieza** antes de seguir.
- **Diseño primero**: cada feature tiene una spec en `.planning/strategy-framework/` antes de codear. Cuando aparece info nueva en código, actualizamos la spec.
- **Skill `/avance`**: comando que muestra dónde estamos en el plan en cualquier momento.

## Arquitectura — vista de pájaro

```
[mercado en vivo + bots PMM corriendo en Hummingbot]
                        ↓
     [3 routines de observación cada 10 min]
        capital_state · market_regime · controller_performance
                        ↓
  [agente LLM razona: ¿este controller está en zona desfavorable?]
                        ↓                  no → no_action + log
                       sí
                        ↓
       [LLM propone: dimensión + dirección + magnitud]
                        ↓
   [expansor determinístico: 3 candidatos numéricos concretos]
                        ↓
   [backtest evidence loop: simula 1 baseline + 3 candidatos
    sobre el período subóptimo, elige ganador por score]
                        ↓
       [invariants duros: cooldowns, no oversub, whitelist]
                        ↓ rechaza → log
                      pasa
                        ↓
        [Telegram: 1 mensaje con tabla de backtests
         + botones ✅ Apply (winner) [c2] [c3] ❌ ⏸]
                        ↓
                humano aprueba
                        ↓
   [MCP tool aplica el cambio al controller en vivo
    (hot-reload nativo de Hummingbot, ~10s)]
                        ↓
       [outcome loop a +30 min: midió real vs predicho]
                        ↓
   [feedback al agente: confiabilidad del backtest engine]
```

## Donde encontrar todo

- **Plan vivo**: `.planning/strategy-framework/TASKS.md`
- **Decisiones**: `.planning/strategy-framework/DECISIONS.md`
- **Diagrama de arquitectura**: `.planning/strategy-framework/ARCHITECTURE_DIAGRAM.md`
- **Specs por componente**: `.planning/strategy-framework/*_SPEC.md`
- **Código**: `condor/trading_agent/adaptive/`, `condor/tools/`, `routines/capital_state.py`
- **Tests**: `tests/`

## Riesgos / cosas a mirar

- **Operacional inmediato**: el wallet de `brigado` está al 97% de utilización. Si el mercado se mueve fuerte, los nuevos fills van a fallar con `insufficient_balance`. Capital_state lo está marcando — vale la pena revisar configs de `total_amount_quote` vs realidad antes de que el framework actúe sobre ello.
- **Calidad del backtest engine**: la fórmula de score y los pesos van a necesitar tuning con datos reales. La medición de outcomes (compute_match_score) es el feedback loop que cierra esto, pero requiere semanas de operación para calibrar.
- **Cold-start del agente**: los primeros 4h después de prender el agente no puede proponer (faltan datos para velocidades). Política documentada, sin sorpresas.
