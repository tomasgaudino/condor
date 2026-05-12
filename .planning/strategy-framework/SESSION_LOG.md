# Session Log — bitácora cronológica del proyecto

> Append-only. Una entrada por sesión de trabajo intensa. Capturar
> **al cierre del día**, mientras el contexto está fresco. La entrada
> de la sesión más reciente es lo primero que se lee al arrancar la
> próxima sesión (junto con `LEARNINGS.md` y `TASKS.md`).
>
> Las entradas son cronológicas más nuevas primero (orden inverso).
>
> Convenciones:
> - Resumen ejecutivo arriba (lo que un compañero leería en 30s).
> - Decisiones operativas con su _por qué_.
> - Fricciones / aprendizajes con link a LEARNINGS.md cuando aplica.
> - Estado al cerrar (qué quedó sólido, qué pendiente).
> - "Para retomar mañana": recomendación concreta, no genérica.

---

## 2026-05-12 · market_regime + controller_performance + MCP tool + agent dir (5.2 → 5.5)

**Contexto inicial**: ayer cerramos capital_state end-to-end y el sistema
de supervivencia entre sesiones. Hoy avanzamos 4 fases del MVP en una
sola sesión: market_regime (5.2), controller_performance (5.3), la MCP
tool update_controller_config (5.4), y el agent dir adaptive_pmm con
su loader de invariants (5.5).

### Lo que se hizo

1. **Fase 5.2 — market_regime multi-pair** (commits `3c4ea4f`, `496af10`, `32380ac`, `cea7fab`, `8e6185f`, `ffd4cd3`).
   - Contrato user/agent fijado en AGENT_VS_USER_VIEW.md ANTES de codear
     (regla 4 del doc — primera vez aplicada en serio).
   - Módulo de indicadores numpy/scipy (NATR, ATR, ADX, Bollinger, EMA
     slopes, Hurst R/S, linreg) con 34 tests. Iteré el Hurst hasta
     entender que va sobre log returns, no precio.
   - Routine multi-pair con autodetect desde controllers activos
     (default), override explícito via `trading_pairs: list[str]`.
     Reporte único agregado: 4 KPIs + tabla resumen ordenada por
     severidad + sub-sección por par con narrativa + niveles cercanos
     S/R + niveles macro + glosario. Agent payloads per-pair
     (~560 chars) + summary cross-pair (~720 chars).
   - Smoke contra brigado: detectó BTC-BRL, BTC-USDT, TON-USDT, los 3
     en 🔴 adverse en este momento (BTC bajando ~1.23% diario).

2. **Bug visual del report multi-pair → fix + LEARNINGS L9/L10**
   (commits `ffd4cd3`, `5fea9f6`).
   - User reportó tablas sin label de par y "falsa navegación" con TOC
     markdown anchors.
   - Root cause: `ReportBuilder._render_sections` por defecto reordena
     por tipo (kpi → plotly → table → markdown), desacoplando tablas
     de sus headers.
   - Fix: `builder.manual_order()` + columna `pair` como primera de
     cada tabla TF (safety net) + integración de niveles + S/R dentro
     del mismo markdown block del par + remoción de la TOC falsa.
   - L9 anotada (manual_order siempre que el orden importe) + L10
     (no contaminar reports/ con datos sintéticos — el 100/101 del
     gráfico de BTC fue un fixture mío que se persistió y confundió).

3. **Fase 5.3 — controller_performance** (commit `ca461c9`).
   - Smoke previo descubrió que el response real NO tiene
     `total_positions` ni `accuracy` (spec asumía). Derivamos
     `positions_closed = sum(close_type_counts)`, `accuracy=null` en MVP.
     `close_type_counts` viene como `"CloseType.TAKE_PROFIT"` —
     normalizamos a `take_profit`.
   - Multi-controller con autodetect, mismo patrón que market_regime.
     Ventanas 1h/6h/24h via state_io JSONL (30d rotation).
     Cold-start F3 (cold_start <30min, adaptive 30min-4h, normal ≥4h).
     `market_share_24h` via `get_candles` 1h × 24.
   - 48 tests + smoke contra brigado: 14 controllers detectados, agent
     payloads 900-916 chars per-ctrl + 462 chars summary. Verifico
     que el JSONL persistido coincide con el snapshot en vivo.

