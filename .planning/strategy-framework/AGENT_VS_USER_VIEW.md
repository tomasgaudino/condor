# Agent View vs User View — contrato vivo por routine

> Documento de referencia. Cada routine del framework adaptativo emite
> dos vistas del mismo cómputo: una para el **humano** (formateada,
> narrada, con KPIs y glosario) y una para el **agente LLM** (estructura
> mínima sin decoraciones).
>
> Mantenelo actualizado cuando cambien los contratos. La función que
> construye cada vista vive en la routine correspondiente, y los tests
> de la routine son los que aseguran que el contrato no se rompe.

## Por qué dos vistas

Las dos audiencias **no necesitan lo mismo**:

| Necesidad | Humano | Agente LLM |
|---|---|---|
| Status visual rápido (verde/amarillo/rojo) | ✅ | ✗ |
| Narrativa interpretativa | ✅ | ✗ (la genera él si hace falta) |
| KPIs formateados | ✅ | ✗ (los reconstruye del payload) |
| Glosario contextual | ✅ | ✗ (ya conoce los conceptos) |
| Tabla pre-renderizada | ✅ | ✗ (lee el array crudo) |
| Datos crudos completos | ✅ (drilldown) | ✅ (obligatorio) |
| IDs canónicos cross-referenciados | ✅ | ✅ |
| Totales pre-agregados | ✅ | ✅ (evitan re-cálculo) |
| Histórico/inventario completo | ✅ (cuando aplica) | ✗ (solo si lo necesita la decisión) |

**Mezclar las dos vistas en una sola tiene costos**:

- Para el humano: ruido de campos que no entiende.
- Para el LLM: tokens gastados en KPIs duplicados, narrativas obvias,
  campos legacy. Y peor: si la narrativa es ambigua, el LLM puede
  citarla en lugar de razonar sobre los datos.

## Patrón de implementación

Por cada routine del framework, dos funciones puras:

```python
def _build_user_view(payload):
    """Returns a string / RoutineResult-friendly format for humans."""
    # KPIs, narrativa, glosario, tabla, etc.

def _build_agent_payload(payload):
    """Returns a minimal dict for the LLM. No human decorations."""
    # Solo los campos que las decisiones del agente consultan.
```

El `RoutineResult` final entrega ambas:

- **Para el humano**: `text`, `sections` con `type="kpi"`,
  `table_data`/`table_columns`, `_save_report` con narrativa + glosario.
- **Para el agente**: una sección `{"type": "data", "title": "agent",
  "data": <agent_payload>}`.

El engine del agente lee **exclusivamente** la sección `title="agent"`.
Si lee otra cosa, está leyendo info pensada para humanos y va a tomar
decisiones de baja calidad.

## Convivencia de canales

Por cada routine:

| Canal | Lee qué | Para qué |
|---|---|---|
| Telegram preview | `RoutineResult.text` (1 línea) | Estado de un vistazo en mobile |
| Web UI new (Reports) | `ReportBuilder` HTML persistido | Narrativa + KPIs + tabla + glosario |
| Web UI legacy (Instance detail) | `RoutineResult.sections` (KPI) + `table_data` | Vista interactiva post-Run |
| CLI dashboard | `sections[type="data", title="payload"]` | Vista detallada read-only |
| Engine del agente | `sections[type="data", title="agent"]` | Decisiones del LLM |
| API REST | todo lo anterior expuesto en `/api/v1/routines/instances/{id}` | Otros consumidores |

## Contratos por routine

### `capital_state` (implementada en MVP)

#### User view

Compone:
- **`text`** — 1 línea ASCII tipo `"14 ctrl · headroom $-203 · 1 stuck? · 3 alerts"`.
- **4 KPI cards** (`type="kpi"`): CONTROLLERS, CAPITAL, HEADROOM, ALERTS — cada una con `value`, `delta`, `trend` (up/down/neutral).
- **Tabla** (`table_data` + `table_columns`): una fila por controller, columnas `controller, bot, pair, committed, nominal, util%, tp_bps, pos, flag`, ordenada por util% desc.
- **Report HTML persistido** con: KPIs → narrativa → wallet → alerts → controllers table → glosario.
- **Narrativa**: 1-4 frases en castellano determinísticas, status 🟢/🟡/🔴.
- **Glosario**: 4 conceptos básicos siempre + condicionales (worst_case, oversub, severity, take_profit, stuck) según presencia en el payload.

