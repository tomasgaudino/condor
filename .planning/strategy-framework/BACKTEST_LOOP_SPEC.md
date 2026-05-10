# Backtest Evidence Loop — spec consolidada

> Capa de evidencia empírica que respalda cada propuesta del agente. Define disparador, ventana, expansor, score, cache y manejo de errores.
> Última actualización: 2026-05-09

## Filosofía

Sin backtest, las propuestas del agente son intuición pura. Con backtest, son intuición + evidencia empírica del período subóptimo. Esta capa es la **diferencia entre "el LLM razona"** y **"el LLM razona y respalda con datos"**.

**Decisión madre (D9)**: cada propuesta que va al humano (modo `propose`) o que se aplica (modo `auto`) pasó por:
1. 1 backtest del baseline (config actual, durante el período subóptimo).
2. Hasta 3 backtests con candidatos alternativos.
3. Función de score que elige el ganador.

## Diagrama del ciclo

```
PROPOSAL del LLM
    ↓
trigger evaluation
    ✗ no_action si no se cumplen las condiciones
    ↓
EXPANDER (PATCH_CONTRACT_SPEC.md)
    → 1-3 candidatos numéricos
    ↓
WINDOW SELECTION
    → ventana = período subóptimo del controller
    ↓
BACKTEST LOOP (en serie, con lock global)
    1. baseline backtest (config actual)
    2-N. candidate backtests
    ↓
SCORING
    → ranking de candidatos por score
    ↓
SELECTED PATCH
    → el ganador + evidencia + alternativas
    ↓
Validación final + dispatch (PATCH_CONTRACT_SPEC.md)
```

## 2.5.1 — Disparador

### Condiciones para disparar el ciclo

Por cada controller en scope, en cada tick del agente:

```python
def should_trigger_backtest_loop(controller_id, ctx) -> tuple[bool, str]:
    # Orden: del más barato al más caro

    # 1. Estado del agente
    if ctx.agent_status.status == "paused":
        return False, "agent_paused"

    # 2. Diagnóstico de performance
    if not ctx.controller_performance[controller_id]["diagnostic"]["suboptimal_now"]:
        return False, "not_suboptimal"

    # 3. Régimen de mercado
    favorability = ctx.market_regime[controller_id]["summary"]["favorability"]
    if favorability == "optimal":
        return False, "regime_optimal"

    # 4. Anti-evidence streak (vicio detectado)
    regime = ctx.market_regime[controller_id]["summary"]["canonical_regime"]
    recent_anti = get_anti_evidence_for_regime(controller_id, regime, n=5)
    same_pattern_count = sum(
        1 for e in recent_anti
        if e["llm_proposal"]["dimension"] == ... # se sabe después del LLM
    )
    # Esta check se hace después de que el LLM emitió proposal,
    # NO antes. Acá solo descartamos si las últimas 3 entradas son
    # del mismo (controller, regime) sin importar dimensión.
    if len([e for e in recent_anti[-3:]]) >= 3:
        return False, "anti_evidence_streak"

    # 5. Cooldowns activos
    cooldowns = get_all_cooldowns_for(controller_id)
    if any(c.is_active for c in cooldowns):
        return False, "cooldowns_active"

    return True, "ok"
```

### Output

Si dispara → flujo completo continúa.

Si no dispara → entrada estructurada al `audit_log` con razón:

```json
{
  "tick_id": 145,
  "controller_id": "001_pmm_binance_BTC-USDT",
  "ts": "2026-05-09T14:32:18Z",
  "trigger_evaluated": false,
  "reason": "regime_optimal",
  "checks": {
    "agent_status": "active",
    "suboptimal_now": false,
    "favorability": "optimal",
    "anti_evidence_recent": 0,
    "cooldowns_active": []
  }
}
```

Útil para debug y para tracking de cuándo el agente "no hizo nada y por qué".

## 2.5.2 — Selección de ventana

### Algoritmo

