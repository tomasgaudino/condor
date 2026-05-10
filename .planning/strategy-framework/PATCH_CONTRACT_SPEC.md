# Patch Contract — spec del flujo de patch del agente

> Cómo viaja una propuesta desde la decisión del LLM hasta el cambio aplicado al controller.
> Última actualización: 2026-05-09

## Filosofía

El patch atraviesa **4 estados** distintos, cada uno con shape específica. Cada estado tiene un validador que puede frenar el flujo. El LLM solo controla el primer estado; el resto es determinístico.

```
PROPOSAL    →  CANDIDATES  →  SELECTED PATCH  →  APPLIED PATCH
(LLM)         (expander)     (scoring)         (MCP tool)
```

## Estado 1 — PROPOSAL (output del LLM)

### Shape

```json
{
  "tick_id": 145,
  "ts": "2026-05-09T14:32:18Z",
  "action": "propose",
  "proposals": [
    {
      "controller_id": "001_pmm_binance_BTC-USDT",
      "dimension": "take_profit",
      "direction": "increase",
      "magnitude_qualitative": "medium",
      "reasoning": "Régimen mean-reverting con NATR alto (0.014 vs p67=0.010). Las oscilaciones grandes hacen que el TP de 0.0003 capture solo parte del rebote.",
      "caveats": [
        "Solo afecta executors nuevos. Effecto gradual a medida que rotan."
      ]
    }
  ]
}
```

O alternativamente:

```json
{
  "tick_id": 145,
  "ts": "2026-05-09T14:32:18Z",
  "action": "no_action",
  "reason": "all controllers in optimal regime"
}
```

### Reglas (D-Q1, D-Q2)

- **Single-field, single-controller**: cada proposal toca exactamente 1 campo de 1 controller. Si el LLM quiere tocar 2 campos del mismo controller, son 2 proposals separados.
  > **Pendiente futuro**: multi-field patches (cambios coordinados, ej. "ampliar spreads Y subir take_profit"). Documentado como mejora post-MVP.
- **3 niveles de magnitud**: `small | medium | large`.
  > **Pendiente futuro**: granularidad fina (5 niveles, percentile numérico). Hoy 3 alcanza.
- **Múltiples proposals por tick** son posibles (uno por controller).
- **`reasoning`** es texto libre — para auditoría y para mostrar al humano. No se parsea.
- **`caveats`** opcional — el LLM puede agregar advertencias técnicas.

### Validación de formato

```python
def validate_proposal_format(proposal):
    assert proposal["controller_id"] in active_controllers
    assert proposal["dimension"] in invariants.allowed_fields
    assert proposal["dimension"] not in invariants.forbidden_fields
    assert proposal["direction"] in ("increase", "decrease")
    assert proposal["magnitude_qualitative"] in ("small", "medium", "large")
    assert proposal.get("reasoning")  # required, must be non-empty
    return True
```

Si falla → log y skip ese proposal. Otros proposals del tick se siguen procesando.

## Estado 2 — CANDIDATES (output del expansor determinístico)

### El expansor

Para cada `proposal`, genera **N candidatos numéricos** según `dimension + direction + magnitude`. Determinístico: misma input → mismos outputs. Sin LLM.

#### Tablas de expansión

**Para campos `percentage_type`** (multiplicar/dividir el valor actual):

| Magnitud | Increase | Decrease |
|---|---|---|
| `small` | × 1.25 | × 0.80 |
| `medium` | × 1.5, × 2.0 | × 0.67, × 0.50 |
| `large` | × 2.0, × 3.0 | × 0.50, × 0.33 |

**Para campos `absolute_type`** (sumar/restar deltas):

| Magnitud | Increase | Decrease |
|---|---|---|
| `small` | + 0.02 | − 0.02 |
| `medium` | + 0.05, + 0.08 | − 0.05, − 0.08 |
| `large` | + 0.10 (clamp) | − 0.10 (clamp) |

(Asignación de tipo por campo viene de `invariants.yaml#field_type_map`.)

#### Comportamiento de `small`

`small` genera **1 solo candidato** (no tiene sentido tener 3 valores casi idénticos). El flujo backtest usa baseline + 1 alternativa.

#### Cómputo de los candidatos