#### Agent view

```json
{
  "ts": "2026-05-10T05:00:00Z",
  "controllers": [
    {
      "id": "bot::config_name",
      "trading_pair": "BTC-USDT",
      "base_asset": "BTC",
      "quote_asset": "USDT",
      "capital": {
        "nominal_budget_usd": 30.0,
        "committed_now_usd": 1500.0,
        "worst_case_usd": 300.0,
        "utilization_now": 50.0
      },
      "config": {
        "take_profit": 0.0001,
        "max_active_executors_by_level": 10,
        "portfolio_allocation": 0.03
      },
      "diagnostic": {
        "stuck_suspect": true,
        "active_executors": 1
      }
    }
  ],
  "wallet": {
    "BTC": {"value_usd": 6500.0, "headroom_usd": 6400.0}
  },
  "oversub_alerts": [
    {
      "asset": "BTC",
      "ratio": 1.4,
      "severity": "warn",
      "controllers": ["bot::c1"]
    }
  ],
  "totals": {
    "controllers": 2,
    "committed_usd": 1600.0,
    "wallet_usd": 10000.0,
    "headroom_usd": 9800.0,
    "stuck_count": 1
  }
}
```

#### Diferencias explícitas user → agent

| Campo del payload completo | En user view | En agent view |
|---|---|---|
| `controllers[i].connector_name` | ✅ (tabla) | ✗ (no afecta decisiones del agente MVP) |
| `controllers[i].config_name`, `bot_name` | ✅ (tabla) | ✗ (codificado en `id` canónico) |
| `controllers[i].inventory.*` | ✅ (drilldown) | ✗ (lo va a procesar `controller_performance`) |
| `controllers[i].history.*` | ✅ (cuando esté) | ✗ (idem) |
| `controllers[i].capital.utilization_vs_worst` | ✅ | ✗ (redundante con `worst_case_usd` y `committed_now_usd`) |
| `controllers[i].capital.active_executors_count` | ✅ | ✅ (renombrado a `diagnostic.active_executors`) |
| `controllers[i].config.total_amount_quote` | ✅ | ✗ (queda implícito en `nominal_budget_usd`) |
| `controllers[i].config.buy_levels`, `sell_levels` | ✅ | ✗ (no es palanca actual del agente) |
| `wallet[asset].balance` | ✅ | ✗ (sólo importa USD) |
| `wallet[asset].used_now_usd` | ✅ | ✗ (queda implícito en `headroom_usd`) |
| `wallet[asset].controllers_using` | ✅ | ✗ (cross-ref ya en `oversub_alerts.controllers`) |
| Narrativa, glosario, KPIs renderizados | ✅ | ✗ |
| Campos formateados (`tp_bps`, `_fmt_usd`, etc.) | ✅ | ✗ (raw numbers) |
| `totals.headroom_usd` pre-agregado | ✗ (en KPI ya formateado) | ✅ |
| `totals.stuck_count` pre-agregado | ✗ (en KPI) | ✅ |

#### Tamaño esperado

- **User view (HTML report)**: ~5-15 KB por snapshot (depende de N controllers).
- **Agent view (JSON)**: ~750 chars con 2 controllers, ~5 KB con 14, **<50 KB** incluso con 50 controllers (cubierto por test).

#### Helper que lo construye

`routines/capital_state.py::_build_agent_payload(payload)` — función pura, testeada con 10 tests dedicados.

#### Cómo el engine del agente lo consume

```python
result = await capital_state_run(config, ctx)
agent_view = next(
    s["data"] for s in (result.sections or [])
    if s.get("type") == "data" and s.get("title") == "agent"
)
# agent_view se inyecta al prompt del LLM
```

---

### `market_regime` (implementada · multi-pair)

> Spec base de cómputo: `MARKET_REGIME_SPEC.md`.
> Connector default: **`binance` (spot)**.
> El routine es **multi-pair por diseño** — opera sobre N pares en
> una sola corrida y emite un único report agregado para el humano,
> pero un agent_view **por par** (B2 — ver decisión 2026-05-12).

#### Estrategia de pares

`Config.trading_pairs: list[str]` con dos modos:
- **vacío (default)**: autodetect desde los controllers activos del
  server elegido. Filtra a spot (los `pmm_mister` no usan perps).
