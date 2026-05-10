# Capital Dynamics — modelo y routine `capital_state`

> Fuente: análisis del controller `pmm_mister` + investigación de la API de Condor/hummingbot-api.
> Última actualización: 2026-05-09

## El problema, en una frase

> Los controllers reservan capital nominalmente, pero comparten una wallet real. Un cambio de config en un controller puede impactar la operación de otros sin avisar — y si la suma supera la wallet, los bots se claven con `insufficient balance`.

## Tres planos de capital

### Plano 1 — Controller-local: nominal vs comprometido

Cada controller razona sobre **su propio** budget nominal:

```
nominal_budget         = total_amount_quote × portfolio_allocation
worst_case_committed   = nominal_budget × max_active_executors_by_level
committed_now          = sum(amount_quote de executors activos)
utilization_now        = committed_now / nominal_budget
utilization_worst      = max_active_executors_by_level (techo teórico)
```

**Implicancias**:
- Si subís `portfolio_allocation` y hay executors viejos → solapamiento (viejos con budget viejo + nuevos con budget nuevo).
- `max_active_executors_by_level=10` → utilization puede llegar a 10× el nominal en condiciones de TPs lentos.
- El controller **no** valida balance antes de colocar — solo emite y delega. El "insufficient" rebota desde abajo y deja al bot atascado.

### Plano 2 — Inter-controller: contención por activo

Cada controller toca **dos activos**: base y quote del trading_pair. La wallet es **compartida**:

```
Para cada activo A en {BTC, USDT, BRL, ...}:
  controllers_que_usan(A)    = [C: A in {base(C), quote(C)}]
  demanda_potencial(A)       = sum(C.worst_case_committed proyectado al activo A)
  suministro(A)              = balance real de A en wallet
  headroom(A)                = suministro(A) − demanda_actual(A)
  oversub_ratio(A)           = demanda_potencial(A) / suministro(A)
```

**Sobre-suscripción** = `oversub_ratio > 1`: dos o más controllers compitiendo por el mismo capital, garantizado choque en condiciones extremas.

**Caveat**: la "demanda proyectada al activo" no es trivial. Si C1 (`BTC-USDT`, target 50%) tiene budget $100, su demanda es ~$50 en BTC y ~$50 en USDT. Pero el split exacto depende del `current_base_pct` actual y del lado al que está colocando. Para MVP usamos `worst_case = budget` por activo (asume que en peor caso, una pata del controller consume todo el budget). Refinable.

### Plano 3 — Cross-pair: efectos colaterales del trading

Esto es lo más sutil y **no se puede medir solo con configs**:

- C1 (`BTC-USDT`) vende BTC → balance BTC baja → C2 (`BTC-BRL`) tiene menos BTC para vender, sin saberlo.
- C2 compra BTC → balance BTC sube → C1 tiene "más BTC del que él tracka".
- Cada controller calcula `current_base_pct` localmente sobre su `total_amount_quote`. **Esa cuenta puede mentir** respecto al estado real de la wallet.

**Implicancia para el agente**: cuando una rule mira inventory drift (`current_base_pct` fuera de target), no puede asumir que el drift es "culpa" del controller que lo reporta. Puede ser efecto cruzado de otro.

**Para MVP**: solo *reportar* este efecto, no actuar sobre él. Cualquier rule que toque sizing se mantiene confinada al controller que la dispara.

## Datos disponibles via hummingbot-api

Confirmé que tenemos todo lo necesario. Resumen de los métodos clave:

| Plano | Método | Devuelve |
|---|---|---|
| L1 | `client.controllers.get_bot_controller_configs(bot_name)` | configs completas (incluye `total_amount_quote`, `portfolio_allocation`, `max_active_executors_by_level`, `min/max_base_pct`) |
| L1 | `client.bot_orchestration.get_active_bots_status()` | per-controller `positions_summary` con `current_value` (= committed inventory) |
| L1 | `client.executors.search_executors(controller_ids=[...], status="active")` | executors detallados con `amount_quote`, `filled_amount_quote` (granularidad fina si la queremos) |
| L2 | `client.portfolio.get_state(account_names, connector_names)` | balances reales de wallet por activo |
| L2 | `client.accounts.list_accounts()` | enumera cuentas multi-wallet |
| L2 | `get_active_bots_status()` (un solo call) | enumera todos los bots/controllers activos |

**Patrón sugerido** (de `mcp_servers/hummingbot_api/tools/portfolio.py:21`): un `asyncio.gather` que pide bots + balances + executors en paralelo, después se mergea.