```python
def expand(proposal, current_config, invariants):
    field = proposal["dimension"]
    current = current_config[field]
    field_type = invariants.field_type_map[field]
    table = invariants.expansion_table[field_type]
    multipliers_or_deltas = table[proposal["magnitude_qualitative"]][proposal["direction"]]
    
    raw_candidates = []
    for m in multipliers_or_deltas:
        if field_type == "percentage_type":
            raw_candidates.append(current * m)
        else:  # absolute_type
            raw_candidates.append(current + m)
    
    # Clamp por invariants
    clamped = []
    for cand in raw_candidates:
        c, was_clamped = clamp_to_invariants(field, current, cand, invariants)
        clamped.append({"value": c, "clamped": was_clamped})
    
    # Clamp por validez del dominio
    valid = []
    for c in clamped:
        c["value"] = clamp_to_domain(field, c["value"])
        valid.append(c)
    
    # Dedup
    deduped = dedup_by_value(valid)
    
    return deduped
```

### Shape

```json
{
  "proposal_ref": {
    "controller_id": "001_pmm_binance_BTC-USDT",
    "dimension": "take_profit",
    "direction": "increase",
    "magnitude_qualitative": "medium"
  },
  "current_value": 0.0003,
  "field_type": "percentage_type",
  "expansion_strategy": "percentage_type_medium_increase",
  "candidates": [
    {"id": "c1", "value": 0.00045, "delta_relative": "× 1.5", "clamped": false},
    {"id": "c2", "value": 0.00060, "delta_relative": "× 2.0", "clamped": false},
    {"id": "c3", "value": 0.00090, "delta_relative": "× 3.0", "clamped": false}
  ],
  "post_invariant_check": "passed"
}
```

### Validación contra invariants

Cada candidato se chequea contra `invariants.yaml`:
- `max_increase` / `max_decrease` (con multiplicador de auto_mode si aplica).
- Dominio del campo (ej: `target_base_pct ∈ [0,1]`, `max_active_executors_by_level` entero ≥ 1).

Si todos los candidatos quedan fuera de límites tras clamp → skip el proposal completo (log especial: "expansion_clamped_to_zero").

Si algunos quedan fuera → marca `clamped: true` y deduplica por valor.

## Estado 3 — SELECTED PATCH (output del scoring)

### Backtest loop (D9)

Para los `candidates` válidos, correr en **serie** (no paralelo, riesgo de pisarse en el engine de Hummingbot):

1. Backtest del **baseline** (config actual del controller, durante el período subóptimo).
2. Backtest de cada **candidato** (mismo período, único cambio: el field).
3. Aplicar la **función de score**.

```python
def score(result, baseline):
    pnl = result.net_pnl_quote
    vol = result.total_volume
    if pnl < 0:
        return float("-inf")
    pnl_norm = min(pnl, baseline.net_pnl_quote * 2) / max(baseline.net_pnl_quote, 1)
    vol_norm = vol / max(baseline.total_volume, 1)
    return vol_norm + 0.3 * pnl_norm
```

(Fórmula tentativa, afinable. Ver D9.)

### Comportamiento por escenario (D-Q3)

| Escenario | Acción |
|---|---|
| Al menos 1 candidato mejora baseline (score > score_baseline) | Elegir el de mejor score → SELECTED PATCH |
| Mezcla (algunos mejoran, otros no) | Elegir el de mejor score (mismo criterio) |
| **Todos los candidatos empeoran al baseline** | **`no_action`** + entrada especial al **anti-evidence log** |
| Todos los candidatos PnL < 0 | `no_action` (filtrado por score = -inf) |
| Backtest falla y `on_backtest_failure: block` | `no_action` |
| Backtest falla y `on_backtest_failure: allow_with_flag` | Elegir el primer candidato, marcar `based_on_backtest: false` |

### Anti-evidence log (NEW por D-Q3)

Cuando todos los candidatos empeoran al baseline, se registra una entrada en `state/anti_evidence_log.jsonl`:

```json
{
  "ts": "2026-05-09T14:32:18Z",
  "controller_id": "001_pmm_binance_BTC-USDT",
  "tick_id": 145,
  "regime_observed": "mean_reverting_high_vol",
  "suboptimal_minutes": 47,
  "llm_proposal": {
    "dimension": "take_profit",
    "direction": "increase",
    "magnitude_qualitative": "medium"
  },
  "llm_reasoning": "...",
  "candidates_tested": [
    {"id": "c1", "value": 0.00045, "score": 0.42, "vs_baseline": -0.18},
    {"id": "c2", "value": 0.00060, "score": 0.31, "vs_baseline": -0.29},
    {"id": "c3", "value": 0.00090, "score": 0.18, "vs_baseline": -0.42}
  ],
  "verdict": "all_candidates_worse_than_baseline",
  "interpretation_hint": "LLM may have wrong direction in this regime"
}
```

