# Routine `controller_performance` — spec completa

> Performance del controller cruzada con datos del exchange. Reemplaza a `volatility_metrics` (D10).
> Última actualización: 2026-05-09

## Propósito

Tres usos identificados:

1. **Disparador del backtest evidence loop (D9)**: la routine entrega los números reales que dicen "estamos en período subóptimo".
2. **Métrica de éxito del agente** (modo `shadow`): permite evaluar después de N días si las propuestas del agente hubieran mejorado las cosas.
3. **Market share**: métrica que el usuario quiere perseguir explícitamente — no solo PnL.

## Filosofía de diseño

`controller_performance` es **pura performance** (no es mercado, no es capital). Cruza estado del controller (PnL, fills, executors) con datos del exchange (volumen total) cuando hace falta.

**Diferencia con otras routines del framework**:
- `capital_state` → estado de capital (nominal, comprometido, oversub).
- `market_regime` → estado del mercado (régimen, vol, S/R).
- `controller_performance` → estado del controller en términos de cómo está rindiendo.

Las tres son ortogonales. Se invocan en cada tick del agente.

## Ventanas de cálculo (Q1: opción b)

**3 ventanas en paralelo**:
- `last_1h` — reciente, sensible al cambio.
- `last_6h` — corto-medio.
- `last_24h` — referencia.

**Por qué múltiples**: comparar `last_1h` vs `last_6h` vs `last_24h` es lo que dispara intuiciones tipo *"el bot venía bien y se cagó hace 2 horas"*. Una sola ventana esconde esa señal.

**Caveat**: si el controller arrancó hace <24h, las ventanas largas tienen pocos datos. Cada ventana lleva un flag `samples` y `available`.

## Métricas

### Bloque 1 — Performance bruta (de `bot_orchestration.get_active_bots_status`)

Por cada ventana:

| Métrica | Cómo se calcula |
|---|---|
| `realized_pnl_quote` | `controller.performance.realized_pnl_quote` (snapshot, no diferencia) |
| `unrealized_pnl_quote` | `controller.performance.unrealized_pnl_quote` |
| `net_pnl_quote` | `realized + unrealized` |
| `volume_traded` | `controller.performance.volume_traded` |
| `total_positions` | `controller.performance.total_positions` |
| `accuracy` | `controller.performance.accuracy` |
| `close_type_counts` | `controller.performance.close_type_counts` (TP / refresh / time_limit / etc.) |

**Importante**: estos son **snapshots acumulados** desde el inicio del controller — no por ventana. Para tener el delta por ventana necesitamos snapshots persistidos.

### Bloque 2 — Derivadas temporales (requieren histórico persistido)

Por cada ventana:

| Métrica | Cómo se calcula |
|---|---|
| `pnl_velocity` | `(net_pnl_now − net_pnl_at_window_start) / window_hours` → USD/hora |
| `pnl_velocity_normalized` | `pnl_velocity / nominal_budget_usd` → ratio comparable entre controllers |
| `volume_velocity` | `(volume_now − volume_at_window_start) / window_hours` → USD/hora |
| `fills_per_hour` | `(positions_now − positions_at_window_start) / window_hours` |
| `time_since_last_fill_minutes` | minutos desde el último incremento de `total_positions` |

**Cómputo**: leer el snapshot persistido más cercano a `now − window`, comparar con el actual.

### Bloque 3 — Cruzadas con mercado (Q2: opción a)

Solo en `last_24h` (las 1h/6h ya quedan capturadas en pnl_velocity y vol_velocity):

| Métrica | Cómo se calcula |
|---|---|
| `market_share_24h` | `volume_traded_24h_controller / volume_24h_par` |
| `gross_spread_captured_avg` | promedio de `(price_sell − price_buy) / mid_price` entre fills consecutivos del controller |
| `adverse_fill_ratio` | de los últimos N fills, qué fracción tuvo movimiento adverso del precio en los siguientes M minutos |

**Cómo se obtiene el volumen del par**:
- `client.market_data.get_klines(connector, trading_pair, interval="1h", limit=24)`.
- Sumar el `volume` de las 24 velas en quote currency.

**Cómo se obtiene `gross_spread_captured`**:
- Iterar `client.executors.search_executors(controller_ids=[...], status="completed")` filtrado por timestamp.
- Para cada par buy→sell consecutivo, calcular `(sell.price − buy.price) / mid`.
- Promediar.

