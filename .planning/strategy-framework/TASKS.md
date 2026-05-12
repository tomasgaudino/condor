# Tasks — Adaptive Strategy Framework

> Fuente de verdad del avance. Editar marcando `[x]` cuando se completa.
> Ver detalle de decisiones en `DECISIONS.md`. Ver diseño en `DESIGN.md`.
>
> **⚠ LECTURA OBLIGATORIA al arranque de cada sesión sobre este
> framework**: `LEARNINGS.md` (errores cometidos antes — no recaer).

## Convenciones

- `[ ]` pending — `[x]` done — `[~]` in progress — `[!]` blocked
- Cada tarea de nivel 1 es un hito. Las subtareas son los pasos para llegar.
- Si una subtarea genera otra, anidar.

---

## Fase 0 — Discovery & Diseño

- [x] **0.1** Mapear el repo (controllers, routines, agentes existentes)
- [x] **0.2** Capturar lecciones del `pmm_mister_supervisor` descartado
    - [x] Leer journals de las 5 sesiones
    - [x] Extraer 6 lecciones a `LESSONS_FROM_SUPERVISOR.md`
- [x] **0.3** Deep dive del controller `pmm_mister`
    - [x] Mapear los 30+ campos por función
    - [x] Identificar los 25 campos `is_updatable` (hot-reload)
    - [x] Documentar caveats (TP solo afecta executors nuevos, etc.)
- [x] **0.4** Resolver primera ronda de open questions
    - [x] Q1 hot-reload, Q3 forma de reglas, Q4 modos, Q5 MVP, Q6 1 agente, Q7 kill switch, Q8 auditabilidad → D1–D8
- [x] **0.5** Investigar dinámica de capital
    - [x] Confirmar fórmula del amount por nivel
    - [x] Identificar el multiplicador oculto (`max_active_executors_by_level`)
    - [x] Confirmar ausencia de validación interna de balance
- [x] **0.6** Diseñar `capital_state` en 3 planos
    - [x] Plano controller-local
    - [x] Plano inter-controller (red por activo)
    - [x] Plano cross-pair (efectos colaterales)
    - [x] Visualización tipo network monitor

---

## Fase 1 — Especificación de routines (MVP)

- [~] **1.1** Routine `capital_state` — spec completa
    - [x] Definir schema JSON exacto del output
    - [x] Decidir formato del histórico → JSONL, 1 archivo por controller
    - [x] Definir cadencia y caché → tick del agente (10 min, D9), sin caché, refresh forzado
    - [ ] Definir nivel de detalle: ¿pulleamos `search_executors` siempre o solo cuando se sospecha? (PENDIENTE — revisar más adelante)
    - [x] Borrador del cómputo del invariant `validate_no_oversub(patch, state)`
- [x] **1.2** Routine `market_regime` — spec completa → `MARKET_REGIME_SPEC.md`
    - [x] Framework conceptual quant para PMM (2 fuentes de PnL, tensión spread vs inventory)
    - [x] Multi-timeframe: 5m (operación) / 1h (zona) / 1d (contexto)
    - [x] Indicadores por timeframe (NATR, ADX, Hurst, EMA slopes, BB width, S/R, etc.)
    - [x] Clasificación local (direccionalidad × volatilidad)
    - [x] Régimen canónico con excepción `trending_with_pullback`
    - [x] Matriz de favorabilidad para PMM
    - [x] Mapeo régimen → trigger_candidates (input para expansor de D9)
    - [x] Schema JSON del output
    - [x] S/R con pivot detection + clustering (algoritmo simple, MVP)
- [x] **1.3** Routine `controller_performance` — spec completa → `CONTROLLER_PERFORMANCE_SPEC.md` (reemplaza `volatility_metrics`, ver D10)
    - [x] Métricas del controller: PnL realizado/no-realizado, volume, num fills, accuracy, executor turnover, fill rate
    - [x] Métricas cruzadas con mercado: market share del par, gross spread captured
    - [x] PnL velocity (derivada temporal) — requiere persistencia mínima
    - [x] Definir cómo se compone el "período subóptimo" para disparar D9 (umbrales)
    - [x] Schema JSON del output
- [x] **1.4** Catálogo de routines reutilizables (no MVP) → `NON_MVP_ROUTINES.md`
    - [x] Filtrar solapamientos: `pnl_velocity` y `volatility_metrics` descartadas
    - [x] Documentar `inventory_drift`, `spread_efficiency`, `price_zone`, `controller_health`, `market_microstructure`, `reconcile_inventory`
    - [x] Criterio de promoción a MVP cuando aparezca una rule que las requiera

