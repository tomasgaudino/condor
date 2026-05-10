# Agent Memory — spec de la persistencia del agente adaptativo

> Cómo el agente recuerda, qué ve cada tick, cómo se cierra el loop de aprendizaje.
> Última actualización: 2026-05-09

## Filosofía

El agente sin memoria es un script que repite. Con memoria mal diseñada, es un script que se enrosca. Con memoria bien diseñada, es un agente que aprende dentro de los guardrails del usuario.

**3 categorías funcionales** de stores, cada una con reglas distintas:

| Categoría | Qué tiene | Quién escribe | Quién lee |
|---|---|---|---|
| **A. Series temporales** | Histórico crudo de routines | Routines (en cada tick) | Routines (agregadas) — el LLM **no** las ve crudas |
| **B. Memoria de decisiones** | Propuestas, outcomes, cooldowns | Agente / handler de respuestas | LLM las ve filtradas en su contexto |
| **C. Memoria semántica** | Insights humano-readable | Humano (principal) + agente (con confirmación) | LLM lee siempre |

## Inventario completo

```
trading_agents/<agent>/state/
├── capital_history/
│   └── <controller_id>.jsonl            ← A. serie temporal
├── regime_history/
│   └── <connector>_<pair>.jsonl         ← A. serie temporal
├── controller_performance/
│   └── <controller_id>.jsonl            ← A. serie temporal
├── audit_log.jsonl                       ← B. memoria de decisiones (mes corriente)
├── audit_log_2026-04.jsonl               ← B. archivado (mes pasado)
├── anti_evidence_log.jsonl               ← B. memoria de decisiones (mes corriente)
├── anti_evidence_log_2026-04.jsonl       ← B. archivado
└── last_changes.json                     ← B. map mutable (cooldowns)

trading_agents/<agent>/
└── learnings.md                          ← C. memoria semántica (humano principal)
```

## Categoría A — Series temporales de observación

### Características comunes

- Append-only, JSONL, una línea = un sample.
- Cadencia: 10 min (el tick del agente).
- Retention: **30 días**.
- Rotación: truncar líneas con `ts < now - 30d`.
- El LLM **nunca** las ve crudas — las routines las leen y le devuelven valores agregados (percentiles, velocidades, persistencia).

### `capital_history/<controller_id>.jsonl`

```json
{"ts":"2026-05-09T14:32:18Z","cmt_usd":26.1,"util":0.87,"util_w":0.087,"exec":3,"in_range":true}
```

Spec completa en `CAPITAL_DYNAMICS.md`.

### `regime_history/<connector>_<pair>.jsonl`

```json
{"ts":"2026-05-09T14:32:18Z","regime":"mean_reverting_high_vol","favorability":"suboptimal","confidence":"high"}
```

Spec en `MARKET_REGIME_SPEC.md`.

### `controller_performance/<controller_id>.jsonl`

```json
{"ts":"2026-05-09T14:32:18Z","r_pnl":7.42,"u_pnl":-0.18,"vol":31200.0,"pos":412}
```

Spec en `CONTROLLER_PERFORMANCE_SPEC.md`.

### Tamaño estimado

A 10 min de cadencia, 144 samples/día. 30 días = 4320 líneas. Estimado por archivo:
- `capital_history` ≈ 150 KB / 30d / controller.
- `regime_history` ≈ 50 KB / 30d / pair.
- `controller_performance` ≈ 150 KB / 30d / controller.

Para 5 controllers + 5 pairs ≈ **~3 MB total** después de 30 días. Trivial.

## Categoría B — Memoria de decisiones

### `audit_log.jsonl` — el registro completo de cada propuesta

**Append-only.** Cada propuesta del agente — aplicada o no — deja una línea acá. Es el corazón de la auditabilidad (D8) y la base del outcome learning.

#### Schema canónico (F6 — fuente única de verdad)

Este schema es la **fuente única de verdad** para el campo `audit_log`. Los demás specs que referencian campos (PROPOSE_MODE_SPEC, PATCH_CONTRACT_SPEC, BACKTEST_LOOP_SPEC, etc.) deben mantenerse consistentes con esta definición.