**Caveat**: `adverse_fill_ratio` requiere data de price posterior a cada fill. Para MVP queda **opcional** — si requiere demasiado costo de cómputo lo dejamos out y vemos si hace falta.

### Bloque 4 — Indicadores compuestos (input directo para D9)

Estos son los que el agente consume para decidir "¿estamos en período subóptimo?":

| Métrica | Cómo se calcula | Para qué |
|---|---|---|
| `is_pnl_flat` | `abs(pnl_velocity_normalized_4h) < 0.001` (ratio del nominal por hora) | Señal de estancamiento |
| `is_volume_dropping` | `volume_velocity_1h < 0.5 × volume_velocity_24h` | Señal de degradación |
| `is_stuck` | `time_since_last_fill_minutes > 60 AND volume_velocity_1h < 10% × volume_velocity_24h` | Controller atrapado |
| `suboptimal_period_minutes` | tiempo continuo en el que `(is_pnl_flat OR is_volume_dropping OR is_stuck)` es `True` | Disparador de D9 |

## Disparador de período subóptimo (Q3 confirmado + F3 cold-start)

**Decisión confirmada por el usuario**, ahora con manejo explícito de cold-start (F3):

```python
def is_suboptimal(perf, regime):
    # F3 — Si el agente está en cold-start (sin historia suficiente), no decidir
    warmup = perf.diagnostic.warmup_status
    if warmup == "cold_start":
        return False
    
    # En modo "adaptive" usamos la ventana efectiva disponible (puede ser <4h)
    # En modo "normal" siempre hay 4h+ de historia disponible
    pnl_velocity = perf.windows[perf.diagnostic.effective_window_key].pnl_velocity
    
    return (
        pnl_velocity <= 0
        and perf.windows.last_1h.volume_velocity < 0.5 * perf.windows.last_24h.volume_velocity
        and regime.favorability != "optimal"
        and perf.diagnostic.suboptimal_period_minutes >= 30
    )
```

**Componentes**:
- **PnL flat o negativo** sobre la ventana efectiva (4h en normal, menor en adaptive).
- **Volumen reciente < 50% del promedio diario**.
- **Régimen no es óptimo** (confirmación de market_regime).
- **Persistencia ≥ 30 min** (no actuar por un evento puntual).
- **No estar en cold-start** (F3).

**Caveat**: thresholds afinables. Después de N días de operación real, los percentiles reales pueden mostrar que `0.5×` debería ser `0.7×` o `0.3×`. Documentado para revisión.

## Cold-start handling (F3)

El framework necesita ≥4h de historia persistida para calcular `pnl_velocity_4h`. La primera vez que el agente se prende, no hay historia. Política:

```python
def get_effective_velocity_window(history_path: Path, now: datetime) -> tuple[float, str]:
    """
    Returns (window_hours, status):
      - "cold_start": <30 min de historia → no proponer (window=0)
      - "adaptive":   30 min – 4h → usar ventana disponible (window=age_hours)
      - "normal":     ≥4h → ventana estándar (window=4.0)
    """
    samples = read_jsonl_all(history_path)
    if len(samples) < 3:
        return 0.0, "cold_start"
    
    oldest_ts = parse_ts(samples[0]["ts"])
    age_hours = (now - oldest_ts).total_seconds() / 3600
    
    if age_hours < 0.5:
        return 0.0, "cold_start"
    elif age_hours < 4.0:
        return age_hours, "adaptive"
    else:
        return 4.0, "normal"
```

### Comportamiento por estado

| Estado | Historia disponible | El agente puede proponer? |
|---|---|---|
| `cold_start` | <30 min | **No** — registra snapshot pero `is_suboptimal` siempre retorna False |
| `adaptive` | 30 min – 4h | **Sí**, usando ventana adaptativa (la mayor disponible) |
| `normal` | ≥4h | **Sí**, comportamiento estándar |

### Output del diagnóstico

El campo `diagnostic` del output (ver Schema más abajo) incluye:

```json
{
  "diagnostic": {
    "suboptimal_now": false,
    "warmup_status": "adaptive",
    "effective_window_key": "last_1h",
    "effective_window_hours": 1.5,
    ...
  }
}
```

El agente loggea explícitamente cuando un tick fue skipeado por cold-start (entrada en audit_log con `action: no_action, reason: cold_start`).

## Schema JSON del output