---

## Fase 2 — Especificación del agente

- [x] **2.1** Esquema del agente adaptativo → `AGENT_SCHEMA_SPEC.md`
    - [x] Estructura de archivos (`agent.md`, `config.yml` extendido, `invariants.yaml`, `state/`)
    - [x] `agent.md` con secciones semánticas (scope, routines, policy, mode, output format)
    - [x] `config.yml` con sub-modelo `adaptive` (mode, scope, routines, backtest, notifications)
    - [x] `invariants.yaml` completo (whitelist, blacklist, deltas, cooldowns, capital, circuit breaker, auto mode)
    - [x] Validación contra invariants (pseudocódigo)
    - [x] Backwards compatibility con agentes legacy de Condor
- [x] **2.2** Contrato del patch → `PATCH_CONTRACT_SPEC.md`
    - [x] 4 estados (PROPOSAL → CANDIDATES → SELECTED PATCH → APPLIED PATCH)
    - [x] Schema de cada estado con shapes JSON
    - [x] Expansor determinístico (tablas para percentage/absolute, behavior de small/medium/large)
    - [x] Comportamiento por escenario (todos mejoran, todos empeoran, mezcla, backtest fail)
    - [x] Anti-evidence log para casos donde todos los candidatos empeoran (D-Q3)
    - [x] Modo de selección humano: aprobar/rechazar + elegir entre candidatos (D-Q4)
    - [x] Validación pre-aplicación con orden estricto
- [x] **2.3** Memoria persistente del agente → `MEMORY_SPEC.md`
    - [x] Inventario de stores (3 categorías: series temporales, decisiones, semántica)
    - [x] Read context del LLM cada tick (policy + learnings + routines + 10 decisiones recientes + 5 anti-evidence + cooldowns)
    - [x] Lifecycle por categoría (series 30d truncado, decisiones rotación mensual indefinida, learnings curado humano)
    - [x] Outcome measurement loop (30 min, mecánica a)
    - [x] Helpers de IO + queries de alto nivel
    - [x] Política de escritura del agente en learnings.md (con confirmación humana, opción b)
- [x] **2.4** Circuit breaker → `CIRCUIT_BREAKER_SPEC.md`
    - [x] Decisión: **manual-only**, sin auto-triggers (consistente con D7)
    - [x] Estados (active/paused) y persistencia (`agent_status.json`)
    - [x] Comportamiento durante pausa (observación sigue, razonamiento no)
    - [x] Métricas de health on-demand (no routine dedicada)
    - [x] Especificación de endpoints UI (para fase posterior)
    - [x] Modo MVP cero sin UI (edición manual o comandos Telegram)

---

## Fase 2.5 — Backtest Evidence Loop ✅ → `BACKTEST_LOOP_SPEC.md`

> Spec ✅ y **implementación ✅** (commit ad-hoc del 2026-05-12):
> - `condor/trading_agent/adaptive/expander.py` — expansor determinístico
> - `condor/trading_agent/adaptive/scoring.py` — score + decomposition + winner
> - `condor/trading_agent/adaptive/backtest_cache.py` — sha256 cache + cleanup
> - `condor/trading_agent/adaptive/backtest_loop.py` — orchestrator + anti-evidence
> - 66 tests nuevos


> Capa de evidencia empírica antes de proponer al humano. Ver D9 en DECISIONS.md.

- [x] **2.5.1** Disparador del ciclo de backtest
    - [x] Orden de chequeos (status → subóptimo → favorability → anti-evidence → cooldowns)
    - [x] Confirmación con `market_regime` integrada
    - [x] Output estructurado de "no_trigger + reason" para debugging
- [x] **2.5.2** Selección de ventana
    - [x] Algoritmo: usa `suboptimal_period_minutes` de `controller_performance`
    - [x] Sin cap (Q1=a) — protección por timeout global
    - [x] Mínimo 30 min como piso
- [x] **2.5.3** Expansor determinístico → consolidado, spec en `PATCH_CONTRACT_SPEC.md`
- [x] **2.5.4** Función de score
    - [x] Fórmula `vol_norm + 0.3 × pnl_norm` con filtro PnL ≥ 0
    - [x] Score baseline = 1.3 explícito
    - [x] Decomposition para auditoría
    - [x] Plan de iteración futura via outcome data (Fase F2)