```python
def select_backtest_window(controller_perf: dict, now: datetime) -> WindowSpec:
    persistence_min = controller_perf["diagnostic"]["suboptimal_period_minutes"]
    end_ts = now
    start_ts = now - timedelta(minutes=max(persistence_min, 30))  # mínimo 30 min
    return WindowSpec(start=start_ts, end=end_ts, resolution="1m")
```

### Decisiones (Q1 confirmado)

- **Sin cap de ventana** (Q1=a). Si el régimen subóptimo lleva 3 días, la ventana es de 3 días.
- **Mínimo 30 min**: si por alguna razón llegamos acá con `suboptimal_period_minutes < 30`, forzamos 30 min como piso (no debería pasar — el disparador requiere ≥30, pero defensa en profundidad).
- **Resolución 1m**: estándar para PMM. Configurable a futuro (ej: 5s para HFT).

### Protección real: timeout

La verdadera protección contra ventanas absurdas es el **timeout global del ciclo** (D9: 5 minutos). Si una ventana grande hace que un backtest tarde 4 min, perfecto — más datos = mejor evidencia. Si pasa de 5 min, el timeout corta y se aplica la política `on_backtest_failure`.

## 2.5.3 — Expansor determinístico

Spec completa en [`PATCH_CONTRACT_SPEC.md`](PATCH_CONTRACT_SPEC.md). Resumen:

- Tablas por tipo de campo (percentage / absolute) × magnitud (small/medium/large) × dirección.
- `small` → 1 candidato. `medium`/`large` → 2 candidatos cada uno.
- Clamp por invariants + clamp por dominio + dedup.

No se duplica acá. El backtest loop **consume** los candidatos del expansor.

## 2.5.4 — Función de score

### Fórmula

```python
def score(result: BacktestResult, baseline: BacktestResult) -> float:
    """
    Score basado en criterio del usuario:
    'PnL ≈ 0+ con mucho volumen > PnL alto con poco volumen'
    """
    pnl = result.net_pnl_quote
    vol = result.total_volume

    # Filtro duro: PnL negativo se rechaza
    if pnl < 0:
        return float("-inf")

    # Normalización
    pnl_norm = min(pnl, baseline.net_pnl_quote * 2) / max(baseline.net_pnl_quote, 1.0)
    vol_norm = vol / max(baseline.total_volume, 1.0)

    # Clamp para evitar valores absurdos cuando baseline ≈ 0
    pnl_norm = min(pnl_norm, 10.0)
    vol_norm = min(vol_norm, 10.0)

    # Score: vol pesa más que pnl (criterio del usuario)
    return vol_norm + 0.3 * pnl_norm
```

### Score baseline

Por convención: `score(baseline, baseline) = 1.0 + 0.3 = 1.3`. Un candidato con score > 1.3 mejoró al baseline.

### Decomposition (para auditoría)

Junto con el score, devolver el desglose:

```python
def score_with_decomposition(result, baseline):
    s = score(result, baseline)
    pnl = result.net_pnl_quote
    vol = result.total_volume
    return {
        "score": s,
        "components": {
            "pnl_norm": min(min(pnl, baseline.net_pnl_quote * 2) / max(baseline.net_pnl_quote, 1.0), 10.0)
                if pnl >= 0 else None,
            "vol_norm": min(vol / max(baseline.total_volume, 1.0), 10.0),
            "weighted_pnl": 0.3 * pnl_norm,
            "raw_pnl": pnl,
            "raw_vol": vol,
            "baseline_pnl": baseline.net_pnl_quote,
            "baseline_vol": baseline.total_volume,
        },
        "formula": "vol_norm + 0.3 * pnl_norm",
        "rejected_for_negative_pnl": pnl < 0,
    }
```

Útil para mostrar al humano en modo `propose` y para análisis post-hoc.

### Iteración futura — outcome-driven calibration