```json
{
  "ts": "2026-05-09T14:32:18Z",
  "controller_id": "001_pmm_binance_BTC-USDT",
  "bot_name": "pmm-btc-1",
  "trading_pair": "BTC-USDT",

  "windows": {
    "last_1h": {
      "samples": 6,
      "available": true,
      "realized_pnl_quote": 0.42,
      "unrealized_pnl_quote": -0.18,
      "net_pnl_quote": 0.24,
      "volume_traded": 1247.3,
      "total_positions": 18,
      "accuracy": 0.83,
      "pnl_velocity": 0.24,
      "pnl_velocity_normalized": 0.008,
      "volume_velocity": 1247.3,
      "fills_per_hour": 18.0,
      "time_since_last_fill_minutes": 2.3
    },
    "last_6h": {
      "samples": 36,
      "available": true,
      "realized_pnl_quote": 1.85,
      "unrealized_pnl_quote": -0.18,
      "net_pnl_quote": 1.67,
      "volume_traded": 7820.0,
      "total_positions": 89,
      "accuracy": 0.87,
      "pnl_velocity": 0.27,
      "pnl_velocity_normalized": 0.009,
      "volume_velocity": 1303.3,
      "fills_per_hour": 14.8,
      "time_since_last_fill_minutes": 2.3
    },
    "last_24h": {
      "samples": 144,
      "available": true,
      "realized_pnl_quote": 7.42,
      "unrealized_pnl_quote": -0.18,
      "net_pnl_quote": 7.24,
      "volume_traded": 31200.0,
      "total_positions": 412,
      "accuracy": 0.85,
      "pnl_velocity": 0.30,
      "pnl_velocity_normalized": 0.010,
      "volume_velocity": 1300.0,
      "fills_per_hour": 17.2,
      "time_since_last_fill_minutes": 2.3
    }
  },

  "market_cross": {
    "market_share_24h": 0.0024,
    "exchange_volume_24h_quote": 13000000.0,
    "controller_volume_24h_quote": 31200.0,
    "gross_spread_captured_avg": 0.00041,
    "adverse_fill_ratio": null
  },

  "diagnostic": {
    "is_pnl_flat": false,
    "is_volume_dropping": false,
    "is_stuck": false,
    "suboptimal_period_minutes": 0,
    "suboptimal_now": false,
    "warmup_status": "normal",
    "effective_window_key": "last_4h",
    "effective_window_hours": 4.0,
    "close_types_dominant": "take_profit"
  },

  "snapshot_for_history": {
    "ts": "2026-05-09T14:32:18Z",
    "realized_pnl_quote": 7.42,
    "unrealized_pnl_quote": -0.18,
    "volume_traded": 31200.0,
    "total_positions": 412
  }
}
```

### Notas del schema

- **`snapshot_for_history`** es lo que la routine persiste en disco (campos mínimos para reconstruir velocidades).
- **`adverse_fill_ratio: null`** = pendiente de implementar (opcional MVP).
- **`suboptimal_now`** = el booleano final que el agente lee para decidir si arrancar D9.
- **`close_types_dominant`** = qué tipo de cierre predomina (TP / time_limit / refresh / etc.) — útil para el LLM cuando razona qué tocar.

## Inputs (Pydantic Config)

```python
class Config(BaseModel):
    """Performance metrics per controller, with multi-window comparison."""
    bot_name: str = Field(default="", description="Bot to inspect (empty = all active)")
    controller_ids: list[str] = Field(
        default=[],
        description="Filter to specific controller IDs (empty = all in bot)",
    )
    
    # Ventanas (afinables si en el futuro queremos otras)
    windows_hours: list[float] = Field(default=[1.0, 6.0, 24.0])
    
    # Cómputos opcionales (caros)
    compute_market_share: bool = Field(default=True)
    compute_adverse_fill_ratio: bool = Field(default=False)
    
    # Thresholds del disparador subóptimo
    pnl_flat_threshold: float = Field(default=0.001)
    volume_drop_ratio: float = Field(default=0.5)
    stuck_minutes_threshold: float = Field(default=60.0)
    suboptimal_min_persistence_minutes: float = Field(default=30.0)
```

## Persistencia (Q4 confirmado)

**Decisión confirmada**: JSONL append-only por controller, snapshot cada tick (10 min), lookback 24h.

### Path

```
trading_agents/<agent>/state/controller_performance/<controller_id>.jsonl
```

### Schema de cada línea

```json
{"ts":"2026-05-09T14:32:18Z","r_pnl":7.42,"u_pnl":-0.18,"vol":31200.0,"pos":412}
```