- [x] **2.5.5** Cache de backtests
    - [x] Hash key con canonicalización de config
    - [x] Reuse `BacktestStore` existente
    - [x] Cleanup diario (Q2=b) con marker file
- [x] **2.5.6** Manejo de errores
    - [x] Connector no soportado → política `on_backtest_failure`
    - [x] Datos insuficientes → idem
    - [x] Timeout 5 min global → cycle abort + log
    - [x] Lock global serializa (Q3=default + revisar en Fase 5)

## Fase 3 — Especificación del modo `propose` ✅ → `PROPOSE_MODE_SPEC.md`

- [x] **3.1** Formato del mensaje de Telegram
    - [x] Layout completo (Q1=a) con tabla de backtests + reasoning blockquote + caveats
    - [x] Botones inline en 2 filas: Apply winner + alternativas + Reject + Snooze
    - [x] Pattern callback: `adaptive:<action>:<patch_id>:<args>`
    - [x] Variante compacta documentada como pendiente futuro
- [x] **3.2** Handler de respuesta
    - [x] Dispatcher por `adaptive:` namespace, prevención de doble-click
    - [x] handle_apply: candidato elegido → MCP tool → audit_log update + last_changes
    - [x] handle_reject: cooldown estándar registrado, sin pedir razón (Q2=a)
    - [x] handle_snooze: cooldown hasta snoozed_until_ts
    - [x] Timeout 24h auto-expire (Q3=a)
    - [x] Cooldowns duros sin override (Q4=a)
- [x] **3.3** Persistencia del flow propose-to-apply
    - [x] State machine completa (pending → approved/rejected/snoozed/expired)
    - [x] Update in-place atómico del audit_log
    - [x] Caveats de concurrencia y pendientes documentados

---

## Fase 4 — MCP tool: `update_controller_config` ✅ → `MCP_TOOL_SPEC.md`

- [x] **4.1** Ubicación: `mcp_servers/hummingbot_api/tools/adaptive_agent.py` + módulo `_controller_field_metadata.py`
    - [x] Reusa `update_bot_controller_config` existente (no requiere cambios en HB ni HB-API)
    - [x] Endpoint HB hace merge shallow → mandamos solo `{field: value}` (atómico naturalmente)
    - [x] Hot-reload confirmado punta a punta (10s ETA via strategy_v2_base.config_update_interval)
- [x] **4.2** API completa
    - [x] Signature: `update_controller_config(client, bot_name, controller_id, field, value, expected_old_value=None)`
    - [x] Validaciones en orden: bot existe → controller existe → whitelist + blacklist → type coercion → optimistic-lock → API validate → apply
    - [x] Output success con `old_value`, `new_value`, `caveats`, `hot_reload_eta_seconds`
    - [x] Output error con `error_code` + `message` + `details` (8 códigos de error)
- [x] **4.3** Manejo de errores
    - [x] Política unificada: dominio = dict, bugs = raise
    - [x] Atomicidad natural por merge shallow
    - [x] Sin rollback automático — `audit_log.old_value` permite manual revert
    - [x] Caveats por field automáticos (take_profit affects only new executors, etc.)

---

## Fase 5 — Implementación

- [ ] **5.1** Implementar `routines/capital_state.py`
- [x] **5.2** Implementar `routines/market_regime.py`
    - [x] Módulo `condor/trading_agent/adaptive/indicators.py` (numpy/scipy, 34 tests)
    - [x] Routine multi-timeframe (5m/1h/1d) con NATR/ADX/Hurst/EMA slope/linreg + S/R pivot detection
    - [x] Canonical regime con excepción trending_with_pullback
    - [x] Favorabilidad matrix + trigger_candidates
    - [x] Dos vistas: user (KPIs+tabla+narrativa+glosario+report HTML) y agent (~560 chars)
    - [x] 39 tests + smoke validado contra brigado (BTC-USDT, ETH-USDT)
    - [ ] Persistence_minutes (regime_history.jsonl): se completa cuando el agente lea el output. Por ahora None.