4. **Fase 5.4 — MCP tool update_controller_config** (commit `e88bd17`).
   - Wrapper estricto sobre `update_bot_controller_config` con
     whitelist (silent-write protection), blacklist absoluta (D7),
     type coercion contra `type(old_value)`, optimistic-lock con
     tolerancia float, caveats automáticos.
   - Atomicidad confirmada: idempotent write contra brigado dejó los
     34 campos del controller bit-idénticos.
   - 6 paths del algoritmo validados contra brigado en vivo
     (success, field_forbidden, field_not_updatable,
     value_changed_externally, bot_not_found, controller_not_found).
   - L11 nueva: el endpoint `validate_controller_config` rechaza el
     `_config_name` que `get_bot_controller_configs` inyecta —
     `extra_forbidden` asimétrico. El tool ya implementa graceful-
     degrade (log warning, proceder al apply); la lección documenta
     no "arreglar" eso abortando en la validación.
   - 48 tests (12 metadata + 36 tool).

5. **Fase 5.5 — agent dir + invariants loader** (commit `91fc011`).
   - `trading_agents/adaptive_pmm/` con `agent.md` (frontmatter
     compatible con StrategyStore + body con scope/routines/policy/
     mode/output format de D5), `invariants.yaml` (24 allowed, 11
     forbidden, 7 overrides, cooldowns, capital, modes), `state/`
     con README + .gitkeep.
   - `condor.trading_agent.adaptive.invariants` loader con cross-check
     contra MCP tool metadata (F5). Detecta drift silent-write +
     forbidden mismatch + fields sin type. Acepta la asimetría
     intencional de `leverage` (humano-only por política aunque sea
     `is_updatable`).
   - Nuevo bucket `categorical_type` en field_type_map para fields
     toggle (tick_mode, position_profit_protection, *_order_type).
   - 36 tests del loader. Condor autodescubre el nuevo agente
     ("Adaptive PMM Supervisor", `adaptive.mode=propose`).

### Decisiones operativas

- **Multi-entity por default**: las 3 routines del framework (capital_state,
  market_regime, controller_performance) emiten vista agregada para humano
  + per-entity agent payloads. El engine futuro filtra por
  `title.startswith("agent:")`. Esto permite que el agente itere por
  controller sin recibir un payload monstruoso.
- **Atomicidad via shallow merge**: validada contra brigado escribiendo
  el mismo valor que ya tenía — 33 campos restantes intactos. Base
  fundacional del MCP tool: pasar `{field: value}` (single-field) es
  atómico naturalmente.
- **Categorical fields en invariants**: agregamos un tercer bucket
  (`categorical_type`) para fields toggle. El expander determinístico
  de D9 va a tratarlos como `direction=switch, magnitude=n/a` en lugar
  de escalarlos con delta. Decisión tomada al cierre de 5.5.
- **`policy.md` separado descartado**: el spec lo mencionaba pero el
  spec mismo embebe la policy en el body de agent.md. Mantenido todo
  en un solo archivo, más simple para el wizard que va a editar la
  sección "User-defined rules".
- **Estado en root vs agent dir**: routines persisten en
  `state/<routine>/...` (repo root) por ahora. Cuando llegue el agent
  loop (5.6), migrar a `trading_agents/adaptive_pmm/state/<routine>/`.
  Documentado en `trading_agents/adaptive_pmm/state/README.md`.

### Fricciones / aprendizajes

1. **Hurst R/S sobre precio vs returns**. Aplicarlo al precio da H≈0.9
   para un random walk porque integra el ruido. La convención del
   estimador es sobre log-returns. Tres iteraciones para entenderlo.
   Anoté en docstring para futuras-yo.

2. **ReportBuilder reordering** (L9). El usuario me ahorró tiempo
   reportando el render roto con screenshot — diagnosticar a partir
   de "veo tablas sin header" me llevó directo a `sorted(sections,
   key=_SECTION_PRIORITY)`. La columna `pair` como red de seguridad
   es la lección operativa: cuando una tabla está semánticamente
   atada a una entidad, prefijar la entidad.

