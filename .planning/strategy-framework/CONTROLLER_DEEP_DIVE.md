# Deep Dive — Controller `pmm_mister`

> Fuente: `~/PycharmProjects/hummingbot/controllers/generic/pmm_mister.py` (1549 líneas).
> Análisis hecho 2026-05-09 por subagente. Referencias en formato `archivo:Lxxx`.

## Pricing por tick

- **Reference price** = `MidPrice` del market data provider (L452–454). No usa fuente externa.
- Fórmula: `price = reference * (1 + side_mult * spread_pct * spread_multiplier)` con `side_mult=-1` para BUY, `+1` para SELL.
- `tick_mode=True` → `spread_multiplier = min_price_increment / reference_price` (spreads en ticks).
- `tick_mode=False` → `spread_multiplier = 1` (spreads en pct).
- **Fallback peligroso**: si reference price inválido, usa `100` (L457, L460). Puede colocar órdenes a precios irreales.

## Skew por inventory

`current_base_pct = position.amount_quote / total_amount_quote` (L496–508).

```
buy_skew  = clamp((max_pct - current_pct) / (max_pct - min_pct), min_skew, 1.0)
sell_skew = clamp((current_pct - min_pct) / (max_pct - min_pct), min_skew, 1.0)
```

- El skew **multiplica el amount**, no el precio (L308). Más cerca de `max_base_pct` → buy_skew → 0 (con piso `min_skew`).
- `min_skew=1.0` desactiva todo el efecto de skew (todas las órdenes a tamaño nominal).

### Hard cutoffs (`get_not_active_levels_ids` L578–581)

- `current_pct < min_base_pct` → **solo BUYs**.
- `current_pct > max_base_pct` → **solo SELLs**.

## `position_profit_protection` (cuando `True`)

Dos efectos (L315–319, L583–594):

1. **No coloca SELL por debajo de `breakeven_price`** (no vende a pérdida).
2. Si `current_pct < target_pct` y `price < breakeven` → solo BUYs. Si `current_pct > target_pct` y `price > breakeven` → solo SELLs.

## Hanging executors

- Cuando un executor `is_trading=True` (entry filled, sosteniendo posición + TP), pasados `buy/sell_position_effectivization_time` segundos, emite `StopExecutorAction(keep_position=True)` (L218–228, L426–442).
- Esto **cierra el TP order** pero deja la posición. La posición se consolida en la posición agregada del bot, liberando el slot para colocar otro nivel.

## Hot-reload — qué se puede cambiar en vivo

**Mecanismo**: Pydantic field flag `json_schema_extra={"is_updatable": True}` (L24–56). El controller lee `self.config.<field>` cada tick (sin caching), entonces basta con mutar el objeto config.

### ✅ Updatables (25 campos, surten efecto en próximo tick sin restart)

`portfolio_allocation`, `target_base_pct`, `min_base_pct`, `max_base_pct`, `buy_spreads`, `sell_spreads`, `buy_amounts_pct`, `sell_amounts_pct`, `executor_refresh_time`, `buy_cooldown_time`, `sell_cooldown_time`, `buy_position_effectivization_time`, `sell_position_effectivization_time`, `price_distance_tolerance`, `refresh_tolerance`, `tolerance_scaling`, `leverage`, `take_profit`, `take_profit_order_type`, `open_order_type`, `max_active_executors_by_level`, `tick_mode`, `position_profit_protection`, `min_skew`, `global_take_profit`, `global_stop_loss`.

### ❌ NO updatables (requieren restart del controller)

`controller_name`, `controller_type`, `connector_name`, `trading_pair`, `position_mode`. El `__init__` inicializa rate sources con connector+pair (L192–194).

### ⚠️ Caveats

- **`take_profit` solo afecta executors nuevos**. Los activos retienen el TP con el que nacieron — está injectado en `triple_barrier_config` (L115–127) al crearse via `get_executor_config`. Mismo comportamiento para `take_profit_order_type` y `open_order_type`.
- **No hay hook `on_config_change`**. Los cambios se aplican via mutación directa del objeto config (`setattr` o `update_parameters`).
- Existe `PMMisterConfig.update_parameters(trade_type, new_spreads, new_amounts_pct)` (L149–159) — pero solo cubre spreads + amounts.

