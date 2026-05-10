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

## 2026-05-10 · Cierre del primer slice de capital_state end-to-end

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

### Estado al cerrar (snapshot 22:30 ART)

- Branch: `feat/pmm_mister_supervisor` (no mergeada, no pusheada).
- Tests: **136/136 pasan** en 0.32s.
- Commits: 27 atómicos (incluyendo los 7 de docs/planning).
- `capital_state` end-to-end funcionando en producción contra `brigado`.
  - 1 línea para Telegram preview.
  - HTML report con KPIs + narrativa + glosario + tabla en pantalla web nueva.
  - Agent view en `sections[type=data, title=agent]` listo para consumir.
  - CLI dashboard sigue funcionando.
- Specs: 21 docs en `.planning/strategy-framework/` (incluyendo el nuevo
  `AGENT_VS_USER_VIEW.md`).

### Para retomar mañana

**Punto natural de continuación**: Fase 1.2 — `routines/market_regime.py`.
Ya tiene spec completa en `MARKET_REGIME_SPEC.md`.

Antes de implementar, **leer obligatoriamente**:
1. `LEARNINGS.md` (8 lecciones).
2. Última entrada de `SESSION_LOG.md` (esta misma).
3. `AGENT_VS_USER_VIEW.md` — armar la sección de `market_regime` ahí
   ANTES de codear (regla establecida hoy: documentar el contrato
   primero, después implementar).
4. `MARKET_REGIME_SPEC.md` para el qué.

**Validación recomendada antes de implementar**: usar `condor-express`
para inspeccionar las velas que devuelve `client.market_data` contra
`brigado` y verificar el shape real (timestamps, volume, formato). No
asumir desde la spec, validar desde código real (LEARNINGS L4).

**Riesgo operacional pendiente**: el wallet de `brigado` está al 97% de
utilización con headroom $-203. El framework está mostrando un setup
agresivo (14 controllers operando 7× nominal con TP de 1bp). Vale la
pena mencionarle al usuario antes de seguir construyendo encima — si
los controllers se claven hoy, no es bug del framework, es la realidad
de su setup actual.

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
```

---