**Propósito**: que el agente (o el humano) pueda revisar **periódicamente** este log y detectar patrones del tipo "el LLM siempre se equivoca cuando está en X régimen". Ese feedback puede:
- Refinar las reglas en `agent.md` (policy).
- Ajustar la matriz de favorabilidad.
- Detectar bugs en `market_regime` (clasificación incorrecta del régimen).

**Lectura del log**: el agente puede leer las últimas N entradas en cada tick antes de razonar — para evitar repetir errores recientes. Estrategia conservadora: si en las últimas 3 propuestas para el mismo controller en el mismo régimen todos los candidatos fueron peor → no proponer nada en ese régimen hasta que el régimen cambie.

> **No es retry automático** (descartado en D-Q3): no flippeamos la dirección automáticamente. Solo registramos y dejamos que el ciclo natural lo absorba.

### Shape de SELECTED PATCH

```json
{
  "patch_id": "p_2026-05-09T14:32:18Z_001",
  "ts": "2026-05-09T14:32:18Z",
  "controller_id": "001_pmm_binance_BTC-USDT",
  "field": "take_profit",
  "old_value": 0.0003,
  "new_value": 0.00045,
  "expected_effect": {
    "based_on_backtest": true,
    "window_backtested": {
      "start": "2026-05-09T13:32:18Z",
      "end": "2026-05-09T14:32:18Z",
      "duration_minutes": 60
    },
    "baseline_results": {
      "net_pnl_quote": 0.12,
      "volume": 12400,
      "total_positions": 87,
      "score": 1.0
    },
    "candidate_results": {
      "net_pnl_quote": 0.45,
      "volume": 9800,
      "total_positions": 64,
      "score": 0.92
    },
    "score_decomposition": {
      "vol_norm": 0.79,
      "pnl_norm": 3.75,
      "formula": "vol_norm + 0.3 * pnl_norm"
    }
  },
  "reasoning_chain": {
    "regime_observed": "mean_reverting_high_vol",
    "suboptimal_minutes": 47,
    "llm_reasoning": "Régimen mean-reverting con NATR alto...",
    "caveats": ["take_profit only affects new executors"]
  },
  "alternative_candidates": [
    {"id": "c2", "value": 0.00060, "score": 0.85},
    {"id": "c3", "value": 0.00090, "score": 0.62}
  ],
  "invariants_check": {
    "ok": true,
    "checks_passed": ["whitelist", "max_delta", "cooldown", "no_oversub"]
  }
}
```

### Notas

- `alternative_candidates` se muestra al humano en modo `propose` (D-Q4: dejar elegir entre candidatos).
- `based_on_backtest: false` cuando connector no soportado o backtest falla con flag.
- En modo `propose`, el patch se persiste en `state/audit_log.jsonl` con `human_verdict: pending` hasta que el usuario responda.

## Estado 4 — APPLIED PATCH

### MCP tool call

```python
await client.update_controller_config(
    bot_name=patch.bot_name,
    controller_id=patch.controller_id,
    field=patch.field,
    value=patch.new_value
)
```

(La spec exacta de `update_controller_config` se trabaja en Fase 4.)

### Shape

```json
{
  "patch_id": "p_2026-05-09T14:32:18Z_001",
  "applied_at": "2026-05-09T14:35:42Z",
  "applied_by": "agent",
  "mcp_call": {
    "tool": "update_controller_config",
    "args": {
      "bot_name": "pmm-btc-1",
      "controller_id": "001_pmm_binance_BTC-USDT",
      "field": "take_profit",
      "value": 0.00045
    }
  },
  "result": {
    "success": true,
    "old_value_confirmed": 0.0003,
    "new_value_set": 0.00045
  }
}
```

### `applied_by`

- `"agent"` en modo `auto`.
- `"human"` en modo `propose` cuando el humano aprueba.
- N/A en modo `shadow` (nunca se aplica).

## Validación pre-aplicación — orden estricto