3. **Contaminé reports/ con fixture sintético** (L10). Smoke local
   con `make_candles(price=100.0)` se persistió como report real en
   la web UI. Usuario lo detectó comparando con TradingView. Lección:
   tempfile para tests que tocan el sistema de archivos compartido,
   o saltearse `_save_report` y solo verificar el `RoutineResult`.

4. **`validate_controller_config` rechaza `_config_name`** (L11). El
   GET lo inyecta, el VALIDATE lo rechaza con `extra_forbidden`.
   Asimetría del server. El graceful-degrade del tool (log + proceder
   al apply) es la respuesta correcta — si el tool abortara en
   validation_failed, el agente no podría escribir nada.

### Estado al cerrar

- Branch: `feat/pmm_mister_supervisor`.
- 354/354 tests passing (era 136 al arranque de hoy, +218).
- 10 commits ahead de `drupman/feat/pmm_mister_supervisor` (push hecho
  al final del handoff).
- Working tree limpio salvo `frontend/package-lock.json` (archivo ajeno
  que el frontend siempre toca; ignorar — L6).
- Fase 5: 5/7 done (5.1 ✓ 5.2 ✓ 5.3 ✓ 5.4 ✓ 5.5 ✓ · 5.6 5.7 pendientes).
- 11 LEARNINGS activos (L1-L11).
- Agente nuevo `Adaptive PMM Supervisor` autodescubrible por
  StrategyStore con `adaptive.mode=propose`.

### Para retomar mañana

**El próximo paso es Fase 5.6 — modo `propose`** (handler de Telegram
con inline buttons). Es la pieza orquestadora que junta TODO lo
construido:

1. Tick del agente (cada 600s — `frequency_sec` del default_config).
2. Llamar a las 3 routines, extraer `agent:summary` + per-entity views.
3. Construir el prompt: `agent.md` body + capital_state.agent +
   market_regime.agent:<pair> + controller_performance.agent:<controller>.
4. Parsear la respuesta JSON del LLM (no_action | propose).
5. Validar cada propuesta contra `invariants.yaml` (whitelist,
   deltas, cooldowns, capital). Usar
   `condor.trading_agent.adaptive.invariants` + el loader.
6. Pasar las que sobrevivan al **Backtest Evidence Loop** (Fase 2.5,
   ya especificado en BACKTEST_LOOP_SPEC.md).
7. Para las que pasen el backtest: mensaje a Telegram con
   `propose_chat_id` + inline buttons `[Apply <winner>] [Apply <alt>]
   [Reject] [Snooze]`. PROPOSE_MODE_SPEC.md tiene el layout exacto.
8. Handler de callbacks: aplica via la MCP tool de 5.4 + escribe al
   `state/audit_log.jsonl`.

**Antes de codear 5.6**, agendaría el routine `controller_performance`
en la web UI con `Schedule` para que vaya acumulando snapshots. En 4h
sale de cold_start y va a tener velocidades reales que el agente
realmente pueda usar para detectar suboptimal. Sin eso, el primer Run
del agente va a tirar `no_action` por cold_start en todos los
controllers.

### Commits del día

- `3c4ea4f` docs(planning): lock market_regime user/agent contract before coding
- `496af10` feat(adaptive): numpy-only technical indicators for market_regime
- `32380ac` feat(routines): market_regime — multi-timeframe regime classifier
- `cea7fab` docs(planning): mark phase 5.2 (market_regime) done in TASKS.md
- `8e6185f` feat(routines): market_regime — multi-pair with autodetect
- `ffd4cd3` fix(routines): market_regime multi-pair report layout
- `5fea9f6` docs(planning): LEARNINGS L9 + L10 from today's UI rendering issues
- `ca461c9` feat(routines): controller_performance — phase 5.3 done
- `e88bd17` feat(mcp): adaptive update_controller_config — phase 5.4 done
- `91fc011` feat(agent): adaptive_pmm agent dir + invariants loader — phase 5.5 done

---

## 2026-05-10 · capital_state end-to-end + sistema de supervivencia entre sesiones

**Contexto inicial**: arrancamos el día con el diseño completo (15 specs)
y el primer commit de `capital_state.py` ya hecho la sesión anterior.
Cerramos el día con el slice de capital_state **funcional end-to-end en
producción** (server `brigado`, 14 controllers reales) y la convención
"agent view vs user view" establecida para todas las routines futuras.

