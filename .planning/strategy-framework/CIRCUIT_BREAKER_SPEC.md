# Circuit Breaker — spec del control humano del agente

> Mecanismo de pausa/reanudación del agente. **Manual-only** por decisión del usuario (consistente con D7).
> Última actualización: 2026-05-09

## Filosofía

El circuit breaker es la **última línea de control** del humano sobre el agente. No es automático ni inteligente: es un interruptor binario que el humano flippea cuando ve que las cosas no andan.

**El agente nunca se autobloquea.** Las métricas de health (definidas más abajo) le dan al humano información para decidir cuándo activarlo, pero la decisión es humana.

Esto respeta literalmente D7: "siempre lo decide el humano".

## Estados del agente

```
┌────────┐  ⏸ pause   ┌────────┐  ▶ resume  ┌────────┐
│ active │  ───────►  │ paused │  ───────►  │ active │
└────────┘            └────────┘            └────────┘
```

- **`active`**: el agente tickea normalmente. Lee routines, razona, propone, aplica.
- **`paused`**: el agente sigue tickeando pero **no propone ni aplica** (Q1 confirmado):
  - Routines de observación **siguen corriendo** → series temporales (`capital_history/`, `regime_history/`, `controller_performance/`) se mantienen continuas.
  - Razonamiento del LLM **no corre** → ahorra tokens, no hay propuestas.
  - Outcomes pendientes **siguen midiéndose** → patches viejos cierran su loop aunque no se generen nuevos.
  - `audit_log.jsonl` **no** recibe entradas nuevas (no hay decisiones).
  - `anti_evidence_log.jsonl` **no** recibe entradas nuevas.

**Por qué seguir tickeando observación durante pausa**: cuando el humano reanuda, queremos:
- Histórico continuo (no agujeros en percentiles).
- Detectar si el régimen cambió mientras el agente estaba dormido.
- Cerrar los outcome loops que estaban en vuelo.

## Persistencia del estado

Archivo: `trading_agents/<agent>/state/agent_status.json`

```json
{
  "status": "active",
  "last_changed_at": "2026-05-09T14:32:18Z",
  "last_changed_by": "human",
  "reason": null,
  "history": [
    {
      "ts": "2026-05-09T10:15:00Z",
      "from": "active",
      "to": "paused",
      "by": "human",
      "reason": "manual review of recent rejections"
    },
    {
      "ts": "2026-05-09T11:45:00Z",
      "from": "paused",
      "to": "active",
      "by": "human",
      "reason": "approved to resume after rule update"
    }
  ]
}
```

### Reglas

- **Lectura**: cada tick del agente lee este archivo. Si `status=paused`, skipea la fase de razonamiento/propuesta.
- **Escritura**: solo via UI de Condor o comandos de Telegram. El agente nunca escribe.
- **`history`**: append-only en cada cambio. Útil para auditoría de cuándo se pausó/reanudó y por qué.
- **Default al crear el agente**: `status=active`.
- **Si el archivo no existe**: tratamos como `status=active` (compatibilidad backward).

### Atomicidad

La escritura del archivo va via `tempfile + rename` (mismo patrón que `last_changes.json`). Evita estado corrupto si la UI crashea durante el write.

## Métricas de health (Q2: helpers, no routine dedicada)

Calculadas **on-demand** cuando la UI las pide (read-only sobre stores existentes). Sin cadencia propia, sin escrituras, sin overhead.

### Inventario