## Política respecto a sobre-suscripción

Decidido con el usuario:

1. **Reportar siempre**: `capital_state` informa el estado en cada tick, haya o no problema.
2. **Worst-case como filtro de operación** (invariant duro): si una propuesta llevaría `oversub_ratio > 1` en cualquier activo, se rechaza antes de mostrarla al humano.
3. **Promedio histórico para optimización** (segunda capa, no MVP): trackear el `utilization_now` real a lo largo del tiempo para identificar oportunidades. Si un controller usa el 20% de su `worst_case` en promedio, hay margen para subirle el `portfolio_allocation` (con confianza estadística, no especulación).
4. **Rebalanceo cross-controller**: si el agente detecta una oportunidad de rebalancear capital entre controllers, **pide aprobación al usuario** (un input específico). No es algo que el agente decida solo en MVP.

## Routine `capital_state` — spec

### Decisiones de diseño

1. **Una sola routine** que devuelve global + controllers en un solo payload. El agente necesita ver el sistema completo para evaluar oversub.
2. **Snapshot puro** en el output principal. El histórico (`p50/p95`) viene **derivado** de logs persistidos previamente; aparece null en el primer run.
3. **Default agregado por controller** (sum de executors via `positions_summary`). Para granularidad por executor, flag opcional `with_executor_details`.
4. **Unidad común: USD**. Hummingbot-api ya provee `value_usd` en `portfolio.get_state()`.
5. **Split base/quote por controller**: en MVP usamos `0.5/0.5` del nominal_budget para cada activo. Caveat documentado; refinable con `current_base_pct` real.
6. **Filtros como parámetros opcionales**: `account_name`, `bot_name`, `controller_ids`. Sin ellos = todo.

### Inputs (Pydantic Config)

```python
class Config(BaseModel):
    """Capital state across controllers and wallet."""
    account_name: str | None = Field(
        default=None,
        description="Account to inspect (default: master_account)",
    )
    bot_names: list[str] = Field(
        default=[],
        description="Filter to specific bots (empty = all active)",
    )
    controller_ids: list[str] = Field(
        default=[],
        description="Filter to specific controller IDs (empty = all)",
    )
    with_executor_details: bool = Field(
        default=False,
        description="Include detailed list of active executors per controller",
    )
    target_chat_id: int | None = Field(
        default=None,
        description="Send compact monitor view to this chat ID",
    )
    history_lookback_hours: int = Field(
        default=24,
        description="Lookback for utilization percentiles (p50/p95)",
    )
```

### Output schema (JSON)

```json
{
  "ts": "2026-05-09T14:32:18Z",
  "account": "master_account",

  "global": {
    "wallet": {
      "BTC": {
        "balance": 0.123,
        "value_usd": 8200.0,
        "used_now_usd": 6100.0,
        "headroom_usd": 2100.0,
        "headroom_pct": 0.256,
        "controllers_using": ["001_pmm_binance_BTC-USDT", "002_pmm_binance_BTC-BRL"]
      },
      "USDT": {
        "balance": 5400.0,
        "value_usd": 5400.0,
        "used_now_usd": 4200.0,
        "headroom_usd": 1200.0,
        "headroom_pct": 0.222,
        "controllers_using": ["001_pmm_binance_BTC-USDT"]
      },
      "BRL": {
        "balance": 12000.0,
        "value_usd": 2400.0,
        "used_now_usd": 0.0,
        "headroom_usd": 2400.0,
        "headroom_pct": 1.0,
        "controllers_using": ["002_pmm_binance_BTC-BRL"]
      }
    },
    "oversub_alerts": [
      {
        "asset": "BTC",
        "demand_potential_usd": 11500.0,
        "supply_usd": 8200.0,
        "ratio": 1.40,
        "severity": "warn",
        "controllers": ["001_pmm_binance_BTC-USDT", "002_pmm_binance_BTC-BRL"]
      }
    ]
  },

  "controllers": [
    {
      "id": "001_pmm_binance_BTC-USDT",
      "bot_name": "pmm-btc-1",
      "trading_pair": "BTC-USDT",
      "base_asset": "BTC",
      "quote_asset": "USDT",
      "connector_name": "binance",

      "config": {
        "total_amount_quote": 1000.0,
        "portfolio_allocation": 0.03,
        "max_active_executors_by_level": 10,
        "buy_levels": 2,
        "sell_levels": 2
      },

      "capital": {
        "nominal_budget_usd": 30.0,
        "committed_now_usd": 26.1,
        "worst_case_usd": 300.0,
        "utilization_now": 0.87,
        "utilization_vs_worst": 0.087,
        "active_executors_count": 3,
        "active_executors_by_side": {"buy": 2, "sell": 1}
      },

      "inventory": {
        "current_base_pct": 0.42,
        "target_base_pct": 0.5,
        "min_base_pct": 0.3,
        "max_base_pct": 0.7,
        "in_range": true,
        "drift_from_target": -0.08
      },

      "history": {
        "lookback_hours": 24,
        "samples": 142,
        "utilization_p50": 0.31,
        "utilization_p95": 0.72,
        "utilization_max": 0.95,
        "max_executors_observed": 6,
        "available": true
      },

      "executors": null
    }
  ],

  "summary": {
    "total_controllers": 1,
    "total_nominal_budget_usd": 30.0,
    "total_committed_now_usd": 26.1,
    "total_worst_case_usd": 300.0,
    "wallet_total_value_usd": 16000.0,
    "any_oversub": true,
    "max_oversub_ratio": 1.40
  }
}
```