### Lo que se hizo (orden cronológico aproximado)

1. **Diagnóstico y fix del bug `Committed=$0`** (commits `70d5097`, `0dc3134`).
   - El matching `perf_by_id.get(_config_name)` fallaba en producción
     porque el shape real de `positions_summary` no usa `current_value`
     sino `amount × breakeven_price`. Causa: documentado en LEARNINGS L1.
   - Solución: helper `_resolve_perf` con probe de 4 keys + fallback por
     `(connector, pair)` + logging estructurado del path de matching.

2. **Telegram UI inutilizable** (commits `161b280`, `5542f58`).
   - El handler trunca el output a 250 chars dentro de un fence
     MarkdownV2. Mi RoutineResult mezclaba un cuerpo markdown rico con un
     bloque ` ```json ` embebido — triple-backticks anidados rompían el
     parser. LEARNINGS L2.
   - Solución: 1 línea ASCII para Telegram + RoutineResult con
     `sections=["kpi" cards + "data" payload]` + `table_data`.

3. **Web UI rediseñado para aprovechar el frontend** (commit `f318d72`).
   - El frontend tiene `RoutineResultView` que ya rinde KPIs (cards)
     y `table_data` (HTML tables con coloreo numérico automático).
   - Migré `_render_compact_summary` a una sola línea + agregué
     `_build_kpi_sections` (4 cards) y `_build_controllers_table`
     (1 fila por controller, ordenada por util%).

4. **Pero la pantalla nueva de routines no muestra `RoutineResult`**.
   - Cuando el usuario probó `/routines → capital_state → Run` en la web,
     veía "No reports yet · Run for the first time" aunque el Run sí
     ejecutaba. La pantalla nueva lee de `condor.reports.ReportBuilder`,
     no de `RoutineResult`. LEARNINGS L3 (la más cara del día).
   - Solución: `_save_report(payload)` con `ReportBuilder.kpi() / .markdown() / .table() / .save()`.

5. **Narrativa + glosario en lenguaje natural** (commit `7f4f43d`).
   - El usuario pidió un párrafo de resumen interpretable arriba del
     report y un glosario al final con los términos relevantes a este
     snapshot. `_build_narrative` (1-4 frases con status 🟢/🟡/🔴
     determinístico) + `_build_glossary_markdown` (filtro contextual de
     9 términos: solo aparecen los que el snapshot necesita).

6. **`condor_inspect` + skill `condor-express`** (commits `1fc9d55`,
   `3235466`, `b7580f9`).
   - Para no depender del usuario para datos en vivo durante debug, agregué
     `condor.tools.condor_inspect` con 8 helpers + skill que documenta
     cómo invocar a un servidor brigado/local desde scripts ad-hoc. Lo
     usé en vivo durante el día (validó el bug de utilization=745% del
     controller btcusdt-1-5). LEARNINGS L7.

7. **`LEARNINGS.md`** (commit `30a1909`).
   - El usuario me señaló que ciertos errores se repetían entre
     sesiones porque yo no tengo memoria. Creé `LEARNINGS.md` con 8
     lecciones activas. TASKS.md y `/avance` ahora lo marcan como
     lectura obligatoria al arrancar.

8. **Agent vs User view** (commits `f924d26`, `2fe8c4a`).
   - Antes de cerrar el día: separé el output en dos vistas.
     `_build_agent_payload(payload)` devuelve un dict mínimo (~5 KB para
     14 controllers) sin narrativa, sin glosario, sin KPIs duplicados,
     sin inventory/history — solo los campos que las decisiones del agente
     consultan. Documentado como contrato vivo en
     `AGENT_VS_USER_VIEW.md`.

9. **Sistema de supervivencia entre sesiones** (commits `a36e516`,
   `c8c952c`, `383718b`).
   - Pregunta del usuario al cierre: "¿cómo me aseguro de que mañana
     hagamos exactamente este mismo proceso?". Identificamos que el
     contexto interpretativo (no las specs, que ya sobreviven en disco)
     se perdía entre sesiones.
   - Solución de tres comandos:
     * `/onboard` — protocolo de arranque determinístico (lee
       LEARNINGS, último SESSION_LOG, TASKS, verifica git/tests,
       detecta gaps si la sesión anterior cerró sin handoff).
     * `/avance` (extendido) — incluye recordatorio activo apuntando
       a `/handoff` cuando detecta cierre próximo.
     * `/handoff` — ritual de cierre que automatiza la metadata
       determinística (commits del día, status, tests) y solo pide
       al humano las 3 cosas interpretativas (decisiones, fricciones,
       "para retomar mañana"). Persiste y pushea a `drupman`.
   - `SESSION_LOG.md` introducido como bitácora cronológica
     append-only, una entrada por sesión productiva.

10. **Remote `drupman` agregado y push inicial** (operación, sin commit).
    - `drupman` → `https://github.com/tomasgaudino/condor` (fork del
      usuario). Branch `feat/pmm_mister_supervisor` ahora respaldada
      ahí y el upstream local apunta a `drupman` para que `git push`
      futuros vayan al fork por defecto, no al `origin/hummingbot`.