```
PROPOSAL (LLM)
    ↓ validate_proposal_format
    ↓   ✗ malformed → log + skip
    ↓
EXPANDER (deterministic)
    ↓ validate_against_invariants (whitelist, max_delta, cooldown)
    ↓   ✗ todos los candidatos fuera de límites → log + skip
    ↓   ✗ algunos fuera → clamp + dedup
    ↓
CANDIDATES
    ↓
BACKTEST LOOP
    ↓ run baseline + N candidates en serie
    ↓   ✗ failure + on_backtest_failure=block → skip
    ↓   ✗ failure + on_backtest_failure=allow_with_flag → continue sin evidencia
    ↓
SCORING
    ↓ rank by score
    ↓   ✗ todos PnL < 0 → no_action
    ↓   ✗ todos worse than baseline → anti-evidence log + no_action
    ↓
SELECTED PATCH
    ↓ validate_no_oversub (final, con valores efectivos)
    ↓ check circuit_breaker
    ↓   ✗ falla → log + skip
    ↓
DISPATCH (según mode)
    ↓ propose → Telegram con botones (incluye candidates alternativos)
    ↓ shadow  → solo audit_log
    ↓ auto    → APPLIED PATCH directo
    ↓
APPLY (solo si propose=approved o auto)
    ↓ MCP tool call
    ↓ persistir en last_changes.json (para cooldowns futuros)
    ↓ persistir en audit_log.jsonl (entrada completa)
    ↓
OUTCOME (delayed +30min)
    ↓ medir efecto real con controller_performance
    ↓ append a la entrada del audit_log
```

## Flujo completo end-to-end (ejemplo)

```
T+0      Agente tickea
T+0.1s   Lee capital_state, market_regime, controller_performance
T+0.5s   LLM razona, emite PROPOSAL (1 proposal)
T+0.6s   Expansor genera CANDIDATES (3 candidatos)
T+0.7s   validate_against_invariants → 3 ok
T+0.8s   Backtest baseline (período subóptimo, ~60 min)
T+10s    Backtest c1
T+19s    Backtest c2
T+28s    Backtest c3
T+29s    Scoring → c1 wins
T+29.1s  Build SELECTED PATCH
T+29.2s  validate_no_oversub final → ok
T+29.3s  Mode propose → mensaje Telegram con 3 candidatos
T+29.4s  Persist en audit_log con verdict: pending
T+...    Humano aprueba c1
T+...    APPLIED PATCH → MCP tool call
T+...    Update last_changes.json
T+...+30min  Outcome measurement (controller_performance comparison)
```

## Casos especiales

### Backtest no soportado

Connector en `invariants.backtest.unsupported_connectors`:
- Skipear backtest loop.
- `expected_effect.based_on_backtest: false`.
- En modo `propose`: el mensaje al humano marca claramente "sin evidencia de backtest, basado en razonamiento del LLM".
- En modo `auto`: si `auto_mode.require_backtest_evidence: true` (default), skip. Si `false`, aplicar igual.

### Cooldown reciente

Si el patch viola algún cooldown:
- En `propose`: el humano puede ver el snooze restante en el mensaje. Le permite forzar (override) si quiere.
- En `auto`: skip silencioso.
- En `shadow`: log con razón "cooldown_blocked".

### Anti-evidence repetida (D-Q3)

Si el agente carga las últimas N entradas de `anti_evidence_log.jsonl` y ve un patrón:
- Mismo controller + mismo régimen + misma direction → 3+ entradas recientes.
- → Skip propuesta para ese controller en ese régimen hasta que cambie el régimen.

Esto evita "vicio" del LLM (caer en errores repetidos).

## Pendientes (para revisar más adelante)

- **Multi-field patches**: cuando el caso de uso aparezca (ej: agente que sabe que cambiar spreads sin cambiar take_profit es subóptimo).
- **Granularidad fina de magnitudes**: 5 niveles o input numérico del LLM. Solo si los 3 niveles se quedan cortos.
- **Auto-flip de dirección**: si todos los candidatos empeoran, probar la dirección opuesta automáticamente. **Desactivado en MVP** — preferimos `no_action` + log.
- **Score formula adaptativa**: hoy es fórmula fija. Podría ajustarse según régimen (en trending tal vez PnL pesa más, en ranging volumen).
- **Outcome learning**: comparar `expected_effect` (backtest) con `outcome_30min` (real) para detectar cuándo el backtest es engañoso. Input para tuning de score.
- **Patch rollback**: si el outcome 30min es muy peor que el expected, considerar revertir automáticamente. Requiere cuidado.

## Próximos pasos (Fase 2 continúa)

1. **2.3** — Diseñar la **memoria persistente** (consolidar `audit_log.jsonl`, `last_changes.json`, `anti_evidence_log.jsonl`).
2. **2.4** — Diseñar el **circuit breaker** (cuándo activar, quién lo desactiva, cuándo el agente lo respeta).