### Notas del schema

**Asset assignment**:
- `controllers_using` lista los controllers que tienen ese activo como base **o** quote.
- `used_now_usd` por activo = sum de las patas asignadas. Para MVP: `0.5 × committed_now_usd` por cada lado. **Caveat**: subestima si el inventory está cargado a un lado.

**Oversub computation**:
```
demand_potential(asset)  = sum(controllers usando asset) of (worst_case_usd × 0.5)
supply(asset)            = wallet[asset].value_usd
ratio                    = demand_potential / supply
severity                 = "ok" if ratio < 0.8
                           "info" if 0.8 ≤ ratio < 1.0
                           "warn" if 1.0 ≤ ratio < 1.5
                           "crit" if ratio ≥ 1.5
```

Solo aparece en `oversub_alerts` si `severity in {"warn", "crit"}`. `info` se omite del array pero los datos siguen visibles en `wallet[asset]`.

**Inventory in_range**:
```
in_range = min_base_pct ≤ current_base_pct ≤ max_base_pct
```

**`history.available`**:
- `true` cuando hay al menos N samples en el lookback (umbral mínimo, ej: 10).
- `false` cuando recién arranca o no se persistió suficiente.
- Cuando es `false`, los percentiles van `null`.

**`executors`**:
- `null` por default.
- Si `with_executor_details=True`: array de executors activos con `executor_id`, `side`, `level`, `amount_quote`, `filled_amount_quote`, `unrealized_pnl_quote`, `age_seconds`.

### Output secundario: monitor compacto (si `target_chat_id` está presente)

Genera el formato visual definido en la sección "Visualización" más arriba y lo envía al chat especificado. La routine sigue retornando el JSON al caller (ej: agente).

### Cómputo (pseudocódigo)

### Cómputo (pseudocódigo)

```python
async def run(config, context):
    client = await get_client(...)
    
    # 1. Fetch en paralelo
    bots_data, accounts = await asyncio.gather(
        client.bot_orchestration.get_active_bots_status(),
        client.accounts.list_accounts(),
    )
    
    # 2. Por cada bot, traer configs y executors (paralelo)
    bot_names = list(bots_data["data"].keys())
    configs_per_bot = await asyncio.gather(*[
        client.controllers.get_bot_controller_configs(b) for b in bot_names
    ])
    
    # 3. Balances
    connector_names = unique_connectors_from_configs(configs_per_bot)
    balances = await client.portfolio.get_state(
        account_names=accounts, connector_names=connector_names, refresh=True
    )
    
    # 4. Construir el output según schema
    controllers_out = build_controller_summaries(bots_data, configs_per_bot)
    global_out = build_global_summary(controllers_out, balances)
    
    return {"global": global_out, "controllers": controllers_out, ...}
```

### Histórico de utilización

Para el `p50/p95/max_observed_24h`: append-only log local del agente. Cada tick que corre `capital_state` deja una entrada con `(ts, controller_id, utilization_now)`. La routine lee las entradas de las últimas 24h al construir el output.

**Storage**: archivo simple por controller dentro del session dir del agente, formato CSV o JSONL. El agente puede empezar sin histórico (los campos quedan `null`) y va populando con el tiempo.

## Visualización: monitor de utilización compacto

Pediste algo "estilo network monitor de servidor" que entre en poco lugar pero diga mucho. Propongo este formato textual (Markdown/Telegram-friendly):