El campo `match_quality` que mide outcome (en `MEMORY_SPEC.md`) es **el feedback loop** para iterar la fórmula:

- Si candidatos con score alto consistentemente tienen `match_quality: bad` → la fórmula está mal calibrada.
- Datos requeridos: ~N semanas de outcomes acumulados.
- Acción: ajustar el peso `0.3` empíricamente.

**Esto es trabajo de Fase F2** (análisis histórico). Por ahora la fórmula es la propuesta y se itera con datos reales.

## 2.5.5 — Cache de backtests

### Cache key

Hash determinístico de los inputs del backtest:

```python
import hashlib
import json

EXCLUDED_CONFIG_FIELDS = {"id", "created_at", "updated_at"}

def canonicalize_config(config: dict) -> str:
    """Serialización determinística para hash estable."""
    cleaned = {k: v for k, v in sorted(config.items()) if k not in EXCLUDED_CONFIG_FIELDS}
    return json.dumps(cleaned, sort_keys=True, separators=(",", ":"))

def backtest_cache_key(
    controller_id: str,
    config_snapshot: dict,
    start_ts: datetime,
    end_ts: datetime,
    resolution: str
) -> str:
    payload = {
        "ctrl": controller_id,
        "cfg": canonicalize_config(config_snapshot),
        "start": int(start_ts.timestamp()),
        "end": int(end_ts.timestamp()),
        "res": resolution,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]
```

### Backend del cache

Reusar el `BacktestStore` existente (`condor/backtest_store.py`):

```python
async def run_backtest_cached(
    client, controller_id, config_snapshot, start, end, resolution
) -> tuple[BacktestResult, str]:
    """Returns (result, source) where source ∈ {'cache_hit', 'cache_miss'}"""
    key = backtest_cache_key(controller_id, config_snapshot, start, end, resolution)

    cached = backtest_store.get(key)
    if cached is not None:
        return cached, "cache_hit"

    result = await client.run_backtest(
        config=config_snapshot,
        start_time=int(start.timestamp()),
        end_time=int(end.timestamp()),
        backtesting_resolution=resolution,
    )

    backtest_store.put(key, result, metadata={
        "controller_id": controller_id,
        "ts_cached": now().isoformat(),
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
    })

    return result, "cache_miss"
```

### Hit rate esperado

- **Baseline backtest**: hit rate alto (~80-100%) — el baseline del mismo período subóptimo se repite mientras el período persiste.
- **Candidatos**: hit rate bajo (~0-20%) — valores nuevos cada tick.
- **Promedio del ciclo (1 baseline + 3 candidatos)**: ~25-40% hit rate global.

Esto traduce en ~30-50% reducción de tiempo total del ciclo en escenarios típicos.

### Invalidación

**No hay invalidación por tiempo** — un backtest del período T1-T2 con config C da el mismo resultado siempre. Si la config cambia, el hash es distinto, entrada vieja queda huérfana pero no incorrecta.

### Cleanup (Q2 confirmado)

**Tarea diferida una vez por día** (similar al outcome loop):

```python
def daily_cache_cleanup(now: datetime, retention_days: int = 30):
    cutoff = now - timedelta(days=retention_days)
    removed = backtest_store.purge_older_than(cutoff)
    log.info(f"Cache cleanup: removed {removed} entries older than {cutoff}")
```

Cuándo se ejecuta:
- En cada tick, el agente revisa si la última cleanup fue hace > 24h.
- Si sí → ejecuta cleanup en background.
- Si no → skip.

**Marker file**: `state/.last_cache_cleanup` con timestamp del último cleanup. Lectura/escritura barata.

## 2.5.6 — Manejo de errores

### Error 1: Connector no soportado

Detección **temprana** (antes de intentar el backtest):

```python
def can_backtest(controller_config: dict, invariants: dict) -> bool:
    return controller_config["connector_name"] not in invariants["backtest"]["unsupported_connectors"]
```

