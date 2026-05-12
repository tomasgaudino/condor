# Routine `market_regime` — spec completa

> Routine de mercado puro (sin estado del controller). Multi-timeframe, cada timeframe con propósito distinto.
> Última actualización: 2026-05-09

## Propósito

Clasificar el régimen de mercado actual en una etiqueta canónica + favorabilidad para PMM, considerando 3 horizontes temporales. Su output es input principal de:
- El **disparador** del backtest evidence loop (¿estamos en zona desfavorable?).
- La **decisión semántica** del LLM (qué dimensión tocar).

## Filosofía de diseño

Un PMM gana plata de **dos fuentes**:
1. **Spread capture** — provee liquidez, cobra spread. Materializa con rotación.
2. **Inventory PnL** — exposición direccional accidental. Va con la tendencia.

Estas fuentes tienen **tensión**:
- **Mean-reverting + vol moderada** = zona ideal (alta rotación, exposición direccional ≈ 0).
- **Trending** = inventory atrapado, fills asimétricos.
- **Vol baja** = capital ocioso.
- **Vol alta** = riesgo de slippage adverso.

`market_regime` clasifica donde está el mercado en este espacio.

## Timeframes y propósitos

### 🔬 Micro — `5m` · "¿cómo está la operación AHORA?"

Calibra parámetros que reaccionan al ruido del momento (spreads, take_profit).

- **Time horizon**: últimas 1-2 horas (de 14 a 30 velas).
- **Pregunta**: ¿hay vol suficiente? ¿precio oscilando o caminando?

### 📊 Meso — `1h` · "¿en qué zona del mercado estamos?"

Calibra decisiones estructurales (allocation, target_base_pct, niveles técnicos).

- **Time horizon**: últimos 2-7 días (50-168 velas).
- **Pregunta**: ¿tendencia clara del último día? ¿cerca de S/R?

### 🌍 Macro — `1d` · "¿cómo está el contexto general?"

Background. No dispara acciones directas pero contextualiza.

- **Time horizon**: últimos 30-90 días.
- **Pregunta**: ¿día anormal vs últimos 30? ¿posición en rango histórico?

## Indicadores por timeframe

### Micro (5m)

| Indicador | Fórmula / período | Para qué |
|---|---|---|
| `natr_14` | Normalized ATR(14) | Calibra spreads y take_profit |
| `bb_width_20` | (BB_upper − BB_lower) / SMA(20) | Cambios rápidos de volatilidad |
| `ema_slope_20` | (EMA(20) − EMA(20)[-10]) / EMA(20)[-10] | Drift de corto |
| `adx_14` | Average Directional Index(14) | Fuerza local de tendencia |
| `realized_vol_60` | std(log returns, 60 períodos) × √(periods/year) | Vol anualizada de las últimas ~5h |

### Meso (1h)

| Indicador | Fórmula / período | Para qué |
|---|---|---|
| `hurst_100` | Hurst exponent sobre 100 períodos | Trending vs mean-reverting estructural |
| `ema_slope_50` | (EMA(50) − EMA(50)[-25]) / EMA(50)[-25] | Tendencia de medio plazo |
| `atr_50` | ATR(50) | Volatilidad estructural |
| `support_resistance` | Pivot detection + clustering, ver abajo | Niveles técnicos cercanos |
| `linreg_slope_100` | Pendiente de regresión lineal sobre 100 períodos | Direccionalidad cuantificada |

### Macro (1d)

| Indicador | Fórmula / período | Para qué |
|---|---|---|
| `volume_today_vs_avg30` | volume(hoy) / mean(volume últimos 30d) | Día anormal por volumen |
| `daily_range_pct` | (high − low) / open | Expansión/compresión del día |
| `volatility_30d_annual` | std(daily log returns, 30) × √365 | Benchmark macro de vol |
| `position_in_range_30d` | (price − low_30d) / (high_30d − low_30d) | Estamos en zona de techo/medio/piso |
| `levels_macro` | high/low de 7d, 30d, 90d | Niveles macro |

## Detección de Soportes/Resistencias (en 1h)

Algoritmo simple para MVP, mejorable después:

```python
def detect_support_resistance(candles_1h, window=168, k=3, cluster_tol=0.003):
    """
    candles_1h: últimas 168 velas (7 días)
    k: una vela es pivot si su high/low supera al de las k velas a cada lado
    cluster_tol: pivots dentro de 0.3% se agrupan en un mismo nivel
    """
    pivots_high = []
    pivots_low = []
    for i in range(k, len(candles_1h) - k):
        window_slice = candles_1h[i-k:i+k+1]
        if candles_1h[i].high == max(c.high for c in window_slice):
            pivots_high.append(candles_1h[i].high)
        if candles_1h[i].low == min(c.low for c in window_slice):
            pivots_low.append(candles_1h[i].low)
    
    # Cluster pivots cercanos
    levels = cluster_levels(pivots_high + pivots_low, tol=cluster_tol)
    
    # Filtrar: nivel válido = al menos 2 toques
    levels = [lv for lv in levels if lv.count >= 2]
    
    # Devolver el S y el R más cercanos al precio actual
    current = candles_1h[-1].close
    nearest_support    = max((lv for lv in levels if lv.price < current), key=lambda lv: lv.price, default=None)
    nearest_resistance = min((lv for lv in levels if lv.price > current), key=lambda lv: lv.price, default=None)
    
    return {
        "support": nearest_support,
        "resistance": nearest_resistance,
    }
```

**Caveats explícitos**:
- No considera volumen en los pivots (un pivot con mucho volumen es más importante).
- No detecta breakouts (cuando un nivel se rompe, ya no es válido).
- Si el algoritmo resulta inútil en la práctica, se reemplaza por volume profile o point-of-control.

## Clasificación local (por timeframe)

Cada timeframe produce **2 clasificaciones independientes** + sus métricas crudas:

### Direccionalidad (3 estados)

```python
def classify_directionality(indicators):
    if hurst >= 0.55 or adx >= 25:
        return "trending_up" if ema_slope > 0 else "trending_down"
    elif hurst <= 0.45:
        return "mean_reverting"
    else:
        return "random_walk"
```

**Umbrales default** (afinables):
- `hurst_trending`: 0.55
- `hurst_mean_rev`: 0.45
- `adx_trending`: 25

### Volatilidad (3 niveles)

```python
def classify_volatility(natr, natr_history_24h):
    p33 = percentile(natr_history_24h, 33)
    p67 = percentile(natr_history_24h, 67)
    if natr < p33: return "low"
    elif natr > p67: return "high"
    else: return "moderate"
```

**Umbrales relativos**, no absolutos. Lo que es "alta vol" para BTC-USDT no lo es para SHIB-USDT. Se calcula contra el propio histórico de 24h del par.

**Caveat**: requiere histórico — los primeros ticks devuelven `null` o usan defaults absolutos hasta tener data.

## Régimen canónico (composición de timeframes)

Esta es la pieza que dispara las rules.

### Direccionalidad canónica

Mayoría simple (consenso) entre los 3 timeframes, **excepto excepciones**:

```python
def canonical_directionality(micro, meso, macro):
    # Excepción: trending con pullback
    if macro in ("trending_up", "trending_down") and \
       meso in ("mean_reverting", "random_walk", opposite(macro)) and \
       micro in ("mean_reverting", "random_walk"):
        return f"trending_{direction(macro)}_with_pullback"
    
    # Consenso simple
    votes = [micro, meso, macro]
    return mode(votes)
```

**Por qué la excepción**: si solo mirás el 5m, decís "lateral, perfecto para PMM" — pero estás contra la corriente macro. El pullback va a terminar y el precio va a saltar. **Capturar este escenario es uno de los lugares donde el agente agrega valor real al humano.**

### Volatilidad canónica

Toma la del **5m** (es la que afecta la operación inmediata). Las otras quedan en sus respectivos bloques.

### Confianza

Cuántos timeframes coinciden en direccionalidad simple:
- **alta**: los 3 coinciden.
- **media**: 2 coinciden.
- **baja**: los 3 difieren.

Excepción de pullback se reporta como **media** (ya hay disonancia explícita).

### Persistencia

Tiempo en minutos desde la última transición de régimen canónico. Requiere histórico — el agente persiste régimen anterior en su `state/`.

## Favorabilidad para PMM (matriz)

| | **Vol baja** | **Vol moderada** | **Vol alta** |
|---|---|---|---|
| **Mean-reverting** | 🟡 ocioso | 🟢 **ZONA IDEAL** | 🟡 slippage compensa spread |
| **Random walk** | 🟡 pocos fills | 🟢 buena | 🔴 riesgoso |
| **Trending suave** | 🔴 inventory atrapado | 🔴 inventory atrapado | 🔴 inventory atrapado |
| **Trending fuerte** | 🔴 cobertura unilateral | 🔴 pérdidas inventory | 🔴 pérdidas grandes |
| **Trending + pullback** | n/a | 🟡 oportunidad con bias | 🟡 con cuidado |

Niveles canónicos:
- 🟢 `optimal`
- 🟡 `suboptimal`
- 🔴 `adverse`

## Mapeo de regímenes a dimensiones de acción

Esto es lo que el LLM lee para elegir qué tocar. No le decimos los valores — solo qué dimensión tiene sentido en cada caso. El expansor determinístico de D9 después genera los valores para backtestear.