- **explícito**: override puro, lista de pares a procesar.

Si en un mismo Run aparecen pares de varios connectors, el routine
los agrupa pero los procesa todos contra el connector declarado en
el Config (default `binance`). El autodetect respeta esto.

#### User view

Compone:
- **`text`** — 1 línea ASCII agregada:
  `"5 pairs · 1🟢 2🟡 2🔴 · worst: SOL-USDT adverse"` (multi-pair) o
  `"BTC-USDT · mean_rev_high_vol · 🟡 suboptimal · conf=hi"` (1 par).
- **4 KPI cards** agregadas:
  - PAIRS (count total).
  - OPTIMAL / SUBOPTIMAL / ADVERSE (count, con delta = % del total).
- **Tabla agregada** (`table_data` + `table_columns`):
  1 fila por par, columnas `pair, regime, favorability, confidence, bias, support_dist, resistance_dist`.
  Ordenada con adverse arriba (lo que necesita atención).
- **Report HTML persistido** (`ReportBuilder.save()` — L3):
  1. **Tabla resumen** arriba (la misma que la del RoutineResult).
  2. **Markdown TOC** con anchors `[BTC-USDT](#btc-usdt) · [ETH-USDT](#eth-usdt) · ...`.
  3. **Sub-sección por par** (`## <PAIR>`):
     - KPIs por par (regime, favorability, confidence, persistence).
     - Narrativa.
     - Tabla por timeframe (5m/1h/1d).
     - Niveles cercanos (S/R) y macro.
  4. **Glosario único** al final (no se repite por par).
  No hay tabs JS — `ReportBuilder` no las soporta. El TOC con anchors
  da la misma función (1 click salta a la sección del par).
- **Narrativa**: 2-5 frases determinísticas en castellano, status
  🟢/🟡/🔴 según `favorability`. Casos especiales:
  - Trending con pullback: mencionar explícitamente que el macro va
    contra la lectura del micro (es uno de los lugares donde la routine
    agrega valor — spec MARKET_REGIME §"Por qué la excepción").
  - Confidence=low: incluir disclaimer del lookback insuficiente.
- **Glosario**: 5 términos base (régimen, favorabilidad, NATR, Hurst,
  persistencia) + condicionales:
  - `trending_with_pullback` si aplica.
  - `support/resistance` si hay niveles cercanos (< 1% del precio).
  - `random_walk` si aparece en algún timeframe.
  - `position_in_range_30d` solo si > 0.85 o < 0.15 (zonas extremas).

#### Agent view (per-pair, multiple sections)

Cada par emite su propia sección `type="data", title="agent:<PAIR>"`,
no un único dict consolidado. Decisión "B2" del 2026-05-12:

- **Por qué**: cuando el engine del agente itere por controllers
  activos, va a querer pasar al LLM **solo** los pares del controller
  que está evaluando, no un payload monstruoso con todos. Las
  secciones independientes per-pair permiten ese filtrado trivialmente
  (`s["title"] == f"agent:{pair}"`).
- **Convivencia**: el `_save_report` y el `RoutineResult.text` del
  routine siguen siendo agregados (visión humana global). Solo el
  agent view es per-pair.

Shape de cada sección:

```json
{
  "ts": "2026-05-12T14:32:18Z",
  "pair": "BTC-USDT",
  "connector": "binance",
  "regime": "mean_reverting_high_vol",
  "favorability": "suboptimal",
  "confidence": "high",
  "persistence_minutes": 47,
  "bias": null,
  "trigger_candidates": ["take_profit", "spreads"],
  "by_timeframe": {
    "micro_5m":  {"dir": "mean_reverting", "vol": "high"},
    "meso_1h":   {"dir": "mean_reverting", "vol": "moderate"},
    "macro_1d":  {"dir": "trending_up",    "vol": "moderate"}
  },
  "key_levels": {
    "support_distance_pct": -0.006,
    "resistance_distance_pct": 0.008
  },
  "history_available": true
}
```

Adicionalmente se emite **una** sección `title="agent:summary"` con
la matriz agregada para uso comparativo del agente (decisiones
inter-controller):