Connectors no soportados (de `invariants.yaml`):
- `hyperliquid`
- `dydx`
- `kraken`
- `coinbase_advanced_trade`

Aplicar política:
- `on_backtest_failure: "block"` (default) → skip ese controller, log + `no_action`.
- `on_backtest_failure: "allow_with_flag"` → proponer marcando `based_on_backtest: false`.

### Error 2: Datos OHLCV insuficientes

Hummingbot levanta excepción si no hay velas suficientes. Capturar:

```python
try:
    result = await client.run_backtest(...)
except DataInsufficientError as e:
    return BacktestError(type="data_insufficient", detail=str(e))
```

Aplicar misma política `on_backtest_failure`.

### Error 3: Timeout

D9: latencia hasta 5 min. Implementar:

```python
async def run_backtest_with_timeout(client, ..., timeout_seconds: int = 300):
    try:
        return await asyncio.wait_for(
            client.run_backtest(...),
            timeout=timeout_seconds
        )
    except asyncio.TimeoutError:
        return BacktestError(type="timeout", detail=f"exceeded {timeout_seconds}s")
```

**Comportamiento del timeout en el ciclo completo**:

```python
async def run_full_cycle(controller_id, ...):
    # Cycle-level timeout: 5 min total
    try:
        async with asyncio.timeout(300):
            baseline = await run_backtest_cached(... config_actual ...)
            candidates_results = []
            for cand in candidates:
                # Si ya gastamos >4 min en baseline, abortamos
                result = await run_backtest_cached(... cand ...)
                candidates_results.append(result)
            return select_winner(baseline, candidates_results)
    except asyncio.TimeoutError:
        return BacktestCycleError(type="cycle_timeout")
```

Si timeout durante el ciclo:
- Cancelar candidatos pendientes (no tiene sentido seguir).
- Aplicar `on_backtest_failure` policy.
- Log estructurado: timeout puede ser señal de engine saturado o ventana muy grande.

### Error 4: Engine compartido

D9 caveat:
> El engine es semi-singleton, dos backtests concurrentes pueden pisarse.

**Q3 confirmado**: lock global como default + flag de revisión en implementación.

```python
backtest_lock = asyncio.Lock()

async def run_backtest_serialized(client, ...):
    async with backtest_lock:
        return await run_backtest_with_timeout(client, ...)
```

> **Pendiente Fase 5**: chequear si el lock es realmente necesario una vez que el engine esté en producción. Posible que con el agente single-threaded no haya concurrencia real. Si se confirma que no, simplificar.

### Política unificada de log

Cualquier error backend → entrada en `audit_log.jsonl`:

```json
{
  "tick_id": 145,
  "controller_id": "001_pmm_binance_BTC-USDT",
  "ts": "2026-05-09T14:32:18Z",
  "stage": "backtest",
  "error": {
    "type": "timeout",
    "detail": "exceeded 300s on candidate c2",
    "during": "candidate_backtest"
  },
  "policy_applied": "block",
  "outcome": "no_action"
}
```

Útil para monitorear frecuencia de cada tipo de error y mejorar el sistema.

## Performance esperada del ciclo completo

Estimaciones (D9):

| Componente | Tiempo típico | Notas |
|---|---|---|
| Trigger evaluation | <100ms | Solo lecturas |
| Window selection | <10ms | Cálculo trivial |
| Expander | <50ms | Determinístico, sin IO |
| Baseline backtest | 3-30s | Depende de ventana |
| Candidate backtest × 3 | 9-90s | En serie |
| Scoring | <50ms | Aritmética |
| Validación final | <100ms | Lecturas |
| **Total típico** | **~12-40s** | Con cache hit en baseline: **~9-30s** |
| **Worst case** | **~5 min** | Cap por timeout |

Con cadencia del agente de 10 min (D9), hay overhead suficiente para 1-2 ciclos completos por tick. Si hay >2 controllers en scope con subóptimo simultáneo, se procesan **en serie** y los que no entren al tick se atienden el siguiente.