| Régimen | Dimensiones candidatas | Dirección esperada | Por qué |
|---|---|---|---|
| `mean_rev_low_vol` | spreads | comprimir | Pocos fills → acercar quotes al mid |
| `mean_rev_high_vol` | take_profit, spreads | ambos subir | Oscilaciones grandes → capturar más rebote, entradas más lejanas (rule MVP D5) |
| `trending_*` | target_base_pct, portfolio_allocation | mover con tendencia / reducir | Inventory neutral → no sumar exposición direccional |
| `trending_*_with_pullback` | buy_amounts_pct, sell_amounts_pct | sesgar a favor de macro | Operar con bias hacia el macro |
| `random_walk_high_vol` | max_active_executors_by_level | reducir | Frenar acumulación en condiciones inciertas |
| `mean_rev_moderate` (zona ideal) | _ninguna_ | _no_action_ | Si funciona no lo toques |

**Nota**: el LLM no está atado a esta tabla. Si su razonamiento (sumando policy del usuario + estado actual) sugiere otra dimensión, puede proponerla — pero debe pasar por los invariants. La tabla es una **guía a priori**.

## Schema JSON del output

```json
{
  "ts": "2026-05-09T14:32:18Z",
  "trading_pair": "BTC-USDT",
  "connector": "binance",

  "summary": {
    "canonical_regime": "mean_reverting_high_vol",
    "favorability": "suboptimal",
    "confidence": "high",
    "persistence_minutes": 47,
    "bias": null,
    "trigger_candidates": ["take_profit", "spreads"]
  },

  "micro_5m": {
    "directionality": "mean_reverting",
    "volatility": "high",
    "indicators": {
      "natr_14": 0.0142,
      "bb_width_20": 0.0185,
      "ema_slope_20": -0.0003,
      "adx_14": 18.2,
      "realized_vol_60_annual": 0.62
    },
    "vol_history_24h": {
      "p33": 0.0058,
      "p67": 0.0098,
      "samples": 287
    }
  },

  "meso_1h": {
    "directionality": "mean_reverting",
    "volatility": "moderate",
    "indicators": {
      "hurst_100": 0.42,
      "ema_slope_50": 0.0012,
      "atr_50": 0.0089,
      "linreg_slope_100": 0.00015
    },
    "support_resistance": {
      "support": {"price": 98400, "distance_pct": -0.006, "touches": 3},
      "resistance": {"price": 99800, "distance_pct": 0.008, "touches": 4}
    }
  },

  "macro_1d": {
    "directionality": "trending_up",
    "volatility": "moderate",
    "indicators": {
      "volume_today_vs_avg30": 1.12,
      "daily_range_pct": 0.018,
      "volatility_30d_annual": 0.48,
      "position_in_range_30d": 0.73
    },
    "levels_macro": {
      "high_7d": 100200,
      "low_7d": 96800,
      "high_30d": 102500,
      "low_30d": 92000,
      "high_90d": 102500,
      "low_90d": 84000
    }
  },

  "history_required": {
    "available": true,
    "samples_micro_24h": 287,
    "samples_meso_7d": 168,
    "samples_macro_30d": 30
  }
}
```

### Notas del schema

- `trigger_candidates`: shortcut para el agente — qué dimensiones aparecen como candidatas según la tabla de mapeo. El LLM las lee como sugerencia.
- `bias`: solo populado en regímenes con direccionalidad fuerte; lleva `"up"` o `"down"`. En regímenes con pullback, **siempre** populado.
- `history_required.available`: `false` cuando el par es nuevo y no hay datos suficientes; en ese caso, `volatility` queda en `"moderate"` por default y se marca con `confidence: "low"`.

## Inputs (Pydantic Config)

```python
class Config(BaseModel):
    """Multi-timeframe market regime classifier."""
    trading_pair: str = Field(default="BTC-USDT")
    connector_name: str = Field(default="binance")  # spot; pmm_mister no usa perps
    
    # Timeframes (configurables — defaults razonables)
    micro_interval: str = Field(default="5m")
    meso_interval: str = Field(default="1h")
    macro_interval: str = Field(default="1d")
    
    # Lookbacks
    micro_lookback_candles: int = Field(default=288)   # 24h de 5m
    meso_lookback_candles: int = Field(default=168)    # 7d de 1h
    macro_lookback_candles: int = Field(default=90)    # 90d de 1d
    
    # Umbrales (afinables)
    hurst_trending_threshold: float = Field(default=0.55)
    hurst_mean_rev_threshold: float = Field(default=0.45)
    adx_trending_threshold: float = Field(default=25)
```

## Cómputo (pseudocódigo)

