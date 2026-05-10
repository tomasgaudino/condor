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

### `market_regime` (no implementada todavía)

Pendiente. Cuando se implemente, agregar acá:

- **User view**: regímenes por timeframe (5m/1h/1d), favorability, niveles
  S/R, indicadores con explicación.
- **Agent view (esperado)**: solo el régimen canónico, favorability,
  trigger_candidates, persistence_minutes, confidence. Sin narrativa de
  cómo se llegó al régimen.

### `controller_performance` (no implementada todavía)

Pendiente. Cuando se implemente, agregar acá:

- **User view**: tabla por controller con PnL/volume/market share por
  ventana, gráficos de evolución temporal.
- **Agent view (esperado)**: por controller `pnl_velocity`,
  `volume_velocity`, `subóptimo_now`, `time_since_last_fill`,
  `period_subóptimo_minutes`. Sin formato.

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