| Métrica | Cómputo | Origen | Para qué |
|---|---|---|---|
| `changes_applied_last_hour` | count en `audit_log` con `applied=True` y `applied_at >= now-1h` | audit_log | Detectar thrashing |
| `changes_applied_last_24h` | idem 24h | audit_log | Tendencia de actividad |
| `verdict_rejection_streak` | rechazos humanos consecutivos por controller | audit_log | Desalineación humano-agente |
| `anti_evidence_count_24h` | entries en anti_evidence_log últimas 24h | anti_evidence_log | Frecuencia de errores del LLM |
| `match_quality_avg_24h` | promedio de `outcome_30min.match_score` últimas 24h | audit_log con outcomes | Confiabilidad del backtest |
| `match_quality_distribution_24h` | conteo `good/mediocre/bad` últimas 24h | audit_log con outcomes | Calidad por bucket |
| `max_oversub_ratio_now` | max ratio actual de `capital_state.global.oversub_alerts` | capital_state (live) | Capital crítico |
| `worst_outcome_24h` | min de `outcome.actual_pnl_delta` últimas 24h | audit_log con outcomes | Pérdidas grandes recientes |
| `pending_outcomes_count` | entries con `outcome_30min: null` y `applied_at + 30min ≤ now` | audit_log | Mide si el outcome loop está al día |

### Helpers

```python
# state/health.py

def compute_health_snapshot(agent_dir: Path, now: datetime) -> HealthSnapshot:
    """One-shot computation of all health metrics. Read-only."""
    return HealthSnapshot(
        changes_applied_last_hour=count_audit_filter(
            agent_dir, lambda e: e.get("applied") and parse(e["applied_at"]) >= now - timedelta(hours=1)
        ),
        # ... resto de métricas
    )
```

### Eficiencia

Lectura típica: `audit_log.jsonl` actual + tail de archivos archivados si la ventana cruza el cambio de mes.
- audit_log típico: <100 entries/mes → tail completo es trivial.
- anti_evidence_log: <50 entries/mes.
- Total: <100ms por cómputo de snapshot completo.

No hace falta caché.

## Integración con UI de Condor (Fase posterior)

La UI necesita:
1. **Lectura del estado** — `GET /agents/<name>/status` → `agent_status.json` actual.
2. **Lectura de health** — `GET /agents/<name>/health` → snapshot de métricas.
3. **Pause** — `POST /agents/<name>/pause` con body `{reason: "..."}`.
4. **Resume** — `POST /agents/<name>/resume` con body `{reason: "..."}`.

### Backend de Condor

```python
# condor/web/routes/agents.py

@router.get("/{name}/status")
async def get_agent_status(name: str) -> dict:
    return read_json(agent_dir(name) / "state" / "agent_status.json")

@router.get("/{name}/health")
async def get_agent_health(name: str) -> HealthSnapshot:
    return compute_health_snapshot(agent_dir(name), now=datetime.utcnow())

@router.post("/{name}/pause")
async def pause_agent(name: str, body: PauseRequest) -> dict:
    return change_status(agent_dir(name), "paused", body.reason, by="human")

@router.post("/{name}/resume")
async def resume_agent(name: str, body: ResumeRequest) -> dict:
    return change_status(agent_dir(name), "active", body.reason, by="human")
```

### Vista propuesta (mock)

```
┌─────────────────────────────────────────────────────────┐
│ ADAPTIVE PMM SUPERVISOR                       [Activo]  │
├─────────────────────────────────────────────────────────┤
│ Health (1h):                                            │
│   Cambios aplicados:        2                           │
│   Rejection streak:         0                           │
│   Match quality avg:        0.74  ✓                     │
│   Pending outcomes:         3                           │
│                                                         │
│ Health (24h):                                           │
│   Cambios aplicados:        14                          │
│   Anti-evidence:            2                           │
│   Match good/mediocre/bad:  18 / 5 / 1                  │
│   Worst outcome:            -0.42 USD (controller C2)   │
│                                                         │
│ Capital:                                                │
│   Max oversub_ratio:        0.78  ✓                     │
│                                                         │
│ Last action: 14:33  take_profit 0.0003 → 0.00045        │
│              (approved by human, outcome pending)       │
│                                                         │
│ [⏸ PAUSE AGENT]  [📊 View audit log]  [📜 History]     │
└─────────────────────────────────────────────────────────┘
```

### Modo MVP cero (sin UI)

Si el MVP no incluye UI todavía, alternativas:
- Editar `agent_status.json` a mano (`vim`).
- Comando de Telegram: `/agent_pause <name>` / `/agent_resume <name>`.

