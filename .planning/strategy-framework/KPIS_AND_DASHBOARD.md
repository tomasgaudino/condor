# KPIs & Dashboard — métricas para tracking del framework

> Cómo medimos si cada módulo hace su trabajo y cómo gestionamos el sistema en conjunto.
> Última actualización: 2026-05-09

## Filosofía

Hasta acá especificamos **qué hace cada módulo**. Este doc agrega **cómo medimos si está funcionando como prometió** y cómo el humano puede gestionar el framework sin tener que leer 15 specs cada vez que vuelve.

3 niveles de medición:
1. **Salud técnica** — ¿el módulo funciona? (latencias, errores, throughput)
2. **Calidad** — ¿el módulo hace bien su trabajo? (precisión, recall)
3. **Negocio** — ¿el framework agrega valor? (mejor PnL, más volumen)

Sin (1) nada anda. Sin (2) anda mal. Sin (3) anda bien pero no sirve.

## Principios de diseño

- **Reusar stores existentes**: ningún KPI requiere persistencia nueva. Todo se deriva de stores ya definidos en specs anteriores.
- **Cómputo determinístico**: misma data → mismo KPI.
- **On-demand** (Q3=a): los KPIs se calculan cuando se piden. Sin caché en MVP.
- **MVP gradual** (Q1=b): implementar primero los críticos (15), agregar el resto cuando hagan falta.
- **Helpers puros** (Q2=ok): `condor/agent_framework/kpis.py` — funciones que toman `agent_dir` + ventana y devuelven valores.

---

## KPIs por módulo

### Routine: `capital_state`

| KPI | Tipo | Cómputo | MVP |
|---|---|---|---|
| `oversub_alerts_count_24h` | técnica | count entries con severity ∈ {warn, crit} en últimas 24h | ✅ |
| `oversub_max_ratio_24h` | calidad | `max(ratio)` de oversub_alerts en 24h | ✅ |
| `controllers_in_suboptimal_pct` | calidad | `% controllers con util > worst_case` por tick | |
| `wallet_headroom_pct_min_24h` | negocio | `min(headroom_pct)` en 24h | ✅ |
| `latency_compute_ms` | técnica | tiempo del call | |

**Fuente de datos**: `state/capital_history/<ctrl>.jsonl`, `audit_log` para alerts.

### Routine: `market_regime`

| KPI | Tipo | Cómputo | MVP |
|---|---|---|---|
| `regime_transitions_count_24h` | calidad | cambios de `canonical_regime` por par en 24h | ✅ |
| `regime_persistence_avg_minutes` | calidad | promedio de duración antes de cambio | |
| `regime_distribution_pct` | calidad | `%` tiempo en cada régimen (último 7d) | |
| `regime_confidence_distribution` | calidad | `%` ticks con confidence high/medium/low | |
| `pullback_detections_count_7d` | calidad | veces que se detectó `trending_with_pullback` | |
| `latency_compute_ms` | técnica | tiempo del call | |

**Fuente**: `state/regime_history/<connector>_<pair>.jsonl`.

### Routine: `controller_performance`

| KPI | Tipo | Cómputo | MVP |
|---|---|---|---|
| `suboptimal_now_count_24h` | calidad | ticks con `suboptimal_now=true` por controller | ✅ |
| `pnl_velocity_avg_24h` | negocio | promedio de PnL/h por controller | ✅ |
| `volume_velocity_avg_24h` | negocio | promedio de volumen/h por controller | |
| `market_share_24h` | negocio | volumen propio / volumen del par | ✅ |
| `time_since_last_fill_p95` | técnica | p95 de tiempo entre fills | |
| `fill_rate_per_hour` | negocio | fills/hora promedio por controller | |

**Fuente**: `state/controller_performance/<ctrl>.jsonl`.

### Agente

| KPI | Tipo | Cómputo | MVP |
|---|---|---|---|
| `proposals_emitted_24h` | técnica | count audit_log entries en 24h | ✅ |
| `proposals_per_controller_24h` | técnica | breakdown por controller_id | |
| `tick_latency_p95_seconds` | técnica | p95 del tiempo total por tick (incluye backtest) | ✅ |
| `llm_token_usage_24h` | técnica | tokens consumidos por LLM | |
| `no_action_ratio_24h` | calidad | `% ticks con action=no_action` | |