```json
{
  "ts": "...",
  "pairs": ["BTC-USDT", "ETH-USDT", ...],
  "by_pair": {
    "BTC-USDT": {"regime": "...", "favorability": "...", "confidence": "..."},
    ...
  },
  "counts": {"optimal": 1, "suboptimal": 2, "adverse": 2},
  "worst": {"pair": "SOL-USDT", "favorability": "adverse"}
}
```

#### Diferencias explícitas user → agent

| Campo del payload completo | En user view | En agent view |
|---|---|---|
| `micro_5m.indicators.*` (NATR, BB, EMA slopes, ADX, realized_vol) | ✅ (tabla + glosario) | ✗ (el régimen ya resume la decisión) |
| `meso_1h.indicators.*` (Hurst, linreg, ATR, EMA slope) | ✅ | ✗ |
| `macro_1d.indicators.*` (volume ratio, range, vol30d, position_in_range) | ✅ | ✗ |
| `micro_5m.vol_history_24h` (p33/p67) | ✅ (debug / drilldown) | ✗ |
| `meso_1h.support_resistance.{support,resistance}.price` | ✅ | ✗ (el agente no opera con precios absolutos) |
| `meso_1h.support_resistance.*.distance_pct` | ✅ | ✅ (renombrado a `key_levels.*`) |
| `meso_1h.support_resistance.*.touches` | ✅ | ✗ (calidad del nivel solo importa al humano) |
| `macro_1d.levels_macro` (high/low 7d/30d/90d) | ✅ | ✗ (no son palancas del MVP) |
| `summary.canonical_regime` | ✅ | ✅ (renombrado a `regime`) |
| `summary.favorability/confidence/persistence_minutes/bias` | ✅ | ✅ |
| `summary.trigger_candidates` | ✅ (en narrativa) | ✅ (lista cruda) |
| `by_timeframe.*` con dir/vol por TF | ✅ (tabla) | ✅ (mínimo — el LLM puede razonar "macro va contra micro" sin tener todos los indicadores) |
| Narrativa, glosario | ✅ | ✗ |
| `history_required.samples_*` | ✅ (drilldown) | ✗ — solo el flag `history_available` |
| Régimen anterior (delta para KPI) | ✅ | ✗ (el agente ya tiene `persistence_minutes`; basta) |

#### Campos del SDK descubiertos en smoke test (2026-05-12)

El response real de `client.market_data.get_candles(...)` es una
**`list[dict]`** (no un dict envoltorio) ordenada **ascendente** por
timestamp, con campos:

```
timestamp, open, high, low, close, volume,
quote_asset_volume, n_trades,
taker_buy_base_volume, taker_buy_quote_volume
```

Implicancias:
- `quote_asset_volume` permite computar `volume_today_vs_avg30`
  **en USD** sin tener que multiplicar por mid-price. Mejora la
  comparabilidad entre pares y la robustez ante saltos de precio.
- `taker_buy_quote_volume / quote_asset_volume` da un "taker imbalance"
  que sirve para desempatar direccionalidad en regímenes ambiguos.
  **No entra al MVP** — anotado como candidato post-MVP.
- `n_trades` (count de trades por candle) podría reemplazar volume en
  pares ilíquidos. Mismo status: anotado, no MVP.

#### Tamaño esperado

- **User view (HTML report)**: ~3-8 KB por snapshot.
- **Agent view (JSON)**: ~400-600 chars (10× más chico que
  `capital_state`, intencional — `market_regime` es un input chico de
  alta densidad informativa).

#### Helper que lo construye

`routines/market_regime.py::_build_agent_payload(payload)` — función
pura, testeada con N tests (TBD durante implementación).

#### Cómo el engine del agente lo consume

Idéntico a `capital_state`:

```python
result = await market_regime_run(config, ctx)
agent_view = next(
    s["data"] for s in (result.sections or [])
    if s.get("type") == "data" and s.get("title") == "agent"
)
```

El agente combina `capital_state.agent_view` + `market_regime.agent_view`
en su prompt. Tamaño total esperado del bloque "market context" en el
prompt: <7 KB (decenas de pares posibles si fuera multi-pair futuro).

### `controller_performance` (en implementación · multi-controller)

> Spec base de cómputo: `CONTROLLER_PERFORMANCE_SPEC.md`.
> Mismo patrón multi-entity que `market_regime`: por defecto opera
> sobre **todos** los controllers activos del server; lista explícita
> via `controller_ids: list[str]` para single-controller / filtrado.
> Output agregado para el humano + per-controller agent views.