Ambos funcionan con el mismo backend. La UI es **valor agregado**, no requisito MVP del framework adaptativo.

## Read context del LLM (Q3: opción a)

**El LLM no recibe información del circuit breaker en su contexto.**

Razón: si el LLM no razona durante la pausa, no hace falta que sepa nada sobre ella. Las routines reportan el estado actual del mercado/capital, no necesita saber el pasado del breaker.

Si en algún momento futuro queremos que el LLM "vea" cuándo estuvo pausado (para informar decisiones tipo "estuviste fuera 3h, hubo cambio de régimen mientras tanto"), se agrega después.

## Comportamiento durante pausa — detalle

Pseudocódigo del tick del agente:

```python
def tick(agent_dir):
    status = read_agent_status(agent_dir)
    
    # 1. Routines de observación SIEMPRE corren
    capital = run_capital_state_routine(agent_dir)
    regime = run_market_regime_routine(agent_dir)
    perf = run_controller_performance_routine(agent_dir)
    
    # 2. Outcome loop SIEMPRE corre
    process_pending_outcomes(agent_dir, now=datetime.utcnow())
    
    # 3. Si paused, terminamos acá
    if status.status == "paused":
        log(f"Agent paused since {status.last_changed_at}, skipping reasoning")
        return
    
    # 4. Razonamiento (solo si active)
    context = build_llm_context(agent_dir, scope_controllers, {capital, regime, perf})
    proposal = llm_reason(context)
    
    if proposal["action"] == "no_action":
        return
    
    # 5. Expansor + backtest + scoring + invariants
    candidates = expand(proposal)
    selected = backtest_and_score(candidates)
    if not validate_invariants(selected):
        return
    
    # 6. Dispatch según mode
    dispatch(selected, mode=config.mode)
```

**Implicancias**:
- En pausa: cero tokens de LLM gastados.
- En pausa: las series temporales se mantienen.
- Al reanudar: el próximo tick va con razonamiento normal.

## Cuándo el humano debería pausar (sugerencias)

Esta sección es **guía** para el humano (no acciones del agente). En el panel de UI puede aparecer como tooltips:

| Señal | Sugerencia |
|---|---|
| `verdict_rejection_streak ≥ 3` | "El humano rechazó 3 propuestas seguidas. Considerar pausar y revisar policy." |
| `anti_evidence_count_24h ≥ 5` | "El LLM se equivocó 5+ veces en 24h. Considerar pausar y revisar matriz de favorabilidad." |
| `match_quality_avg_24h < 0.4` | "El backtest está desviando del outcome real. Considerar pausar y revisar score formula." |
| `worst_outcome_24h < -X% del nominal` | "Pérdida grande reciente. Considerar pausar y diagnosticar." |
| `changes_applied_last_hour ≥ 5` | "Frecuencia alta de cambios. Verificar que no haya thrashing." |

Estas son **alertas suaves** — la UI puede mostrarlas con colores pero no bloquean nada. La decisión es del humano.

## Pendientes (revisar más adelante)

- **UI de Condor**: pantalla del supervisor con el panel de health + botones pause/resume. Documentado en backlog (ver TASKS.md).
- **Comandos de Telegram**: `/agent_pause`, `/agent_resume` como alternativa a UI web.
- **Per-controller pause**: actualmente la pausa es global. Si en el futuro se necesita pausar 1 controller específico, agregar a config.yml#adaptive.scope.
- **Cooldown post-resume**: hoy reanuda inmediato. Si en práctica resulta agresivo, agregar buffer configurable.
- **Auto-triggers** (descartados explícitamente, ver más arriba): si en algún momento se decide volver a tener triggers automáticos, requiere D-decision nueva del usuario.

## Próximos pasos

1. (Implementación, Fase 5) — `state/io.py` con `read_agent_status` / `change_status`.
2. (Fase posterior) — Endpoints en `condor/web/routes/agents.py`.
3. (Fase posterior) — Vista UI con panel de health.