**Fuente**: `state/audit_log.jsonl` + log estructurado del agente.

### Backtest Evidence Loop

| KPI | Tipo | Cómputo | MVP |
|---|---|---|---|
| `backtest_cache_hit_rate` | técnica | `hits / (hits + misses)` últimas 24h | ✅ |
| `backtest_avg_duration_seconds` | técnica | promedio de duración por backtest | |
| `backtest_failures_24h` | técnica | count por tipo de error | ✅ |
| `cycles_with_winner_pct` | calidad | `% ciclos con winner` (vs anti-evidence) | |
| `score_distribution` | calidad | histograma de scores ganadores | |
| `anti_evidence_count_7d` | calidad | entries en anti_evidence_log | ✅ |

**Fuente**: log estructurado del backtest loop + `BacktestStore` + `state/anti_evidence_log.jsonl`.

### Modo `propose`

| KPI | Tipo | Cómputo | MVP |
|---|---|---|---|
| `verdict_distribution_24h` | calidad | `%` approved/rejected/snoozed/expired | ✅ |
| `time_to_verdict_p50_seconds` | técnica | p50 desde send hasta clic | |
| `expired_count_24h` | calidad | count de propuestas expiradas | ✅ |
| `apply_failure_rate_24h` | técnica | `apply_failed / approved` | ✅ |
| `verdict_choice_distribution` | calidad | `%` c1 / c2 / c3 elegidos | |

**Fuente**: `state/audit_log.jsonl` (campos `human_verdict`, `verdict_at`, `verdict_chose_candidate`, `applied`).

### MCP Tool `update_controller_config`

| KPI | Tipo | Cómputo | MVP |
|---|---|---|---|
| `calls_24h` | técnica | count total | |
| `success_rate_24h` | técnica | `% success: true` | ✅ |
| `errors_by_code` | técnica | breakdown por error_code | ✅ |
| `latency_p95_ms` | técnica | tiempo de la tool call | |

**Fuente**: log estructurado de la tool (sumarse a audit_log si conviene).

### Memoria persistente

| KPI | Tipo | Cómputo | MVP |
|---|---|---|---|
| `state_dir_size_mb` | técnica | tamaño total de `state/` | |
| `audit_log_entries_count` | técnica | total + breakdown por mes | |
| `outcome_pending_count` | calidad | entries con `outcome_30min: null` aplicables | ✅ |
| `outcome_match_quality_distribution` | calidad | `%` good/mediocre/bad | ✅ |

**Fuente**: filesystem + `state/audit_log.jsonl`.

### Circuit breaker

| KPI | Tipo | Cómputo | MVP |
|---|---|---|---|
| `agent_status_now` | técnica | `active` o `paused` | ✅ |
| `time_paused_pct_7d` | técnica | `%` del tiempo pausado | |
| `pause_count_7d` | técnica | activaciones manuales en 7d | |
| `pause_duration_p50_minutes` | técnica | p50 de duración por pausa | |

**Fuente**: `state/agent_status.json` (incluyendo `history`).

---

## KPIs de NEGOCIO (los más importantes)

Miden si el framework agrega valor real. Comparan **período supervisado vs no supervisado**.

| KPI | Cómputo | Pregunta | MVP |
|---|---|---|---|
| `pnl_attributable_to_agent_24h` | suma de `outcome.actual_pnl_delta` por patches aplicados | ¿Cuánta plata agregó el agente? | ✅ |
| `volume_uplift_pct` | volume después / volume antes (del cambio) | ¿Estamos tradeando más? | ✅ |
| `controllers_recovered_from_suboptimal_count` | controllers que pasaron subóptimo → óptimo después de patch | ¿El agente saca controllers del pozo? | ✅ |
| `bot_uptime_pct` | `%` tiempo controllers operando sin clavarse | Estabilidad operativa | |
| `match_quality_avg_30d` | promedio de match_score en 30 días | Confianza en backtest | ✅ |
| `human_intervention_avoided_count` | rough count: cambios aplicados que el humano no tuvo que pensar | Time saved del humano | |

---

## Tablero general — diseño

### Layout propuesto

Una sola pantalla, 5 secciones, alerts al final:

```
═══════════════════════════════════════════════════════════════
 ADAPTIVE STRATEGY FRAMEWORK · DASHBOARD
═══════════════════════════════════════════════════════════════

🟢 Status: ACTIVE       ⏰ Last tick: 14:32 UTC (2 min ago)

┌─ PERFORMANCE (24h) ─────────────────────────────────────────┐
│ PnL agente:           +$2.34   (vs $-0.18 sin agente)       │
│ Volume uplift:        +12.4%                                │
│ Controllers OK:       4 / 5    (1 en subóptimo)             │
│ Match quality avg:    0.78  ✓                               │
└─────────────────────────────────────────────────────────────┘

┌─ AGENT ACTIVITY (24h) ──────────────────────────────────────┐
│ Proposals:           18  →  ✅ 14  ❌ 3  ⏸ 1  ⏰ 0           │
│ Apply failures:       0%                                    │
│ No-action ratio:     43%                                    │
│ Anti-evidence:        2 (mean_rev_high_vol × take_profit)   │
└─────────────────────────────────────────────────────────────┘

┌─ MARKET REGIME (now) ───────────────────────────────────────┐
│ BTC-USDT:  mean_reverting_high_vol   🟡 47 min              │
│ BTC-BRL:   trending_up               🔴 2h  bias: up         │
│ ETH-USDT:  optimal                   🟢 1h 20min            │
└─────────────────────────────────────────────────────────────┘

┌─ CAPITAL ───────────────────────────────────────────────────┐
│ Wallet headroom:      78% (BTC), 22% (USDT) ⚠️              │
│ Oversub alerts:       0                                     │
│ Total committed:      $156 / $200 nominal                   │
└─────────────────────────────────────────────────────────────┘

┌─ TECH HEALTH ───────────────────────────────────────────────┐
│ Tick latency p95:     34s                                   │
│ Backtest cache hit:   42%                                   │
│ State dir size:       4.2 MB                                │
│ LLM tokens (24h):     ~28k                                  │
└─────────────────────────────────────────────────────────────┘

┌─ ALERTS ────────────────────────────────────────────────────┐
│ ⚠️ USDT headroom < 30% — considerar redistribución          │
│ ℹ️ 1 outcome pendiente de medición (controller C2)          │
└─────────────────────────────────────────────────────────────┘
```

### Decisiones del tablero

- **Una sola pantalla** — todo lo crítico visible.
- **5 secciones**: performance, agent activity, market, capital, tech health.
- **Comparativos** donde aplica ("vs sin agente", "vs ayer").
- **Alerts al final** — lo que requiere atención humana.
- **Status grande** arriba — saber si está corriendo de un vistazo.
- **Emojis con sentido**: 🟢 ok, 🟡 atención, 🔴 alerta. ✓ válido. ⚠️ warning. ⏰ pendiente.

### Reglas de coloreo de alerts

| Condición | Severidad |
|---|---|
| `oversub_max_ratio_24h ≥ 1.0` | ⚠️ warning |
| `oversub_max_ratio_24h ≥ 1.5` | 🔴 critical |
| `wallet_headroom_pct_min_24h < 0.30` | ⚠️ warning |
| `verdict_rejection_streak ≥ 3` | ⚠️ "considerar pausar" |
| `match_quality_avg_24h < 0.4` | ⚠️ "backtest engañoso" |
| `apply_failure_rate_24h > 0.1` | 🔴 "MCP tool inestable" |
| `expired_count_24h ≥ 5` | ℹ️ "atender propuestas más rápido" |

Estas alerts se computan al renderizar el dashboard, son visuales — **no son actions**.

---

## Implementación — niveles graduales

### Nivel 1 — CLI (MVP del MVP)

```bash
python -m condor.tools.dashboard <agent_name>
```

Imprime el tablero ASCII en stdout. Cero infra extra.

Implementación: `condor/tools/dashboard.py` que importa `condor/agent_framework/kpis.py` y renderiza con un template string.

### Nivel 2 — Web (parte de Fase F1)

Vista en `condor/web/routes/agents/dashboard.py`. Reutiliza los mismos KPIs computados en `kpis.py`.

Vista HTML con estilo similar a otras pantallas de Condor.

### Nivel 3 — Telegram (post-MVP)

Comando `/dashboard <agent_name>` que devuelve el tablero como mensaje markdown. Reutiliza el render del CLI.

---