#### Desviaciones vs spec (validadas contra brigado 2026-05-12)

El response real de `bot_orchestration.get_active_bots_status` NO
incluye los campos que el spec asume (`total_positions`, `accuracy`).
Lo que sí viene en `controller.performance.*`:

```
realized_pnl_quote, unrealized_pnl_quote, unrealized_pnl_pct,
realized_pnl_pct, global_pnl_quote, global_pnl_pct,
volume_traded, positions_summary[], close_type_counts{}
```

`close_type_counts` viene con keys `"CloseType.TAKE_PROFIT"` (el
enum stringified). Las normalizamos a `take_profit`, `early_stop`,
`position_hold`, etc.

Derivaciones que reemplazan campos faltantes:
- `total_positions_closed` = `sum(close_type_counts.values())`.
- `total_positions_open`   = `len(positions_summary)`.
- `accuracy` ≡ `null` en MVP. Computarlo requiere PnL realizado por
  position **histórico** (ver pendientes); el snapshot solo expone PnL
  realizado del controller, no por position.
- `close_types_dominant` = key con max value tras strip de `"CloseType."`.

#### Persistencia (MVP)

`state/controller_performance/<canonical_id>.jsonl`, append-only,
1 snapshot por Run, rotación 30 días (mismo patrón que `capital_state`).
Schema de cada línea:

```json
{"ts": "...", "r_pnl": 12.42, "u_pnl": -11.48, "vol": 316717.16, "pos_closed": 2383}
```

El `<canonical_id>` es `{bot_name}::{_config_name}` (L1) sanitizado
para filesystem con `::` → `__` (helper `state_io.canonical_id_to_filename`).

Cuando llegue Fase 5.5 (agent dir) este path se moverá a
`trading_agents/<agent>/state/controller_performance/`. Por ahora
queda en la raíz para no acoplar la routine al agente que aún no
existe.

#### User view

Compose:
- **`text`** — 1 línea ASCII agregada:
  `"14 ctrl · 8 stuck? · 3 subopt · worst: btcusdt-1-5 PnL -2.3%"`.
- **4 KPI cards** agregadas:
  - CONTROLLERS (count total).
  - SUBOPTIMAL (count + delta `% del total`).
  - STUCK (count, trend `down` si > 0).
  - PNL 24h (sum across controllers en USD, trend según signo).
- **Tabla agregada** (`table_data` + `table_columns`):
  1 fila por controller, columnas
  `controller, pair, pnl_24h, vol_24h, pos_closed, fills/h_1h, last_fill_min, flags`
  donde `flags` resume `stuck/subopt/cold_start` con emojis.
  Ordenada con `suboptimal_now` arriba (atención humana).
- **Report HTML persistido** (`ReportBuilder.save()` — L3, con
  `manual_order()` por L9):
  1. 4 KPIs agregadas.
  2. Resumen narrativo (1-2 frases).
  3. Tabla resumen.
  4. Sub-sección por controller (`## <bot::config>`):
     - bullets: pair, régimen del controller, warmup_status, dominant close type.
     - mini-tabla de ventanas (`last_1h / last_6h / last_24h`) con
       `r_pnl, u_pnl, net_pnl, vol, vel_pnl, vel_vol, fills/h, last_fill_min`.
     - market_cross block (market_share_24h) si `compute_market_share=True`.
     - flags del diagnóstico.
  5. Glosario único al final.

#### Agent view (per-controller, multiple sections)

Igual que `market_regime`: una sección por controller con
`title="agent:<canonical_id>"` + una sección `agent:summary`. El
engine futuro filtra por `title.startswith("agent:")` y pasa al LLM
solo el del controller que está evaluando.

Shape per-controller:

```json
{
  "ts": "...",
  "controller_id": "{bot_name}::{config_name}",
  "trading_pair": "BTC-USDT",
  "windows": {
    "last_1h":  {"pnl_velocity": ..., "volume_velocity": ..., "fills_per_hour": ..., "samples": 6, "available": true},
    "last_6h":  {...},
    "last_24h": {...}
  },
  "snapshot": {
    "realized_pnl_quote": 12.42,
    "unrealized_pnl_quote": -11.48,
    "net_pnl_quote": 0.93,
    "volume_traded": 316717.16,
    "positions_open": 1,
    "positions_closed": 2383
  },
  "market_cross": {
    "market_share_24h": 0.0024
  },
  "diagnostic": {
    "is_pnl_flat": false,
    "is_volume_dropping": false,
    "is_stuck": false,
    "suboptimal_now": false,
    "suboptimal_period_minutes": 0,
    "warmup_status": "normal",
    "effective_window_key": "last_4h",
    "effective_window_hours": 4.0,
    "close_types_dominant": "take_profit",
    "time_since_last_fill_minutes": 2.3
  }
}
```

`agent:summary`:

```json
{
  "ts": "...",
  "controllers": ["bot1::c1", "bot1::c2", ...],
  "counts": {"suboptimal_now": 3, "stuck": 8, "cold_start": 2},
  "worst": {"controller_id": "bot1::c5", "pnl_velocity_24h_norm": -0.003}
}
```

#### Diferencias explícitas user → agent

| Campo del payload completo | User view | Agent view |
|---|---|---|
| `windows[i].realized_pnl_quote`/`unrealized_pnl_quote` | ✅ (tabla por TF) | ✗ (el agente decide con velocidades, no con totales) |
| `windows[i].volume_traded`, `total_positions_closed/open` | ✅ | ✗ |
| `windows[i].pnl_velocity*`, `volume_velocity`, `fills_per_hour` | ✅ | ✅ |
| `market_cross.exchange_volume_24h_quote`, `controller_volume_24h_quote` | ✅ | ✗ (la decisión solo necesita `market_share`) |
| `market_cross.market_share_24h` | ✅ | ✅ |
| `diagnostic.*` completo | ✅ | ✅ (entero — el LLM razona con todos los flags) |
| `close_type_counts` cruda | ✅ (drilldown) | ✗ (solo el dominante) |
| `close_types_dominant` | ✅ | ✅ (en diagnostic) |
| `positions_summary[]` cruda | ✅ (debug) | ✗ (lo procesa `capital_state`) |
| Narrativa, glosario, KPIs renderizados | ✅ | ✗ |
| `snapshot_for_history` (los 4 campos persistidos) | ✗ (interno) | ✗ |

#### Helper que lo construye

`routines/controller_performance.py::_build_agent_payload(ctrl_payload)`
para per-controller; `_build_agent_summary(aggregate)` para el resumen
cross-controller.

#### Tamaño esperado

- User view (HTML report): ~10-30 KB para 14 controllers.
- Agent view per-controller: ~500-700 chars.
- Agent summary: ~500 chars con 14 controllers.
- Total payload del LLM (1 controller en evaluación): ~2 KB
  (`agent:<id>` + `agent:summary` + `capital_state.agent` +
  `market_regime.agent:<pair>`).

---

## Reglas de oro

1. **El agente nunca lee user view**, ni siquiera "por debugging". Si
   hace falta debug del agente, se logea el agent_view tal cual.
2. **Si el agente necesita un campo nuevo**, agregarlo al
   `_build_agent_payload` Y a este doc Y a un test. Los tres se
   actualizan en el mismo commit.
3. **Si el user view cambia** (más KPIs, otra narrativa, etc.), no
   tocar el agent_view a menos que haga falta. Las dos vistas
   evolucionan de forma independiente.
4. **Cuando se implemente una nueva routine MVP**, agregar su sección
   acá antes de implementarla, no después. Forzar el ejercicio de
   pensar las dos audiencias separadas.
5. **Si `payload` (la sección con el dict completo) deja de ser útil
   para el CLI dashboard / archival**, repensar. Por ahora coexiste
   con `agent` en `sections` por trazabilidad.

---

## Cómo extender este doc

Cuando agregues una routine nueva al framework:

1. Diseñar el `_build_<routine>_user_view` (que probablemente sea
   el conjunto de funciones existente: KPIs + narrativa + tabla +
   glosario + ReportBuilder).
2. Diseñar el `_build_<routine>_agent_payload`.
3. Documentar acá:
   - Campos que entran al agent payload + por qué.
   - Campos que NO entran + por qué.
   - Tamaño esperado.
   - Cómo lo consume el engine del agente.
4. Tests de cada vista en el archivo de tests de la routine.