- [x] **5.3** Implementar `routines/controller_performance.py` (D10)
    - [x] Multi-controller con autodetect (mismo patrón que market_regime multi-pair)
    - [x] Snapshot + velocidades sobre 1h/6h/24h via state_io JSONL append-only (30d rotation)
    - [x] Cold-start (F3): cold_start <30min · adaptive 30min-4h · normal ≥4h
    - [x] Diagnóstico: pnl_flat / volume_dropping / stuck / suboptimal_now / time_since_last_fill
    - [x] Market_share_24h via get_candles 1h × 24 (quote_asset_volume sum)
    - [x] Dos vistas: aggregate user + per-controller agent payloads (~900 chars c/u, summary ~460)
    - [x] Reporte HTML con manual_order (L9) — KPIs + tabla resumen + sub-secciones por controller
    - [x] 48 tests + smoke validado contra brigado (14 controllers)
    - [ ] `suboptimal_period_minutes` real (requiere tracking de transiciones, igual que `persistence_minutes` de market_regime — se completa cuando el agente lea el output)
    - [ ] `accuracy`, `adverse_fill_ratio`, `gross_spread_captured` — fuera del MVP (requieren PnL por position histórico o data de price post-fill)
- [x] **5.4** Implementar la MCP tool `update_controller_config`
    - [x] Módulo de metadata `_controller_field_metadata.py` (whitelist pmm_mister, blacklist absoluta, caveats)
    - [x] Tool `adaptive_agent.py::update_controller_config` con todos los paths del spec
    - [x] Registración en `server.py` con `@mcp.tool()`
    - [x] 48 tests (whitelist, blacklist, controller_not_supported, coerce bool/int/float/list/string, optimistic-lock, validation graceful-degrade, api_error, atomic payload single-field, response shape, caveats)
    - [x] Smoke validado contra brigado: idempotent write OK, los 6 error paths funcionan
    - [x] L11 anotada: `validate_controller_config` rechaza `_config_name` inyectado por el GET — el graceful-degrade del tool maneja esto correctamente
- [x] **5.5** Crear el agent dir: `trading_agents/adaptive_pmm/`
    - [x] `agent.md` con la rule MVP de D5 — frontmatter compatible con StrategyStore + body con scope/routines/policy/mode/output format. Condor lo autodescubre (verificado).
    - [x] ~~`policy.md`~~ → la policy queda dentro del body del agent.md (sección "Policy"). El spec mismo embebe la policy en el body, no es archivo separado.
    - [x] `invariants.yaml` con límites duros (24 allowed, 11 forbidden, 7 overrides, cooldowns, capital, modes)
    - [x] `state/` con README + .gitkeep para memoria persistente
    - [x] Loader/validator `condor/trading_agent/adaptive/invariants.py` con cross-check contra MCP tool metadata (F5 — fuente única de truth)
    - [x] 36 tests del loader (file errors, required keys, type overlap, capital bounds, backtest enum, helpers, cross-check drift detection)
- [x] **5.6** Implementar handler de modo `propose` (Telegram inline buttons)
    - [x] `condor/trading_agent/adaptive/audit.py`: append + update-in-place atómico del audit_log.jsonl, upsert de last_changes.json, compute_cooldown_end con 3 tiers (per-field/per-controller/global) + snooze cooldown_until extiende, expire_old_pending_proposals (24h)
    - [x] `handlers/adaptive/messages.py`: callback data builders (apply/reject/snooze), build_propose_text (MarkdownV2 escapado), build_propose_keyboard (winner-first + alternativas + reject/snooze), verdict edit messages
    - [x] `handlers/adaptive/__init__.py`: dispatcher con `pattern="^adaptive:"`, descubrimiento del agent_dir por presencia de invariants.yaml, double-click guard, apply/reject/snooze handlers con full state lifecycle, integración con la MCP tool de 5.4, send_proposal API para la fase 5.7
    - [x] Registración en `main.py`
    - [x] 94 tests nuevos (24 audit + 22 messages + 48 handler con fakes)
    - [x] Note: agent loop que GENERA las proposals queda para 5.7 — esta fase entrega la infraestructura de propose mode (lifecycle + persistencia + UI Telegram + apply via MCP tool)