```
📊 PORTFOLIO UTILIZATION  (master_account · 2026-05-09 14:32 UTC)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

WALLET HEADROOM
  BTC   ████████░░░░░░░░░░  74% used   $2.1k free
  USDT  ███████████████░░░  78% used   $1.2k free
  BRL   ░░░░░░░░░░░░░░░░░░   0% used   $2.4k free

CONTROLLERS  (utilization vs nominal)
  C1 BTC-USDT     ▓▓▓▓▓▓▓▓░░  87%  · alloc 3% × 10 lvl · p50 31% / p95 72%
  C2 BTC-BRL      ▓▓░░░░░░░░  18%  · alloc 5% ×  4 lvl · p50 12% / p95 45%
  C3 ETH-USDT     ▓▓▓▓▓▓▓▓▓▓ 102%  · alloc 4% ×  6 lvl · p50 88% / p95 110% ⚠️

⚠️ OVERSUBSCRIPTION
  BTC ratio 1.4× — C1, C2 (potential collision in worst case)
```

**Decisiones de diseño**:
- **Wallet primero**: lo más crítico. Una sola línea por activo: barra + % + headroom absoluto.
- **Controllers**: una línea por controller. La barra muestra `utilization_now`. La cola tiene los parámetros que importan (alloc, levels) + percentiles históricos.
- **Símbolos**:
  - `▓` lleno, `░` vacío, 10 caracteres = 10% c/u.
  - `⚠️` cuando un controller pasa el 100% (señal de problema o de oportunidad de subir budget si es promedio bajo).
- **Oversub al final**: visible pero no domina si todo está OK.

**Variantes**:
- **Versión ultra-compacta** (1 línea por controller, sin percentiles) para Telegram en mobile.
- **Versión expandida** con sparkline de utilización 24h por controller (cuando tengamos histórico).

Implementación: helpers `_bar(ratio, width)` y `_fmt(n)` ya existen en `routines/pmm_mister_supervisor.py:46-50` — los reutilizamos.

## Bootstrapping & Reconciliación (fuera del MVP, pero referenciado)

Hay un gap conocido en el flujo actual de creación de controllers que afecta a este framework: el campo `initial_positions` (que vive en `ControllerConfigBase` de Hummingbot, default `[]`) **no se setea desde Condor** — está explícitamente excluido del wizard, los fetchers y el editor.

**Consecuencia**: cuando arrancás un controller con inventory preexistente (ej. tenés 10k en BTC y querés repartir en 4 controllers), cada uno arranca creyendo que tiene 0% en base. El skew, el `current_base_pct` y la lógica de profit protection arrancan **desinformados**. Los controllers se van a comportar como si tuvieran que comprar BTC desde cero — incluso cuando en realidad tienen el activo de sobra.

**Dos cosas distintas que hay que abordar (en backlog, ver TASKS.md)**:

1. **Wizard de bootstrapping**: setear `initial_positions` correctamente al crear un controller con inventory ya existente. Es un gap del flujo de creación, no del agente.
2. **Routine `reconcile_inventory`**: periódicamente comparar el `current_base_pct` declarado del controller vs el real de la wallet (vía `portfolio.get_state`). Si hay discrepancia > umbral, alertar. Útil porque trades cruzados entre controllers pueden generar drift entre lo que el controller cree que tiene y la realidad.

**Por qué NO entra en MVP**:
- El MVP de `capital_state` solo *reporta*, y la rule MVP toca `take_profit` (no requiere conocer `initial_positions` correctamente).
- El día que tengamos rules de inventory (subir/bajar `target_base_pct`), bootstrapping y reconcile pasan a ser críticos.

## Lo que `capital_state` NO hace (boundaries)

- **No actúa**: solo reporta. Las decisiones las toma el agente.
- **No calcula efectos cross-pair en el trading** (Plano 3): solo los reporta cualitativamente.
- **No persiste en infraestructura nueva**: usa los archivos del session dir del agente para el histórico.
- **No reemplaza la verificación de balance al colocar orden**: es complementario, no sustituto.

## Persistencia del histórico

**Decisión: JSONL** (1 línea = 1 sample por controller).

### Por qué JSONL

- Volumen estimado: 1440 samples/día/controller × 5 controllers ≈ 150 KB/día. SQLite no compensa la complejidad a este volumen.
- Append-only matchea la lección L5 (audit log inmutable).
- Inspectable a ojo (`cat`, `tail`, `grep`).
- Importable a DataFrame en 1 línea cuando se necesite analytics.