```json
{
  "tick_id": 145,
  "ts": "2026-05-09T14:32:18Z",
  "patch_id": "p_2026-05-09T14:32:18Z_001",

  "controller_id": "pmm-btc-1::001_pmm_binance_BTC-USDT",
  "bot_name": "pmm-btc-1",
  "config_name": "001_pmm_binance_BTC-USDT",
  "trading_pair": "BTC-USDT",
  "connector_name": "binance",

  "regime_observed": "mean_reverting_high_vol",
  "suboptimal_minutes": 47,
  "warmup_status": "normal",

  "llm_proposal": {
    "dimension": "take_profit",
    "direction": "increase",
    "magnitude_qualitative": "medium",
    "reasoning": "Régimen mean-reverting con NATR alto...",
    "caveats": []
  },

  "expanded_candidates": [
    {"id": "c1", "value": 0.00045, "delta_relative": "× 1.5", "clamped": false},
    {"id": "c2", "value": 0.00060, "delta_relative": "× 2.0", "clamped": false},
    {"id": "c3", "value": 0.00090, "delta_relative": "× 3.0", "clamped": false}
  ],

  "backtests": {
    "baseline":   {"net_pnl_quote": 0.12, "volume": 12400, "score": 1.3,  "duration_s": 8.3, "source": "cache_hit"},
    "candidate1": {"net_pnl_quote": 0.45, "volume": 9800,  "score": 1.42, "duration_s": 9.1, "source": "cache_miss"},
    "candidate2": {"net_pnl_quote": 0.78, "volume": 8200,  "score": 1.35, "duration_s": 7.9, "source": "cache_miss"},
    "candidate3": {"net_pnl_quote": 1.12, "volume": 5400,  "score": 1.12, "duration_s": 8.1, "source": "cache_miss"}
  },

  "selected": "c1",
  "selected_value": 0.00045,
  "old_value": 0.0003,

  "invariants_check": {
    "ok": true,
    "checks_passed": ["whitelist", "max_delta", "cooldown", "no_oversub"],
    "reasons": []
  },
  "mode": "propose",

  "telegram_chat_id": -3055388363,
  "telegram_message_id": 18234,

  "human_verdict": "approved",
  "verdict_at": "2026-05-09T14:33:42Z",
  "verdict_chose_candidate": "c1",
  "verdict_by": 1972815752,
  "snoozed_until_ts": null,

  "pre_apply_snapshot": {
    "net_pnl_quote": 7.42,
    "volume_traded": 31200.0,
    "total_positions": 412,
    "ts": "2026-05-09T14:33:43Z"
  },

  "applied": true,
  "applied_at": "2026-05-09T14:33:43Z",
  "apply_result": {"success": true, "raw_api_result": {}},
  "apply_error": null,

  "outcome_30min": null
}
```

#### Campos por sección

| Sección | Campos |
|---|---|
| **Identidad** | `tick_id`, `ts`, `patch_id`, `controller_id` (canonical), `bot_name`, `config_name`, `trading_pair`, `connector_name` |
| **Contexto** | `regime_observed`, `suboptimal_minutes`, `warmup_status` (normal/adaptive/cold_start) |
| **Decisión** | `llm_proposal`, `expanded_candidates`, `backtests`, `selected`, `selected_value`, `old_value`, `invariants_check`, `mode` |
| **Telegram** | `telegram_chat_id`, `telegram_message_id` (para editar el mensaje al expirar/aplicar) |
| **Verdict humano** | `human_verdict`, `verdict_at`, `verdict_chose_candidate`, `verdict_by`, `snoozed_until_ts` |
| **Pre-apply** | `pre_apply_snapshot` (necesario para outcome measurement) |
| **Apply** | `applied`, `applied_at`, `apply_result`, `apply_error` |
| **Outcome** | `outcome_30min` (populado por outcome loop) |

#### Estados de `human_verdict`

- `null` — agente todavía está procesando, o modo `shadow`.
- `pending` — modo `propose`, esperando respuesta del humano.
- `approved` — humano aprobó (con `verdict_chose_candidate` que puede ser distinto al `selected` si el humano eligió otro de los 3, D-Q4).
- `rejected` — humano rechazó.
- `snoozed` — humano pospuso, con `snoozed_until_ts`.
- `auto_applied` — modo `auto`, aplicado sin intervención humana.