## Helpers — `condor/agent_framework/kpis.py`

Estructura del archivo:

```python
"""KPI computations for the adaptive strategy framework.

All functions are pure: take an agent_dir and a window, return a value.
No side effects, no caching.
"""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# ─── KPIs de salud técnica ───────────────────────────────────────────

def proposals_emitted_count(agent_dir: Path, hours: int = 24) -> int: ...

def tick_latency_p95(agent_dir: Path, hours: int = 24) -> float | None: ...

def backtest_cache_hit_rate(agent_dir: Path, hours: int = 24) -> float | None: ...

def apply_success_rate(agent_dir: Path, hours: int = 24) -> float | None: ...

# ─── KPIs de calidad ─────────────────────────────────────────────────

def verdict_distribution(agent_dir: Path, hours: int = 24) -> dict[str, int]: ...

def regime_transitions(
    agent_dir: Path, connector: str, pair: str, hours: int = 24
) -> int: ...

def anti_evidence_count(agent_dir: Path, days: int = 7) -> int: ...

def match_quality_distribution(agent_dir: Path, hours: int = 24) -> dict[str, int]: ...

# ─── KPIs de negocio ─────────────────────────────────────────────────

def pnl_attributable_to_agent(agent_dir: Path, hours: int = 24) -> float: ...

def volume_uplift_pct(agent_dir: Path, hours: int = 24) -> float | None: ...

def controllers_recovered_count(agent_dir: Path, hours: int = 24) -> int: ...

# ─── Compositor del dashboard ────────────────────────────────────────

def compute_dashboard_snapshot(agent_dir: Path) -> DashboardSnapshot:
    """One-shot computation of all dashboard data."""
    return DashboardSnapshot(
        status=read_agent_status(agent_dir),
        performance=...,
        activity=...,
        regime=...,
        capital=...,
        tech=...,
        alerts=compute_alerts(...),
    )
```

### Convenciones

- **Window default**: 24h donde tenga sentido, 7d para KPIs raros (anti_evidence), 30d para confianza estadística (match_quality).
- **Return `None`** cuando no hay datos suficientes (no inventar 0).
- **Helpers de fechas**: usar `datetime.utcnow()` siempre, todos los timestamps en UTC isoformat.

### Eficiencia

- Lectura de JSONL: tail seek cuando se busca ventana corta.
- Cache implícito de Python (lectura de archivos chicos es <10ms).
- Total cómputo del dashboard completo estimado: <500ms.

Si en práctica resulta lento, agregar caché en memoria con TTL corto.

---

## MVP de KPIs — los 15 críticos

Lista exacta para arrancar:

**Salud técnica (5)**:
1. `agent_status_now`
2. `proposals_emitted_24h`
3. `tick_latency_p95_seconds`
4. `apply_success_rate_24h`
5. `errors_by_code_24h` (de la MCP tool)

**Calidad (5)**:
6. `verdict_distribution_24h`
7. `oversub_alerts_count_24h`
8. `suboptimal_now_count_24h`
9. `anti_evidence_count_7d`
10. `outcome_match_quality_distribution`

**Negocio (5)**:
11. `pnl_attributable_to_agent_24h`
12. `volume_uplift_pct_24h`
13. `controllers_recovered_from_suboptimal_count_24h`
14. `wallet_headroom_pct_min_24h`
15. `match_quality_avg_30d`

Los otros ~30 KPIs quedan para agregar cuando hagan falta.

---

## Pendientes (revisar más adelante)

- **Tendencias**: hoy son métricas snapshot. Agregar comparativos vs ayer/semana pasada.
- **Drilldowns**: cliquear sobre un KPI debería abrir detalle (ej: clic en `anti_evidence_count` → últimas N entradas).
- **Alerting proactivo**: Telegram pings cuando ciertas alerts se prenden (no solo visualización).
- **Export a CSV/JSON**: para análisis ad-hoc en herramientas externas.
- **Histórico de dashboard**: snapshots periódicos para ver evolución del propio framework en el tiempo (meta-monitoring).

## Próximos pasos

1. (Implementación, Fase 5) — escribir `condor/agent_framework/kpis.py` con los 15 KPIs MVP.
2. (Implementación, Fase 5) — `condor/tools/dashboard.py` CLI renderer.
3. (Fase F1) — vista web del dashboard.