### Estructura

**Un archivo por controller**, no uno global:

```
trading_agents/<agent>/state/capital_history/<controller_id>.jsonl
```

Razones: ciclo de vida independiente por controller, lecturas filtradas más rápidas, archivar/borrar uno solo es trivial.

### Schema de cada línea

```json
{"ts":"2026-05-09T14:32:18Z","cmt_usd":26.1,"util":0.87,"util_w":0.087,"exec":3,"in_range":true}
```

Solo lo que cambia tick a tick. Datos derivables (worst_case, nominal_budget) viven en la config y no se persisten.

### Cómputo de percentiles

```python
def compute_percentiles(history_path: Path, lookback_hours: int = 24, min_samples: int = 10):
    cutoff = datetime.now(UTC) - timedelta(hours=lookback_hours)
    utils = []
    with open(history_path) as f:
        for line in f:
            entry = json.loads(line)
            if datetime.fromisoformat(entry["ts"]) >= cutoff:
                utils.append(entry["util"])
    if len(utils) < min_samples:
        return {"available": False, "samples": len(utils)}
    return {
        "available": True,
        "samples": len(utils),
        "utilization_p50": float(np.percentile(utils, 50)),
        "utilization_p95": float(np.percentile(utils, 95)),
        "utilization_max": max(utils),
    }
```

### Rotación

- Truncado por edad: descartar líneas anteriores a 30 días al final de cada run.
- Cuando el volumen crezca: pasar a rotación por mes (`<controller_id>-YYYY-MM.jsonl`).

## Cadencia y caché

- **Cadencia**: la routine se invoca en cada tick del agente. **Tick del agente: 10 minutos** (D9 — para acomodar el ciclo de backtests). Cuando el agente está en backtest loop, no afecta la cadencia de capital_state — sigue corriendo cada 10 min como parte del ciclo.
- **Caché interno**: ninguno. Cada call refresca completo — el frescor es crítico para detectar oversub temprano.
- **Balances**: usar `refresh=True` en `client.portfolio.get_state()` para forzar fetch desde exchange.

## Invariant `validate_no_oversub`

Función que el agente invoca **antes** de proponer cualquier patch. Si retorna `False`, el patch se descarta sin llegar al humano.

### Firma

```python
def validate_no_oversub(
    patch: dict[str, dict],          # {controller_id: {field: new_value}}
    current_state: dict,              # output de capital_state
    safety_margin: float = 0.95,      # max ratio permitido (95% del wallet)
) -> tuple[bool, dict | None]:
    """
    Returns (True, None) if patch is safe.
    Returns (False, {reason, asset, ratio, demand_usd, supply_usd, controllers}) if rejected.
    """
```

### Cómputo

1. Aplicar el patch hipotéticamente al `current_state` (in-memory, no persiste).
2. Para cada activo, recalcular `demand_potential` con los valores nuevos.
3. Si `demand / supply > safety_margin` para algún activo → rechazar.
4. Si todo OK → aceptar.

### Optimización: detectar early-out

Solo 3 campos del patch alteran la demanda:
- `total_amount_quote`
- `portfolio_allocation`
- `max_active_executors_by_level`

Si el patch no toca ninguno → retornar `(True, None)` sin recomputar. El resto de campos (spreads, take_profit, etc.) no afectan el invariante.

### `safety_margin` default

**0.95** — deja 5% de colchón para variaciones naturales (precios fluctúan, fees, slippage). Configurable por agente en `invariants.yaml`.

### Multi-controller atomicidad

Un patch puede tocar varios controllers (rebalanceo, futuro). El cómputo aplica **todos los cambios al estado proyectado** antes de evaluar — no se valida controller por controller aislado.

## Pendientes (para revisar más adelante)

- **Nivel de detalle de executors**: cuándo invocar con `with_executor_details=True`. Default `False`. Posible criterio: cuando hay oversub_alert activa, o cuando el agente investiga un controller específico.
- **Refinar split base/quote**: en MVP usamos `0.5/0.5` del nominal_budget. Refinable usando `current_base_pct` real para split exacto.
- **Análisis cross-pair**: efectos colaterales del trading entre controllers (Plano 3) hoy solo se reportan, no se modelan formalmente.

## Próximos pasos

1. Implementar `routines/capital_state.py` siguiendo el patrón de `pmm_mister_supervisor.py`.
2. Implementar el invariant `validate_no_oversub` (probablemente en `condor/agent_invariants/` o similar — definir ubicación al implementar).