#### Estados de `outcome_30min`

- `null` — todavía no se midió.
- `{...}` — populado por la tarea diferida del agente (ver sección "Outcome measurement").

### `anti_evidence_log.jsonl` — donde el LLM se equivocó

**Append-only.** Cuando todos los candidatos del backtest empeoran al baseline (D-Q3).

#### Schema

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
    "magnitude_qualitative": "medium",
    "reasoning": "..."
  },
  "candidates_tested": [
    {"id": "c1", "value": 0.00045, "score": 0.42, "vs_baseline": -0.18},
    {"id": "c2", "value": 0.00060, "score": 0.31, "vs_baseline": -0.29},
    {"id": "c3", "value": 0.00090, "score": 0.18, "vs_baseline": -0.42}
  ],
  "verdict": "all_candidates_worse_than_baseline",
  "interpretation_hint": "LLM may have wrong direction in this regime"
}
```

#### Cómo lo usa el agente

En cada tick, antes de razonar, el agente puede leer las **últimas 5 entradas** del mismo `(controller_id, regime_observed)`. Si encuentra patrón repetido (3+ entradas con misma `direction` + misma `dimension`), puede:
- Skipear ese controller en ese régimen.
- Razonar "ya lo intenté, no funcionó — tal vez probar otra dimensión".

### `last_changes.json` — map de cooldowns

**Mutable**, JSON map (no JSONL). Se sobreescribe atómicamente (tempfile + rename).

#### Schema

```json
{
  "001_pmm_binance_BTC-USDT": {
    "take_profit":  {"ts": "2026-05-09T14:33:43Z", "old": 0.0003, "new": 0.00045, "patch_id": "p_..."},
    "buy_spreads":  {"ts": "2026-05-09T13:01:42Z", "old": "0.0002,0.0006", "new": "0.0003,0.0008", "patch_id": "p_..."}
  },
  "002_pmm_binance_BTC-BRL": {
    "take_profit": {"ts": "2026-05-08T20:14:00Z", "old": 0.0002, "new": 0.0003, "patch_id": "p_..."}
  }
}
```

#### Uso

- Antes de validar un patch contra cooldowns: lectura del map.
- Después de aplicar un patch: upsert atómico.
- El validador chequea contra `invariants.cooldowns.per_controller_per_field_minutes`.

### Lifecycle de la categoría B

| Aspecto | Decisión |
|---|---|
| Retention | **Indefinida** — preservar historia completa para auditoría y análisis post-hoc |
| Rotación | Mensual al boundary del mes: `audit_log.jsonl` → `audit_log_YYYY-MM.jsonl`, archivo nuevo vacío |
| Truncado | **No** — los archivos archivados se pueden comprimir o mover offline pero no se pierden |
| Tamaño estimado | `audit_log` ≈ 1 MB/año, `anti_evidence` <100 KB/año |

**Por qué rotación mensual y no truncado**: queremos poder responder "este patch lo apliqué en abril, ¿cómo le fue?" en diciembre. Los archivos por mes son fáciles de archivar.

## Categoría C — Memoria semántica

### `learnings.md`

Mantiene el formato actual de Condor (ver `pmm_mister_supervisor/learnings.md` como ejemplo):

```markdown
# Learnings

## Active Insights
- [2026-05-04 21:33] insight 1...
- [2026-05-04 22:33] insight 2...