- [x] **5.7** Implementar modos `shadow` y `auto` + orchestrator del tick
    - [x] `condor/trading_agent/adaptive/orchestrator.py` — `run_tick(ctx, agent_md_body)`. Pre-flight (agent_paused, expire pendientes, cache cleanup) → build_llm_prompt → invoke llm_call → parse_llm_response → loop por proposal: invariants pre-check → backtest_cycle → audit entry → mode-dependent action (propose/shadow/auto). LLM y MCP apply son INJECTADOS (testeable sin Condor).
    - [x] Modo `propose`: usa `send_proposal` del handler de 5.6.
    - [x] Modo `shadow`: log-only en audit_log con `human_verdict=shadow_logged`.
    - [x] Modo `auto`: aplica directo via MCP tool tras stricter check (`require_backtest_evidence`). Marca `human_verdict=auto_applied` + escribe `last_changes` para cooldowns.
    - [x] 24 tests (paused, parse variants, invariants pre-check, auto strict checks, los 3 modos con verdicts variados, fallos parsing/LLM, build_audit_entry canónico).
    - [ ] **Pendiente para una pasada de integración futura**: `runner.py` que (a) corre las 3 routines reales del repo, (b) extrae sus `agent:summary` + per-entity sections, (c) instancia el LLM client (claude-code via ACP o pydantic-ai) según `agent.agent_key`, y (d) invoca `run_tick`. Lo separamos como integración porque depende del runtime de Condor y se valida end-to-end con un smoke test en vivo, no con unit tests.

---

## Fase 6 — Validación

- [ ] **6.1** Correr el agente en modo `shadow` por N días
- [ ] **6.2** Analizar logs: ¿las propuestas tendrían sentido?
- [ ] **6.3** Pasar a modo `propose` con bot real (capital chico)
- [ ] **6.4** Iterar policy según feedback
- [ ] **6.5** Decisión: ¿pasar a `auto` o quedarse en `propose`?

---

## Fixes aplicados (2026-05-10)

> Resolución de blockers de REVIEW_NOTES.md. Detalle en `FIXES_2026-05-10.md`.

- [x] **F1** `controller_id` canónico = `{bot_name}::{config_name}` + helper `resolve_controller`
- [x] **F2** `compute_match_score` y `read_controller_performance_window` con fórmulas concretas en MEMORY_SPEC
- [x] **F3** Política de cold-start con ventana adaptativa (`cold_start` / `adaptive` / `normal`)
- [x] **F4** Rename `subóptimo` → `suboptimal` en todos los identificadores
- [x] **F5** `invariants.yaml` declarado como fuente única de truth para whitelist/blacklist
- [x] **F6** Schema canónico de audit_log con todos los campos (`bot_name`, `pre_apply_snapshot`, `telegram_message_id`, etc.)
- [x] **F7** Outcome loop cross-archivo (lee mes corriente + mes anterior si <7 días del mes)

---

## Fases futuras (post-MVP)

> Cosas que el usuario quiere que **no perdamos de vista**. Tienen entidad propia (no son simples ítems sueltos) y se planificarán como fases dedicadas cuando llegue el momento.

### Fase F1 — Pantallas de UI del Supervisor

**Objetivo**: dar visibilidad y control humano via web.

Componentes:

- **Pantalla de control del PMM Supervisor** (agente adaptativo):
  - Panel de health (todas las métricas definidas en `CIRCUIT_BREAKER_SPEC.md`).
  - Botones pause/resume con captura de razón.
  - Audit log navegable (últimas N propuestas con outcome).
  - Anti-evidence log para inspección.
  - Vista del histórico de cambios de status.

- **Pantalla por controller PMM Mister** (más detallada):
  - Estado actual de la config (todos los campos relevantes).
  - Histórico de cambios aplicados a este controller.
  - Series temporales: utilization, regime, performance (gráficos).
  - Tabla de outcomes recientes vs expected.
  - Scoreboard de propuestas: cuántas aprobadas/rechazadas, match quality.

Subtareas estimadas (a detallar cuando se aborde):
- [ ] Endpoints REST en `condor/web/routes/agents.py` (status, health, pause, resume).
- [ ] Endpoints REST por controller (config histórica, decisiones, métricas).
- [ ] Frontend (probablemente extender `frontend/src/` existente).
- [ ] WebSocket para updates live (opcional).

**Cuándo**: después de validar el MVP en `propose` mode con uso real.

### Fase F2 — Análisis histórico de bots (mejora continua empírica)

**Objetivo**: aprovechar la base de datos de bots que ya corrieron en el pasado (con su performance real) para entrenar/calibrar al agente con datos empíricos.

**Lo que esto resuelve** (corazón de la idea del usuario): hoy el agente solo aprende de las propuestas que aplicó. Pero hay **muchísima información histórica** disponible: bots que corrieron meses con configs fijas, atravesaron distintos regímenes, con outcomes reales medibles. Esa data es oro empírico que el agente debería tener en cuenta.

Componentes:

- **Pipeline de análisis offline**:
  - Identificar bots completados en la base de datos de Hummingbot.
  - Para cada período de un bot: clasificar régimen (con `market_regime` retroactivo).
  - Cruzar régimen × config × performance real.
  - Output: dataset de "en régimen X con config Y, el outcome real fue Z".

