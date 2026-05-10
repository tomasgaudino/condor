# Non-MVP Routines — catálogo

> Routines identificadas como útiles pero **fuera del scope del MVP**. Quedan capturadas acá para no perderlas.
> Cuando una de estas se promueva a MVP, se mueve a un `*_SPEC.md` dedicado.

## Filtros aplicados

Estas routines se descartaron del MVP por una de tres razones:

- **Cubierta por otra routine MVP** (ej: `pnl_velocity` ya vive en `controller_performance`).
- **Útil pero no crítica para la rule MVP** (D5: "lateral + std dev grande + NATR > threshold → subir take_profit").
- **Requiere infraestructura que aún no tenemos** (ej: orderbook tick data).

## Routines descartadas por solapamiento

### `pnl_velocity` ❌ → cubierta por `controller_performance`

Ya implementada como métrica derivada multi-ventana en `CONTROLLER_PERFORMANCE_SPEC.md`. **No se mantiene como routine separada.**

### `volatility_metrics` ❌ → cubierta por `market_regime`

Descartada en D10. **No se mantiene como routine separada.**

---

## Routines no-MVP que quedan vivas

### 1. `inventory_drift`

**Qué mide**: cuánto y por cuánto tiempo el `current_base_pct` del controller está fuera del target.

**Por qué es útil**:
- El controller hace **hard cutoff** cuando `current_base_pct` sale de `[min_base_pct, max_base_pct]` (solo opera un lado). Esto puede dejar al bot atrapado.
- `controller_performance` no mira inventory, solo PnL/volumen.
- Detectar drift sostenido es la señal natural para mover `target_base_pct` o reducir `portfolio_allocation`.

**Output candidato**:
```json
{
  "current_base_pct": 0.32,
  "target_base_pct": 0.5,
  "drift_pct": -0.18,
  "in_range": true,
  "drift_persistence_minutes": 145,
  "side_of_drift": "below_target",
  "approaching_cutoff": false,
  "minutes_outside_range_24h": 0
}
```

**Cuando promover a MVP**: cuando aparezca una rule del agente que toque `target_base_pct` o `min/max_base_pct`.

---

### 2. `spread_efficiency`

**Qué mide**: relación entre los spreads configurados del controller y los spreads reales del exchange + fill rate efectiva.

**Por qué es útil**:
- Si tus spreads están dentro del spread del orderbook → te frontrunean otros makers.
- Si tus spreads están demasiado lejos → no llenan, capital ocioso.
- Es una señal de microestructura que `market_regime` no captura (porque trabaja con candles, no con orderbook tick data).

**Output candidato**:
```json
{
  "configured_buy_spread_min": 0.0002,
  "configured_sell_spread_min": 0.0002,
  "exchange_orderbook_spread": 0.00018,
  "is_inside_book": true,
  "fills_last_hour": 14,
  "expected_fills_at_this_vol": 22,
  "fill_efficiency_ratio": 0.64,
  "diagnostic": "fills_underperforming_for_volatility"
}
```

**Cuando promover a MVP**: cuando se quiera tunear spreads automáticamente o cuando se vea que el agente decide mal porque no ve microestructura.

**Bloqueante actual**: requiere acceso a orderbook tick data o snapshots del orderbook. Hay que confirmar si la API de Hummingbot lo expone fácilmente.

---

### 3. `price_zone`

**Qué mide**: dónde está el precio actual respecto al `breakeven_price` del inventory acumulado.

**Por qué es útil**:
- Si el precio está muy debajo del breakeven, vender genera pérdida realizada.
- Si está muy encima, comprar significa estar pagando caro.
- Combinado con `position_profit_protection` del controller, esto define qué lado tiene sentido operar.

**Output candidato**:
```json
{
  "current_price": 99250,
  "breakeven_price": 99850,
  "distance_to_breakeven_pct": -0.006,
  "zone": "below_breakeven",
  "unrealized_pnl_pct": -0.006,
  "minutes_in_current_zone": 47,
  "position_profit_protection_active": true,
  "implied_constraint": "selling_blocked_by_profit_protection"
}
```