## Métricas internas expuestas (en `processed_data` cada tick)

Disponibles en L518–536 + sub-secciones:

- `reference_price` (mid price)
- `current_base_pct`, `position_amount`, `breakeven_price`
- `deviation = (target_position - position_quote) / target_position`
- `unrealized_pnl_pct`
- `buy_skew`, `sell_skew`
- `cooldown_status` (L1072)
- `price_distance_analysis` (L1112)
- `effectivization_tracking` (L1161)
- `level_conditions` (L1201)
- `executor_stats` (L1293)
- `refresh_tracking` (L1319)
- `price_history` (60 puntos, L196)
- `order_history` (20 entries, L199)
- Por nivel via `_analyze_by_level_id` (L602): `total_active_executors`, `open_order_last_update`, `min_price`, `max_price`, separación trading vs not_trading.

**Estos son inputs naturales para routines** del nuevo framework.

## Modos de falla (cuándo deja de tradear o queda atrapado)

1. `manual_kill_switch=True` → corta. Manejado por `ControllerBase`.
2. **Inventory atrapado fuera de min/max** (L578–581): si los spreads/cooldowns del lado permitido no producen fills, queda fuera de rango indefinidamente. **Este es exactamente el problema que motiva el framework.**
3. `position_profit_protection=True` + precio del lado adverso del breakeven (L583–594, L316–319) → solo un lado, puede combinarse con (2) y dejar sin colocar nada.
4. `max_active_executors_by_level` alcanzado → ese nivel no recoloca (L361).
5. `price_distance_tolerance` muy alto → todos los niveles violados → no coloca.
6. `min_skew` cerca de 1.0 con inventory en límite → ignora skew, puede colocar tamaño completo en el lado que se quiere reducir.
7. Reference price inválido → fallback a `100` (L457, L460). **Peligro: órdenes a precios irreales.**
8. `amount=0` post-quantization → skipea nivel (L311–313). Pasa con `portfolio_allocation` muy chico.
9. `global_take_profit` / `global_stop_loss` están en config pero **no se ve la lógica que los consume en este archivo** — probablemente los maneja `ControllerBase` o un componente externo. **TODO: verificar.**

## Implicancias para el framework LLM

### Palancas potentes (efecto inmediato)

| Palanca | Cuándo usarla | Riesgo |
|---|---|---|
| `buy_spreads` / `sell_spreads` | Volatilidad cambia, fill rate baja | Bajo si está acotado |
| `buy_amounts_pct` / `sell_amounts_pct` | Sesgar tamaño por nivel | Bajo |
| `target_base_pct` + `min/max_base_pct` | Drift persistente del inventory | Medio: cambia hard cutoffs |
| `min_skew` | Habilitar/deshabilitar el skew | Alto: deshabilitar (`=1.0`) puede empeorar el drift |
| `portfolio_allocation` | Reducir exposición en régimen adverso | Medio |
| `position_profit_protection` (bool) | Permitir vender a pérdida o no | Alto: cambio binario fuerte |

### Palancas con lag (solo afectan executors nuevos)

- `take_profit`, `take_profit_order_type`, `open_order_type` — los executors activos no heredan el cambio. Para que aplique a todo, hay que esperar al refresh natural o forzarlo.

### Palancas a evitar (requieren restart)

- `connector_name`, `trading_pair`, `position_mode`, `controller_name`, `controller_type`.

### Palancas que el framework NO debería tocar (decisión del usuario, ver DECISIONS.md)

- `manual_kill_switch` — siempre humano.
- `leverage` — sí es updatable pero es decisión de riesgo, mejor humano.

## Confirmación de Q1 (¿soporta hot-reload?)

**Sí, nativamente, a nivel Pydantic field.** No hace falta crear infra de hot-reload — solo necesitamos:
- Una API/MCP tool que mute el objeto config del controller corriendo (con validación de `is_updatable`).
- O un endpoint que reciba un patch y aplique `setattr` para cada campo updatable.

Esto simplifica mucho la implementación del framework.