### Decisiones operativas

- **`controller_id` canónico** = `{bot_name}::{_config_name}`. Era ambiguo
  (D-FIXES F1), ahora explícito en código + tests.
- **`stuck_suspect` heurística**: `take_profit ≤ 5 bps AND ≥1 posición
  con `unrealized_pnl_quote ≥ 0` que no cerró. Tentativa, false positives
  posibles, por eso "stuck?" con interrogación.
- **Convención dos vistas por routine**: cada routine del framework MVP
  debe emitir `user view` (rica) y `agent view` (mínima) en `RoutineResult`
  separadas. Documentado en `AGENT_VS_USER_VIEW.md`.
- **Severity ladder de oversub**: `info` (<1×), `warn` (1–1.5×), `crit`
  (≥1.5×). El compact summary solo muestra crit; warn queda implícito.
- **Reglas de commit ratificadas**: nunca `git add .`, atómicos por
  intención, footer `Co-Authored-By: Claude Opus 4.7`, no mergear a main.

### Fricciones / aprendizajes

8 lecciones nuevas en `LEARNINGS.md` (todas marcadas hoy 2026-05-10):

| # | Título corto |
|---|---|
| L1 | `controller_id` en HB no es lo que parece (`_config_name`, no `id`) |
| L2 | Telegram trunca a 250 chars dentro de MarkdownV2 — `text` debe ser 1 línea ASCII |
| L3 | La pantalla nueva de routines usa `ReportBuilder`, NO `RoutineResult` |
| L4 | Tests passing ≠ end-to-end working (validar contra server real con `condor-express`) |
| L5 | `WebRoutineContext(chat_id=0)` ignora `active_server` del user_data |
| L6 | Reglas de commits del usuario (sin `git add .`, atómicos, etc.) |
| L7 | Comunicación intermitente sesga debugging — usar `condor-express` o prompt full a Condor |
| L8 | El usuario tiene memoria sobre mí; yo no — mantener LEARNINGS.md |

**Momentos donde el usuario me corrigió con razón**:
- Cuando quise hacer `git add .` (commit 1, evitado a tiempo).
- "No entiendo de qué me sirve esto" — el output no era accionable.
- "Lo que ves vos no es lo que veo yo" — yo asumía Telegram como UI principal cuando él tenía la web abierta.
- "¿Vos tenés un learnings propio?" — la pregunta que originó este sistema.

### Estado al cerrar (snapshot final del día)

- Branch: `feat/pmm_mister_supervisor` (no mergeada, **pusheada a
  `drupman`** que es el fork del usuario en
  `github.com/tomasgaudino/condor`).
- Tests: **136/136 pasan** en 0.33s.
- Commits del día: **36 atómicos** (todos en la branch).
- `capital_state` end-to-end funcionando en producción contra
  `brigado` (server con 14 controllers `pmm_mister`):
  - 1 línea ASCII para Telegram preview.
  - HTML report (`ReportBuilder`) con KPIs + narrativa determinística
    en español + glosario contextual + tabla en la pantalla web nueva.
  - Agent view en `sections[type=data, title=agent]` (~5 KB) listo
    para consumir por el engine del agente cuando se construya.
  - CLI dashboard (`python -m condor.tools.dashboard`) sigue
    funcionando vía la sección de payload completo.
- Specs: 22 docs en `.planning/strategy-framework/` (incluyendo
  `SESSION_LOG.md` y `AGENT_VS_USER_VIEW.md` nuevos).
- Tooling Claude: 3 slash commands (`/onboard`, `/avance`,
  `/handoff`) + 1 skill (`condor-express`).

### Para retomar mañana

**Primer paso al arrancar**: invocar `/onboard`. Va a leer
LEARNINGS.md, esta entrada, TASKS.md, verificar git status y tests.
Sale en <2 minutos con un reporte de contexto cargado.

**Punto natural de continuación**: Fase 1.2 — `routines/market_regime.py`.
Ya tiene spec completa en `MARKET_REGIME_SPEC.md`.

Antes de implementar, **leer obligatoriamente**:
1. `LEARNINGS.md` (8 lecciones).
2. Última entrada de `SESSION_LOG.md` (esta misma).
3. `AGENT_VS_USER_VIEW.md` — armar la sección de `market_regime` ahí
   ANTES de codear (regla establecida hoy: documentar el contrato
   de las dos vistas primero, después implementar).
4. `MARKET_REGIME_SPEC.md` para el qué.

**Validación recomendada antes de implementar**: usar `condor-express`
para inspeccionar las velas que devuelve `client.market_data` contra
`brigado` y verificar el shape real (timestamps, volume, formato). No
asumir desde la spec, validar desde código real (LEARNINGS L4).

**Riesgo operacional pendiente**: el wallet de `brigado` está al 97% de
utilización con headroom $-203. El framework está mostrando un setup
agresivo (14 controllers operando 7× nominal con TP de 1bp). Vale la
pena mencionárselo al usuario al arrancar — si los controllers se
claven, no es bug del framework, es la realidad de su setup actual.

**Recordatorio del ritual**: al cerrar la próxima sesión, invocar
`/handoff` para que esta misma estructura quede actualizada
automáticamente. El sistema tolera olvido único (`/onboard` detecta
gaps al día siguiente y reconstruye desde commits), pero el handoff
explícito es siempre mejor.

### Commits del día (orden cronológico)

```
fab32dc  test(adaptive): cover state_io with 30 tests
c0bd549  feat(routines): add capital_state routine
073aee6  test(routines): cover capital_state pure helpers with 33 tests
f449fe0  feat(tools): add CLI dashboard for adaptive framework
52b879a  test(tools): cover dashboard render and JSON extraction
1770bf1  docs(planning): add smoke test checklist for first vertical slice
161b280  fix(routines): make capital_state output usable in Telegram UI
20cb510  fix(tools): read capital_state payload from RoutineResult.sections
5542f58  test(routines): cover compact summary properties
70d5097  fix(routines): resolve performance dict robustly across MQTT key variants
1daa2dd  refactor(routines): refine capital_state compact summary for clarity
deb88fc  chore(routines): add temporary [diag] logging in capital_state.run
2adda16  chore(routines): also dump positions_summary[0] shape
0dc3134  fix(routines): compute committed_now_usd from amount × breakeven_price
d21147a  revert: drop temporary [diag] logging from capital_state
1fc9d55  feat(tools): add condor_inspect module for live server inspection
3235466  test(tools): cover condor_inspect pure helpers with 10 tests
b7580f9  feat(claude): add condor-express skill for live server queries
e971d42  docs(planning): add executive catchup for sharing with the team
f318d72  feat(routines): leverage web UI for capital_state output
c170cfd  feat(routines): persist capital_state output as an HTML report
30a1909  docs(planning): add LEARNINGS.md to retain mistakes between sessions
7f4f43d  feat(routines): add narrative summary + contextual glossary to capital_state
f924d26  feat(routines): emit a separate agent-facing payload from capital_state
2fe8c4a  docs(planning): add AGENT_VS_USER_VIEW.md as living routine contract
a36e516  docs(planning): start SESSION_LOG.md with today's closing entry
c8c952c  feat(claude): add /onboard command and close-of-day reminder in /avance
383718b  feat(claude): close the loop with /handoff and gap detection in /onboard
```

---