## Retired Insights
- [2026-04-15] insight viejo...
```

#### Política de escritura (Q4: opción b)

- **Lectura**: el LLM lee `learnings.md` completo en cada tick.
- **Escritura por humano**: principal — el humano edita manualmente.
- **Escritura por agente**: el agente puede **proponer** insights nuevos pero requiere confirmación humana antes de persistir.

#### Mecánica de propuesta de insights por el agente

Cuando el agente detecta un patrón (ej: 5 patches consecutivos del mismo tipo, todos rechazados o con anti-evidence), puede:

1. Detectar el patrón al razonar.
2. Incluir en su output del tick un campo opcional:

```json
{
  "action": "propose",
  "proposals": [...],
  "insight_proposed": {
    "title": "take_profit increases in mean_rev_high_vol regime are consistently rejected",
    "evidence": [
      "audit_log entries: tick#142, tick#138, tick#129 — all rejected by human",
      "anti_evidence_log: 2 entries this week"
    ],
    "suggested_action": "skip take_profit increase proposals when regime is mean_rev_high_vol AND human has rejected last 3"
  }
}
```

3. La capa de orquestación lo muestra al humano como mensaje separado.
4. Humano aprueba/rechaza/edita.
5. Si aprueba → se persiste en `learnings.md` con timestamp.

**No es auto-promotion**. Mantenemos `learnings.md` curado por humano con sugerencias del agente.

> **Pendiente futuro (opción c)**: auto-promotion automático cuando el patrón es muy claro. Riesgo: deshabilitar cosas legítimas por mala muestra. Postpuesto.

## Read context del LLM (qué ve cada tick) — Q1 confirmado

El agente, en cada tick, construye el **contexto del LLM** así:

### Siempre (lectura completa)

1. **`agent.md` (sección Policy)** — la policy completa del agente.
2. **`learnings.md` (sección Active Insights)** — todos los insights activos, ignorar Retired.
3. **Output de las 3 routines del tick** — `capital_state`, `market_regime`, `controller_performance`.

### Filtrado por relevancia

Por cada controller en scope:

4. **Últimas 10 entradas de `audit_log.jsonl`** filtradas por `controller_id == X`. Decoradas con `outcome_30min` si lo tienen, marcadas como "pending outcome" si no.

5. **Últimas 5 entradas de `anti_evidence_log.jsonl`** filtradas por `controller_id == X AND regime_observed == current_regime`.

6. **Estado de cooldowns** del controller — leer `last_changes.json[controller_id]` y reportar qué campos están en cooldown y cuándo expiran.

### Nunca (no van al LLM)

- Los archivos JSONL crudos de `capital_history/`, `regime_history/`, `controller_performance/`. Estos los leen las routines y le devuelven al LLM las agregaciones ya calculadas.
- Archivos `audit_log_YYYY-MM.jsonl` archivados (solo el del mes corriente).

### Volumen aproximado del contexto

- Policy + learnings: ~2 KB.
- Routines (3): ~5 KB.
- Audit log decorado (10 entries × 2 controllers): ~10 KB.
- Anti-evidence (5 × 2): ~3 KB.
- Cooldowns: <1 KB.

**Total: ~20 KB de contexto** en cada tick. Muy razonable para Claude / LLMs modernos. Hay margen de sobra para más controllers.

## Outcome measurement — Q3 confirmado

Cierra el loop de aprendizaje: medimos qué pasó realmente después de un cambio.

### Mecánica (opción a, cross-archivo — F7)

En cada tick (10 min), el agente hace una pasada rápida sobre `audit_log.jsonl` **y el archivo del mes anterior si estamos en los primeros 7 días del mes** (resuelve F7: patches aplicados al final de un mes con outcomes que cruzan al mes siguiente):

```python
def find_pending_outcomes(agent_dir: Path, now: datetime) -> list[tuple[Path, dict]]:
    """Scan current month + previous month (if recent) for pending outcomes."""
    state = agent_dir / "state"
    files_to_scan = [state / "audit_log.jsonl"]
    
    # Si estamos en los primeros 7 días del mes, leer también el mes anterior
    if now.day <= 7:
        prev_month = (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
        prev_file = state / f"audit_log_{prev_month}.jsonl"
        if prev_file.exists():
            files_to_scan.append(prev_file)
    
    pending = []
    for path in files_to_scan:
        if not path.exists():
            continue
        for entry in read_jsonl_all(path):
            if entry.get("applied") and entry.get("outcome_30min") is None:
                applied_at = parse_ts(entry["applied_at"])
                if (now - applied_at) >= timedelta(minutes=30):
                    pending.append((path, entry))
    return pending


def process_pending_outcomes(agent_dir: Path, now: datetime):
    for path, entry in find_pending_outcomes(agent_dir, now):
        outcome = measure_outcome(entry, agent_dir, now)
        # update_audit_outcome busca el patch_id en current+previous month files
        update_audit_outcome(entry["patch_id"], outcome, agent_dir)
```

### Cómo se mide

```python
def measure_outcome(audit_entry: dict, agent_dir: Path, now: datetime) -> dict:
    # 1. Snapshot pre-cambio (en el audit_entry — el agente lo guarda al aplicar)
    pre_pnl = audit_entry["pre_apply_snapshot"]["net_pnl_quote"]
    pre_vol = audit_entry["pre_apply_snapshot"]["volume_traded"]
    
    expected = audit_entry["backtests"][audit_entry["selected"]]
    expected_pnl_delta = expected["net_pnl_quote"]
    expected_volume_delta = expected["volume"]
    
    # 2. Performance post-cambio: lee snapshots persistidos en
    # state/controller_performance/<sanitized_canonical_id>.jsonl
    window = read_controller_performance_window(
        agent_dir,
        canonical_id=audit_entry["controller_id"],
        from_ts=parse_ts(audit_entry["applied_at"]),
        to_ts=now,
    )
    if window is None or window.samples_count < 2:
        # No hay suficientes samples post-apply — outcome no medible aún
        return None
    
    actual_pnl_delta = window.net_pnl_end - window.net_pnl_start
    actual_volume_delta = window.volume_end - window.volume_start
    
    # 3. Score de match (F2 — definición concreta abajo)
    match_score = compute_match_score(
        actual_pnl_delta, expected_pnl_delta,
        actual_volume_delta, expected_volume_delta,
    )
    
    if match_score > 0.7:
        quality = "good"
    elif match_score >= 0.4:
        quality = "mediocre"
    else:
        quality = "bad"
    
    return {
        "measured_at": now.isoformat(),
        "actual_pnl_delta": actual_pnl_delta,
        "actual_volume_delta": actual_volume_delta,
        "expected_pnl_delta": expected_pnl_delta,
        "expected_volume_delta": expected_volume_delta,
        "match_quality": quality,
        "match_score": match_score,
        "notes": "",
    }
```

### `read_controller_performance_window` (F2)

Lee snapshots persistidos del store de la routine `controller_performance` y devuelve los deltas entre el primer y último sample en la ventana:

```python
@dataclass
class WindowSnapshot:
    from_ts: datetime
    to_ts: datetime
    net_pnl_start: float
    net_pnl_end: float
    volume_start: float
    volume_end: float
    positions_start: int
    positions_end: int
    samples_count: int


def read_controller_performance_window(
    agent_dir: Path,
    canonical_id: str,
    from_ts: datetime,
    to_ts: datetime,
) -> WindowSnapshot | None:
    """
    Read state/controller_performance/<sanitized_id>.jsonl, return deltas
    between first and last sample within [from_ts, to_ts]. None if <2 samples.
    
    canonical_id format: "{bot_name}::{config_name}" — sanitized via :: → __
    """
    safe_id = canonical_id.replace("::", "__")
    path = agent_dir / "state" / "controller_performance" / f"{safe_id}.jsonl"
    if not path.exists():
        return None
    
    samples = []
    for entry in read_jsonl_all(path):
        try:
            ts = parse_ts(entry["ts"])
            if from_ts <= ts <= to_ts:
                samples.append((ts, entry))
        except Exception:
            continue
    
    if len(samples) < 2:
        return None
    
    samples.sort(key=lambda x: x[0])
    first_ts, first = samples[0]
    last_ts, last = samples[-1]
    
    return WindowSnapshot(
        from_ts=first_ts, to_ts=last_ts,
        net_pnl_start=first["r_pnl"] + first["u_pnl"],
        net_pnl_end=last["r_pnl"] + last["u_pnl"],
        volume_start=first["vol"], volume_end=last["vol"],
        positions_start=first["pos"], positions_end=last["pos"],
        samples_count=len(samples),
    )
```

### `compute_match_score` (F2)

Mide qué tan bien el outcome real matcheó la predicción del backtest. Retorna `[0.0, 1.0]`:

```python
def compute_match_score(
    actual_pnl_delta: float,
    expected_pnl_delta: float,
    actual_volume_delta: float,
    expected_volume_delta: float,
) -> float:
    """
    Weighted average of per-dimension match:
      - PnL match: 0.6 weight (más importante para confianza en backtest)
      - Volume match: 0.4 weight
    """
    pnl_match = _dimension_match(actual_pnl_delta, expected_pnl_delta)
    vol_match = _dimension_match(actual_volume_delta, expected_volume_delta)
    return 0.6 * pnl_match + 0.4 * vol_match


def _dimension_match(actual: float, expected: float) -> float:
    """
    Single-dimension match score in [0.0, 1.0]:
      - Both ≈ 0 → 1.0 (no signal either way)
      - Sign mismatch → 0.0 (predicción totalmente errada)
      - Same sign → 1.0 - relative_error (clamped)
    """
    eps = 1e-6
    if abs(expected) < eps and abs(actual) < eps:
        return 1.0
    if abs(expected) < eps:
        return max(0.0, 1.0 - min(abs(actual), 1.0))
    if (actual > 0) != (expected > 0):
        return 0.0
    rel_error = abs(actual - expected) / max(abs(expected), eps)
    return max(0.0, 1.0 - min(rel_error, 1.0))
```

**Pesos `0.6/0.4`**: PnL pesa más para juzgar si el backtest engine es confiable. Volume sirve como check secundario. Ajustables con datos reales (Fase F2 outcome learning).

### Pre-apply snapshot — campo nuevo en audit_log

Para que `measure_outcome` pueda calcular deltas reales necesita conocer el estado del controller **al momento del apply**. Por eso el handler de apply persiste `pre_apply_snapshot` antes de invocar la MCP tool:

```json
{
  "pre_apply_snapshot": {
    "net_pnl_quote": 7.42,
    "volume_traded": 31200.0,
    "total_positions": 412,
    "ts": "2026-05-09T14:33:43Z"
  }
}
```

Este campo se agrega al schema canónico del `audit_log` (ver "Schema canónico" más abajo).

### Window de 30 min — caveat conocido

Para campos como `take_profit` que afectan **solo executors nuevos**, 30 min puede ser corto si la rotación de executors es lenta. La routine reporta el caveat en `notes` cuando detecta esto:

```
"notes": "Window may be short — only N new executors created since apply"
```

### Por qué importa

Acumular outcomes permite responder preguntas críticas:
- ¿El backtest engine miente sistemáticamente en algún régimen?
- ¿El score formula está bien calibrado?
- ¿El LLM acierta más en `mean_reverting` que en `trending`?

Insights de este análisis pueden ir a `learnings.md` (con confirmación humana).

## Helpers de IO

### Bajo nivel — `state/io.py`

```python
def append_jsonl(path: Path, entry: dict) -> None: ...
def read_jsonl_window(path: Path, since: datetime) -> list[dict]: ...
def read_jsonl_last_n(path: Path, n: int) -> list[dict]: ...
def read_jsonl_filter(path: Path, predicate: Callable, limit: int = None) -> list[dict]: ...
def truncate_jsonl_older_than(path: Path, cutoff: datetime) -> int: ...
def rotate_monthly(path: Path, current_month: str) -> Path: ...
def upsert_json_map(path: Path, key: str, value: dict) -> None: ...  # atómico (tempfile + rename)
def read_json_map(path: Path) -> dict: ...
```

### Alto nivel — `state/queries.py`

Funciones que componen lecturas comunes:

```python
def get_recent_decisions(controller_id: str, n: int = 10, agent_dir: Path) -> list[Decision]:
    """Last N entries from audit_log filtered by controller_id."""

def get_anti_evidence_for_regime(
    controller_id: str, regime: str, n: int = 5, agent_dir: Path
) -> list[AntiEvidence]:
    """Anti-evidence entries for same (controller, regime)."""

def get_cooldown_status(
    controller_id: str, field: str, invariants: dict, agent_dir: Path
) -> CooldownStatus:
    """Returns: ok | locked_until_ts | last_change_ts."""

def get_utilization_percentiles(
    controller_id: str, hours: int, agent_dir: Path
) -> Percentiles:
    """Reads capital_history, computes p50/p95/max."""

def get_regime_persistence(
    connector: str, pair: str, agent_dir: Path
) -> int:
    """Minutes since last canonical regime change."""

def get_pending_outcomes(agent_dir: Path, now: datetime) -> list[Entry]:
    """Entries with applied=True and outcome_30min=null and applied_at + 30min <= now."""

def update_audit_outcome(patch_id: str, outcome: dict, agent_dir: Path) -> None:
    """Find audit entry by patch_id and add outcome (in-place rewrite)."""
```

**Caveat de `update_audit_outcome`**: requiere reescribir un archivo append-only para mutar una línea. Implementación: leer todo, mutar la línea, reescribir atómicamente. Costoso pero infrecuente (1 vez por patch). Aceptable.

**Alternativa futura**: separar audit_log en dos archivos: `audit_proposals.jsonl` (append-only puro) y `audit_outcomes.jsonl` (también append-only, joineado por `patch_id` al leer). Más limpio pero más complejo. Postpuesto.

## Lectura cross-store: el "context bundle"

Para que el código del agente no haga 10 lecturas dispersas, definimos un helper que arma todo el contexto del LLM en una sola llamada:

```python
def build_llm_context(agent_dir: Path, scope_controllers: list[str], routines_output: dict) -> dict:
    """Assemble the complete read context for one tick of the LLM."""
    return {
        "policy": read_policy_from_agent_md(agent_dir / "agent.md"),
        "learnings": read_active_insights(agent_dir / "learnings.md"),
        "routines": routines_output,  # ya pre-computado por las routines
        "per_controller": {
            ctrl_id: {
                "recent_decisions": get_recent_decisions(ctrl_id, n=10, agent_dir=agent_dir),
                "anti_evidence":    get_anti_evidence_for_regime(
                    ctrl_id,
                    routines_output["market_regime"][ctrl_id]["summary"]["canonical_regime"],
                    n=5,
                    agent_dir=agent_dir
                ),
                "cooldowns":        get_all_cooldowns_for(ctrl_id, agent_dir=agent_dir),
            }
            for ctrl_id in scope_controllers
        }
    }
```

## Estrategias de robustez

### Crashes del agente

- Todos los appends son **single-write** (una sola syscall a `write`). Si crashea durante un append, peor caso: archivo termina con línea parcial. La función de lectura debe ignorar líneas no-parseables.
- `last_changes.json` se escribe via `tempfile + rename` → atómico, nunca corrompido.

### Concurrent access

- Suponemos un único proceso del agente por agente_dir. No hay multi-writer.
- Si en el futuro hay UI web concurrente leyendo, usa los mismos helpers de lectura — están diseñados para ser idempotentes.

### Backups

- El `state/` debe estar bajo git **opcionalmente** — `learnings.md` sí, los JSONL probablemente no (se rotan/truncan).
- `.gitignore` recomendado:
  ```
  state/capital_history/
  state/regime_history/
  state/controller_performance/
  ```
  Los logs de decisiones (`audit_log`, `anti_evidence_log`) **sí** podrían ir a git si querés versión auditable. Decisión del usuario al deploy.

## Pendientes (revisar más adelante)

- **Auto-promotion de insights** (opción c que descartamos): auto-mover patrones detectados a `learnings.md` sin confirmación humana. Postpuesto por riesgo de deshabilitar cosas legítimas.
- **Outcome window adaptativo**: en vez de 30 min fijo, ajustar según tipo de cambio (take_profit → más largo, spreads → más corto).
- **Audit_log split**: separar `proposals.jsonl` y `outcomes.jsonl` para evitar reescrituras.
- **Compresión de archivos archivados**: gzip los `audit_log_YYYY-MM.jsonl` viejos.
- **Métricas del agente**: dashboard que lea audit_log y muestre "% propuestas aprobadas", "match_quality promedio", "controllers más cambiados", etc.

## Próximos pasos

1. **2.4** — Diseñar el **circuit breaker** (cuándo activar, quién lo desactiva, qué bloquea).
2. (Implementación) — Cuando se llegue a Fase 5, implementar `state/io.py`, `state/queries.py`, y la pasada de outcomes en el tick del agente.