**Cuando promover a MVP**: cuando aparezca una rule que considere "no vender en pérdida" o cuando se quiera reportar el efecto de `position_profit_protection` al humano.

**Sinergia**: complementaria con `controller_performance` (que mira PnL bruto) y `inventory_drift` (que mira ratio). `price_zone` cierra la triada.

---

### 4. `controller_health`

**Qué mide**: snapshot agregado de las métricas internas del controller (`processed_data`) que vimos en `CONTROLLER_DEEP_DIVE.md`.

**Por qué es útil**:
- El controller expone 12+ métricas internas por tick (cooldown_status, price_distance_analysis, level_conditions, executor_stats, refresh_tracking, etc.).
- Para debug/diagnóstico, tener un snapshot estructurado es más útil que pedir cada una por separado.
- Útil cuando una propuesta del agente es rechazada y se quiere entender por qué.

**Cuando promover a MVP**: cuando el agente tenga que diagnosticar fallas operativas (un controller que no coloca, fills atascados, etc.).

**Status**: posiblemente reemplazable por exponer `processed_data` directamente desde una MCP tool en lugar de ser una routine. Decidir cuando llegue el momento.

---

### 5. `market_microstructure` (idea agregada)

**Qué mide**: orderbook snapshot + order flow imbalance (OFI).

**Por qué es útil**:
- Indicador rápido de presión compradora/vendedora.
- Predictor short-term de dirección.
- Útil para timing fino de entradas (modular spreads dinámicos).

**Bloqueante actual**: requiere stream de orderbook ticks. No está claro que la API de Hummingbot lo exponga sin abrir una conexión websocket dedicada.

**Cuando promover a MVP**: probablemente nunca para este framework — es más territorio de un controller HFT que de un PMM con configs adaptativas.

---

### 6. `reconcile_inventory` (ya identificada en backlog)

**Qué mide**: comparar `current_base_pct` declarado del controller vs el real de la wallet.

**Origen**: surgió de la conversación sobre `initial_positions` (ver sección "Bootstrapping & Reconciliación" en `CAPITAL_DYNAMICS.md`).

**Cuando promover a MVP**: junto con el wizard de bootstrapping. Hoy `initial_positions` arranca vacío siempre — cuando se arregle ese gap, esta routine se vuelve crítica para detectar drift entre lo declarado y la realidad.

---

## Resumen — qué entra al MVP y qué no

| Routine | Estado | Razón |
|---|---|---|
| `capital_state` | ✅ MVP | Prerequisito de cualquier rule de sizing |
| `market_regime` | ✅ MVP | Input directo de la rule MVP (D5) |
| `controller_performance` | ✅ MVP | Disparador de D9, métrica de éxito en shadow |
| `pnl_velocity` | ❌ descartada | Cubierta por `controller_performance` |
| `volatility_metrics` | ❌ descartada (D10) | Cubierta por `market_regime` |
| `inventory_drift` | 🔮 non-MVP | Útil cuando rule toque inventory targets |
| `spread_efficiency` | 🔮 non-MVP | Útil pero requiere orderbook data |
| `price_zone` | 🔮 non-MVP | Útil para rules que respeten breakeven |
| `controller_health` | 🔮 non-MVP | Útil para debug, posiblemente como MCP tool |
| `market_microstructure` | 🔮 non-MVP | Bloqueante por infra orderbook |
| `reconcile_inventory` | 🔮 non-MVP | Junto con wizard de bootstrapping |

## Promoción de routines

Cuando una routine non-MVP se promueva a MVP:

1. Crear `<NOMBRE>_SPEC.md` con la spec completa siguiendo el patrón de las MVP.
2. Mover la entrada de este doc a la spec.
3. Agregar tarea en `TASKS.md` (Fase 5 implementación).
4. Documentar la decisión de promoción en `DECISIONS.md`.