- **Insights extraíbles**:
  - "Configs con `take_profit > T` rinden mejor en `mean_reverting_high_vol`".
  - "En `trending_up`, `portfolio_allocation < A` evita pérdidas grandes".
  - "Cuando `market_share < S`, los spreads más bajos no mejoran fills".
  - Etc.

- **Integración con el agente**:
  - Una nueva routine `historical_priors` que dado el régimen actual + controller config, devuelve "qué hicieron configs similares en regímenes similares en el pasado".
  - El LLM lee esto en su contexto y razona con priors empíricos, no solo intuición.

- **Datos requeridos** (a verificar cuando se aborde):
  - ¿Hay base de datos persistente de bots completados? Qué guarda.
  - ¿Tenemos OHLCV histórico para reclasificar régimen retroactivamente?
  - ¿Tenemos config inicial + cambios de config a lo largo del tiempo del bot?

Subtareas estimadas:
- [ ] Auditar qué datos históricos están disponibles en Condor/Hummingbot.
- [ ] Diseñar el dataset agregado (régimen × config × outcome).
- [ ] Construir el pipeline offline (probablemente un Jupyter notebook o script en `routines/`).
- [ ] Diseñar la routine `historical_priors`.
- [ ] Integrar con el read context del LLM.
- [ ] Validar que los priors mejoran las propuestas (A/B con/sin priors).

**Por qué importa para el framework**: cierra el loop de **mejora continua con información empírica real**, no solo backtest sintético. Es la diferencia entre un agente que razona en abstracto y uno que razona basado en lo que efectivamente funcionó antes.

**Cuándo**: después del MVP. Es una fase con entidad propia que justifica su propio diseño.

---

## Backlog (no MVP, ideas futuras)

- [ ] Memoria de "qué cambios funcionaron históricamente"
- [ ] A/B de configs entre 2 controllers similares
- [ ] Generalizar a otros controllers además de `pmm_mister`
- [ ] Routine de rebalanceo cross-controller (con aprobación humana)
- [ ] Detección automática de "controller atrapado" (fuera de rango por mucho tiempo + sin fills)
- [ ] Dashboard web del estado del agente (no solo Telegram)
- [ ] **Bootstrapping de controllers**: wizard que setee `initial_positions` correctamente al crear un controller con inventory preexistente. Hoy Condor lo deja siempre vacío y el controller arranca creyendo que tiene 0% en base. Documentado en `CAPITAL_DYNAMICS.md` (sección Bootstrapping).
- [ ] **Routine `reconcile_inventory`**: periódicamente comparar `current_base_pct` declarado del controller vs el real de la wallet. Alertar si hay discrepancia significativa. Útil después del bootstrapping inicial y para detectar drift por trades cruzados entre controllers.
- [ ] **Multi-field patches** (post-MVP): permitir que un patch toque múltiples campos del mismo controller en una sola operación atómica. Útil cuando dos cambios coordinados son mejores que dos cambios secuenciales (ej: ampliar spreads + subir take_profit). Documentado en PATCH_CONTRACT_SPEC.md.
- [ ] **Granularidad fina de magnitud**: extender de 3 niveles (small/medium/large) a 5 niveles o a percentile numérico. Solo si los 3 actuales se quedan cortos.
- [ ] **Auto-flip de dirección**: cuando todos los candidatos empeoran al baseline, probar automáticamente la dirección opuesta. Desactivado en MVP — preferimos `no_action` + anti-evidence log.
- [ ] **Score formula adaptativa**: hoy fórmula fija, podría ajustarse según régimen.
- [ ] **Outcome learning**: comparar `expected_effect` (backtest) vs `outcome_30min` (real) para detectar cuándo el backtest miente. Input para tuning del score.
- [ ] **Patch rollback automático**: si outcome 30min es mucho peor que expected, revertir.
- [ ] **Auto-promotion de insights** (post-MVP): auto-mover patrones detectados desde anti_evidence_log a learnings.md sin confirmación humana. Postpuesto por riesgo.
- [ ] **Outcome window adaptativo**: en vez de 30 min fijo, ajustar según tipo de cambio (take_profit más largo, spreads más corto).
- [ ] **Audit_log split**: separar proposals.jsonl y outcomes.jsonl para evitar reescrituras costosas.
- [ ] **Métricas del agente**: dashboard sobre audit_log (% aprobaciones, match_quality, controllers más cambiados).