```python
async def run(config, context):
    client = await get_client(...)
    
    # 1. Fetch candles en paralelo (3 timeframes)
    micro, meso, macro = await asyncio.gather(
        fetch_candles(client, config.connector_name, config.trading_pair,
                      config.micro_interval, config.micro_lookback_candles),
        fetch_candles(client, config.connector_name, config.trading_pair,
                      config.meso_interval, config.meso_lookback_candles),
        fetch_candles(client, config.connector_name, config.trading_pair,
                      config.macro_interval, config.macro_lookback_candles),
    )
    
    # 2. Calcular indicadores por timeframe
    micro_block = compute_micro(micro, config)
    meso_block  = compute_meso(meso, config)
    macro_block = compute_macro(macro, config)
    
    # 3. Clasificación local
    classify_locally(micro_block)
    classify_locally(meso_block)
    classify_locally(macro_block)
    
    # 4. Régimen canónico
    canonical = build_canonical(micro_block, meso_block, macro_block)
    
    # 5. Persistencia (lee state del agente)
    canonical["persistence_minutes"] = compute_persistence(canonical, state_path)
    
    # 6. Trigger candidates desde la tabla de mapeo
    canonical["trigger_candidates"] = lookup_action_dimensions(canonical)
    
    return {
        "summary": canonical,
        "micro_5m": micro_block,
        "meso_1h": meso_block,
        "macro_1d": macro_block,
        "history_required": availability_flags(...),
    }
```

## Performance estimada

- 3 fetches paralelos de candles = ~1-3s.
- Cómputo de indicadores (numpy/pandas-ta) sobre <300 puntos = sub-100ms.
- Total estimado: **<5s por call**.

A 10 min de cadencia del agente, esto es despreciable. El cómputo se puede mover a un thread si en el futuro se vuelve cuello de botella.

## Persistencia entre ticks

La routine es stateless en el cómputo — todos los inputs vienen del mercado y los outputs son derivables.

**Excepción**: `persistence_minutes` requiere conocer el régimen del tick anterior. Convención:
- Append-only: `trading_agents/<agent>/state/regime_history/<connector>_<pair>.jsonl`.
- Cada línea = `{"ts": ..., "regime": "mean_rev_high_vol"}`.
- Para calcular persistencia: leer hacia atrás hasta encontrar la primera entrada con régimen distinto.

## Caveats explícitos

1. **Hurst exponent y ADX dan resultados poco confiables con <100 puntos**. Si el lookback es chico (par nuevo, gap de datos), `confidence` se degrada automáticamente.
2. **Volatility classification es relativa al propio par/24h**. Comparar `natr_14` entre pares distintos requiere los percentiles del par específico.
3. **No hay protección contra outliers** (ej: un wick de 10% en 1 segundo distorsiona ATR/NATR). Mejorable con winsorización futura.
4. **Trending con pullback** se detecta solo entre micro/meso/macro consistentes. Pullbacks intra-1h no se detectan (require timeframes intermedios — fuera de MVP).

## Pendientes (revisar más adelante)

- **Volume profile / point-of-control** como reemplazo o complemento de S/R simple.
- **Multi-pair correlation**: si `BTC-USDT` está en régimen X y `ETH-USDT` también, hay confianza mayor que si solo uno.
- **Regime change detection**: alertar específicamente en transiciones (no solo reportar persistencia).
- **Tunear los thresholds** con datos reales después de N días de operación.
- **Microestructura** (orderbook spread, OFI, tick rate) — fuera del MVP, requiere fuente de datos distinta de candles.

## Shape real del response del SDK (validado 2026-05-12)

`client.market_data.get_candles(connector_name, trading_pair, interval,
max_records)` devuelve **`list[dict]`** (sin envoltorio), ordenada
ascendente por `timestamp`. Cada dict tiene:

```
timestamp (float, epoch seconds, UTC)
open, high, low, close (float)
volume                 (base asset)
quote_asset_volume     (quote / USD — usar este para volume_today_vs_avg30)
n_trades               (float — viene como float aunque sea integer)
taker_buy_base_volume
taker_buy_quote_volume
```

Connector default: **`binance` (spot)**. Los `pmm_mister` no usan
perps — confirmado por el usuario en sesión 2026-05-12.

Campos disponibles pero **fuera del MVP** (candidatos post-MVP):
- `taker_buy_quote_volume / quote_asset_volume` → taker imbalance, sirve
  para desempatar direccionalidad en regímenes ambiguos.
- `n_trades` → posible reemplazo de volume en pares ilíquidos.

## Próximos pasos

1. Implementar `routines/market_regime.py` — usar `pandas-ta` o `talib` para indicadores estándar.
2. Implementar S/R simple (algoritmo descrito).
3. Implementar persistence file (formato JSONL).
4. Probar con datos reales en `/routines` (Telegram) antes de meterlo al agente.