Solo lo necesario para reconstruir velocidades. Otras métricas se recomputan en cada call.

### Cómputo de derivadas temporales

```python
def compute_velocities(history_path: Path, current_snapshot: dict, window_hours: float):
    cutoff = datetime.now(UTC) - timedelta(hours=window_hours)
    # Buscar el snapshot más cercano a cutoff (puede no haber uno exacto)
    target = find_closest_snapshot_before(history_path, cutoff)
    if target is None:
        return {"available": False, "reason": "insufficient_history"}
    
    elapsed_h = (current.ts - target.ts).total_seconds() / 3600
    return {
        "pnl_velocity": (current.r_pnl + current.u_pnl - target.r_pnl - target.u_pnl) / elapsed_h,
        "volume_velocity": (current.vol - target.vol) / elapsed_h,
        "fills_per_hour": (current.pos - target.pos) / elapsed_h,
        "samples": count_samples_between(history_path, target.ts, current.ts),
        "available": True,
    }
```

### Rotación

Truncar a 30 días (igual que `capital_state`). 144 samples/día/controller × 30 días ≈ 4.3k líneas, ~500 KB. Despreciable.

## Cómputo (pseudocódigo)

```python
async def run(config, context):
    client = await get_client(...)
    
    # 1. Snapshot actual del controller (en paralelo: status + klines del par)
    bots_data, klines = await asyncio.gather(
        client.bot_orchestration.get_active_bots_status(),
        client.market_data.get_klines(connector, pair, interval="1h", limit=24)
            if config.compute_market_share else asyncio.sleep(0)
    )
    
    # 2. Por cada controller objetivo:
    for ctrl in target_controllers(bots_data, config):
        # 3. Construir snapshot actual
        current = build_snapshot(ctrl)
        
        # 4. Para cada ventana, calcular derivadas con histórico
        windows = {}
        for hours in config.windows_hours:
            windows[f"last_{int(hours)}h"] = compute_velocities(
                state_path / f"{ctrl.id}.jsonl",
                current,
                hours
            )
        
        # 5. Cross con mercado
        market_cross = compute_market_cross(ctrl, klines) if config.compute_market_share else None
        
        # 6. Diagnóstico
        diag = diagnose(windows, market_cross, config)
        
        # 7. Persistir snapshot mínimo (para próximos cálculos)
        append_snapshot(state_path / f"{ctrl.id}.jsonl", current)
        
        # 8. Build output
        results.append(build_output(ctrl, windows, market_cross, diag, current))
    
    return results
```

## Performance estimada

- `get_active_bots_status` → ~200ms.
- `get_klines` (24 velas) → ~300ms.
- Cómputo por controller (lectura JSONL + derivadas) → <50ms.
- Total para 5 controllers: **<1s**.

Trivial.

## Caveats explícitos

1. **Las ventanas largas (24h) no son confiables si el controller arrancó hace <24h**. Flag `available` y `samples` lo capturan.
2. **`is_stuck` y `time_since_last_fill_minutes`** dependen de que `total_positions` se incremente cuando hay fills — verificar que sea así en la API. Si la API solo cuenta posiciones cerradas, hay que usar `total_executors` o llamar a `search_executors`.
3. **Volumen del exchange** viene de `get_klines`. Si la API no lo expone para el connector específico (revisar caso por caso), `market_share` queda en `null`.
4. **`gross_spread_captured`** requiere iterar executors completados — costoso si hay muchos. Considerar cache o cómputo incremental.
5. **`adverse_fill_ratio`** queda **fuera del MVP** — requiere data de precio posterior a cada fill, costoso de computar bien. Se evalúa después.

## Pendientes (revisar más adelante)

- **Adverse fill ratio**: implementación correcta y eficiente.
- **Comparativo entre controllers** (ranking de quién va mejor): útil para rebalanceo cross-controller futuro.
- **Performance por nivel** (level_id en ejecutors): qué nivel funciona mejor, cuál peor.
- **PnL attribution**: descomponer PnL en spread capture vs inventory PnL — útil para diagnóstico fino.
- **Tunear thresholds del subóptimo** después de N días de operación.

## Próximos pasos

1. Implementar `routines/controller_performance.py` siguiendo el patrón de `pmm_mister_supervisor.py`.
2. Verificar que `total_positions` cuenta lo que esperamos (ver caveat 2).
3. Decidir cómo invocar la routine: por controller o por bot. Mi voto: por bot (un solo call al status), filtra en el cómputo.