## Estructura del output del ciclo

Para integrar con el `SELECTED PATCH` de `PATCH_CONTRACT_SPEC.md`:

```json
{
  "controller_id": "001_pmm_binance_BTC-USDT",
  "field": "take_profit",
  "window": {
    "start_ts": "2026-05-09T13:32:18Z",
    "end_ts": "2026-05-09T14:32:18Z",
    "duration_minutes": 60,
    "resolution": "1m"
  },
  "baseline": {
    "value": 0.0003,
    "result": { "net_pnl_quote": 0.12, "total_volume": 12400, ... },
    "score_decomposition": { "score": 1.3, "components": {...} },
    "source": "cache_hit"
  },
  "candidates": [
    {
      "id": "c1", "value": 0.00045,
      "result": { "net_pnl_quote": 0.45, "total_volume": 9800, ... },
      "score_decomposition": { "score": 1.42, "components": {...} },
      "source": "cache_miss",
      "duration_seconds": 8.3
    },
    {
      "id": "c2", "value": 0.00060,
      "result": { "net_pnl_quote": 0.78, "total_volume": 8200, ... },
      "score_decomposition": { "score": 1.35, "components": {...} },
      "source": "cache_miss",
      "duration_seconds": 7.9
    },
    {
      "id": "c3", "value": 0.00090,
      "result": { "net_pnl_quote": 1.12, "total_volume": 5400, ... },
      "score_decomposition": { "score": 1.12, "components": {...} },
      "source": "cache_miss",
      "duration_seconds": 8.1
    }
  ],
  "selected": {
    "id": "c1",
    "value": 0.00045,
    "score": 1.42,
    "vs_baseline_delta": 0.12
  },
  "rejected": [
    { "id": "c2", "score": 1.35, "reason": "lower score" },
    { "id": "c3", "score": 1.12, "reason": "lower score" }
  ],
  "verdict": "winner_found",
  "errors": [],
  "total_duration_seconds": 31.2,
  "cache_stats": { "hits": 1, "misses": 3 }
}
```

## Casos especiales

### Todos los candidatos PnL < 0

Score = -inf para todos. `select_winner` retorna `None`.

Acción: `verdict = "all_candidates_negative_pnl"` → ciclo completa con `no_action` (no se propone nada).

### Todos los candidatos peor que baseline (score < 1.3)

Cubierto en `PATCH_CONTRACT_SPEC.md` (D-Q3): `no_action` + entrada en `anti_evidence_log.jsonl`.

### Ningún candidato válido (todos clamped a baseline)

Si después del clamp todos los candidatos colapsaron al valor del baseline → `verdict = "no_valid_candidates"` → `no_action`. Log especial.

### Backtest del baseline falla pero candidatos OK

Sin baseline no podemos scorear → `no_action`. Log: `baseline_backtest_failed`.

## Pendientes (revisar más adelante)

- **Outcome-driven score calibration**: ajustar el peso `0.3` con datos reales (Fase F2).
- **Score adaptativo por régimen**: distinta fórmula en trending vs ranging. Postpuesto.
- **Backtest paralelo**: cuando el engine soporte concurrencia segura, paralelizar candidatos. Reduce ~3× el tiempo total.
- **Backtest incremental**: si solo cambió un campo, ¿podemos reusar parte del cómputo? Probablemente no por como está el engine. Revisar si el engine evoluciona.
- **Cache compartido entre agentes**: hoy el cache es per-agent. Si hay múltiples agentes que supervisan los mismos controllers, conviene compartir.
- **Lock global revision**: una vez en producción, revisar si el lock es estrictamente necesario.

## Próximos pasos

1. **Fase 3** — Modo `propose`: cómo se presenta el SELECTED PATCH al humano via Telegram.
2. **Fase 4** — MCP tool `update_controller_config`.
3. (Implementación, Fase 5) — `condor/agent_framework/backtest_loop.py`.
