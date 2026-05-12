# LEARNINGS — memoria propia del agente entre sesiones

> Esto NO es la memoria del agente adaptativo (esa vive en
> `MEMORY_SPEC.md` y `state/`).  Esta es **mi** memoria — del agente que
> está construyendo el framework — para no repetir errores entre
> sesiones.  Cada vez que el usuario me corrige o que descubro algo del
> repo de la mala manera, lo registro acá.
>
> **Lectura obligatoria al arranque de cada sesión** del trabajo en este
> framework.  Si el usuario me pide algo y me suena familiar, primero
> grep acá.

## Convenciones

- Una entrada por lección.  Fecha, título corto, qué pasó, qué hacer.
- Si una lección queda obsoleta (cambio de versión, refactor, etc.),
  la marco como `RETIRED` con la razón, no la borro.
- Mantenelo curado — si crece a 50 entradas perdés el efecto.
  Consolidá patrones recurrentes en lecciones más amplias.

---

## Active learnings

### L1 — `controller_id` en Hummingbot no es lo que parece (2026-05-10)

**Qué pasó.** Asumí que `cfg.get("id")` o `cfg.get("controller_id")`
estaban poblados en el response de
`client.controllers.get_bot_controller_configs(bot_name)`.  No lo
están.  La key real que el server inyecta es `_config_name` (el
filename del YAML sin extensión).

**Síntoma**: la routine `capital_state` reportaba `Capital: $0` para
los 14 controllers porque el lookup `perf_by_id.get(config_name)`
devolvía `None` y nunca se sumaban posiciones.

**Qué hacer.**
- Para identificar un controller: `cfg.get("_config_name")` (con
  fallback a `cfg.get("id")` si lo necesita).
- Cuando hace falta matchear `cfg` con su `performance` del MQTT,
  probar varias keys en orden y caer al fallback de
  `(connector_name, trading_pair)` (helper `_resolve_perf` en
  `routines/capital_state.py`).
- El identificador canónico que adoptó el framework es
  `{bot_name}::{_config_name}` (ver F1 en
  `FIXES_2026-05-10.md`).

### L2 — Telegram trunca routine results a 250 chars dentro de un fence MarkdownV2 (2026-05-10)

**Qué pasó.** Diseñé el output de `capital_state` con un cuerpo
markdown rico (headers, blockquotes, ASCII bars) seguido de un bloque
` ```json ` con el payload completo.  El handler de Telegram
(`handlers/routines/__init__.py:_refresh_detail_msg`) toma el
resultado, lo trunca a `result[:250]`, y lo embebe en otro fence
` ``` `.  Resultado: triple-backticks anidadas que rompen el parser
de MarkdownV2 → preview en blanco.

**Síntoma**: el usuario reportó "lo único que hace es cambiarme el
server default" porque el render fallaba silenciosamente.

**Qué hacer.**
- `RoutineResult.text` debe ser **una sola línea ASCII pura**
  (ningún backtick, ningún caracter MarkdownV2 sin escapar) y
  **fácil de truncar**: la info más importante primero.
- Para el detalle rico, usar `RoutineResult.sections` (KPIs) o
  `table_data`+`table_columns`.  Esos los renderiza el frontend
  web.
- Para "vistas extensas" en Telegram (no detalle de instancia),
  enviar un mensaje aparte con `context.bot.send_message(...)`
  cuando `target_chat_id` está configurado.

### L3 — La "pantalla nueva" de routines en la web UI usa `ReportBuilder`, NO `RoutineResult` (2026-05-10)

**Qué pasó.** Cuando el usuario abrió `/routines` → `capital_state`
en la web UI nueva, vio "No reports yet · Run for the first time".
Yo había implementado todo con `RoutineResult.sections` (KPIs) y
`table_data` pensando que el frontend lo iba a renderizar.

Es cierto que el frontend tiene un componente `RoutineResultView`
que sí renderiza KPIs y tablas… pero ese es para la **vista de
detalle de instancia** (la vieja).  La **pantalla principal de
routine en la web UI** muestra la lista de "reports" — un sistema
distinto, persistido en disco como HTML por `ReportBuilder`.

Un `RoutineResult` rico nunca aparece en la pantalla nueva si la
routine no llamó a `builder.save()` explícitamente.

**Síntoma**: el botón "Run" cliquea, el server log dice solo
`Set active server to brigado` (otra acción separada), y la pantalla
sigue mostrando "No reports yet".

**Qué hacer.**
- Para que un Run aparezca en la pantalla nueva: usar
  `ReportBuilder` desde la routine.  Patrón:
  ```python
  from condor.reports import ReportBuilder
  builder = ReportBuilder("Mi título")
  builder.source("routine", "<nombre>").tags([...])
  builder.kpi("Label", "value", trend="up"/"down")
  builder.markdown("…")
  builder.table(rows)
  builder.save()
  ```
- Ejemplos vivos: `routines/arb_check.py`, `routines/market_scanner.py`,
  `routines/pmm_mister_supervisor.py`.
- `RoutineResult` sigue siendo útil para Telegram (`text`) y para
  la vista de detalle de instancia (legacy), pero **no es lo que el
  humano ve en la web UI nueva por defecto**.

### L4 — Validación end-to-end: escribir el código no es validar (2026-05-10)

**Qué pasó.** Implementé `capital_state` con tests que pasaban,
smoke tests con datos sintéticos, etc.  Asumí que iba a funcionar en
producción.  Cuando el usuario lo probó:
- El committed era $0 (bug L1).
- El preview de Telegram no mostraba nada útil (bug L2).
- La pantalla nueva mostraba "No reports yet" (bug L3).

Cada uno requirió una iteración con el usuario para diagnosticar.

**Qué hacer.**
- **Antes de pedirle al usuario que pruebe**, validar contra un
  servidor real con la skill `condor-express`.  Cualquier
  asunción sobre el shape de un response del API se debe verificar
  contra datos reales.
- Si la pantalla donde el resultado va a aparecer no es la de
  detalle de instancia, **mirar específicamente cómo el frontend
  rinde lo que estoy escribiendo** (ver `RoutineResultView.tsx`,
  `RoutineReports.tsx`, etc.).  No asumir que "el frontend lo va
  a parsear bien".

### L5 — `WebRoutineContext` con `chat_id=0` no respeta `active_server` del user_data (2026-05-10)

**Qué pasó.** En el smoke test del CLI dashboard, instancié
`WebRoutineContext(server_name='brigado')` y la routine corrió contra
el server `local` igual.

**Causa**: `config_manager.get_client(chat_id, ...)` resuelve el
servidor preferido a partir del `chat_id` y del `user_data`.  Con
`chat_id=0` y sin `user_id`, la cadena de resolución cae en el
default global del config_manager, ignorando el `active_server` que
había seteado en el user_data.

**Qué hacer.**
- Para invocar una routine fuera de Telegram contra un server
  específico, usar `condor.tools.condor_inspect` (que llama
  `get_config_manager().get_client(server_name)` directo, sin pasar
  por la lógica de `chat_id`).
- O pasar `chat_id` real Y `user_id` real al `WebRoutineContext`.
- En la web UI esto no es problema porque el endpoint
  `/api/v1/routines/run` recibe `server_name` en el body y lo pasa
  explícitamente al `WebRoutineContext`.

### L6 — Reglas de commits del usuario (2026-05-10)

- **Nunca** `git add .` o `git add -A`.  Siempre archivos por nombre.
- **Commits atómicos por intención**: un commit = una unidad lógica.
  Si modifiqué dos cosas conceptualmente distintas, son dos commits.
- **Mensajes con cuerpo**: subject corto (`tipo(scope): qué`) + cuerpo
  explicando el "por qué", no solo el "qué".
- **Footer obligatorio**: `Co-Authored-By: Claude Opus 4.7
  <noreply@anthropic.com>`.
- **No mergear a main**.  Trabajo en branch `feat/pmm_mister_supervisor`.
- **No tocar archivos ajenos** (`frontend/package-lock.json`,
  `Dockerfile.api`, `routines/*.py` que ya estaban en el repo, etc.).

### L7 — Comunicación intermitente sesga el debugging (2026-05-10)

**Qué pasó.** Cuando un bug requiere ver logs reales del server,
ir y venir entre yo (sin acceso al server) y el usuario (que tiene
que correr cosas y pegar logs) hace que cada iteración tarde y se
sesgue por lo que el usuario recuerda incluir.  Llegamos a iterar
2-3 rondas para debuggear `committed=$0`.

**Qué hacer.**
- Cuando aparezca un bug que requiere datos del server real, usar
  **inmediatamente** `condor-express` (skill local) para obtener los
  datos sin pedírselos al usuario.
- Si la skill no alcanza, armarle al usuario un **prompt completo
  para Condor** (el agente Telegram) en lugar de pedirle un dato a
  la vez.  Que Condor mismo investigue, fixee y reporte de vuelta.

### L8 — El usuario tiene memoria sobre mí; yo no (2026-05-10)

**Qué pasó.** Cuando aparece un bug recurrente, el usuario lo nota:
"esto ya pasó antes".  Yo no — cada sesión arranco de cero leyendo
TASKS, DECISIONS, etc., pero no tenía un lugar donde anotar lo que
aprendí _hacer mal_.

**Qué hacer.**
- Mantener este `LEARNINGS.md` actualizado.  Cada vez que el usuario
  me corrige o descubro algo a la mala, agregarlo.
- Al arranque de cualquier sesión sobre este framework, leer este
  archivo antes de tocar nada.

---

### L9 — `ReportBuilder` reordena las secciones por defecto (2026-05-12)

**Qué pasó.** Implementé el report multi-pair de `market_regime` con la
secuencia `## PAIR → tabla TF → ## Niveles cercanos → ## Niveles macro`
para cada par. Al verlo en la web UI, todas las tablas aparecían
agrupadas juntas (sin headers que las identificaran) y todo el
markdown agrupado en otro bloque. Parecía un bug aleatorio pero era
determinístico: `ReportBuilder._render_sections()` por defecto hace
`sorted(sections, key=type_priority)` con orden `kpi → plotly →
table → markdown`. Insertar en cierto orden no implica que se
renderice en ese orden.

**Síntoma**: usuario reporta "las tablas no expresan a qué trading pair
se refieren, y hay una falsa navegación con secciones de texto".

**Qué hacer.**
- Siempre que el orden de las secciones importe (multi-bloque,
  per-entity sub-secciones), llamar `builder.manual_order()` apenas
  se instancia.
- Como red de seguridad: si una tabla está semánticamente atada a
  una entidad (par, controller, etc.), incluir esa entidad como
  primera columna de la tabla. Así sigue siendo legible aunque algo
  reordene el HTML.
- TOC con anchors markdown (`[label](#header-id)`) **no funciona**
  con el render actual — los `<h2>` no llevan `id` autogenerado. Si
  hace falta navegación verdadera, va a requerir extender
  `ReportBuilder` (no improvisar con markdown plano).

### L10 — Reportes generados con datos sintéticos contaminan la web UI (2026-05-12)

**Qué pasó.** Para validar `_save_report` corrí un smoke con
`make_candles(price=100.0)` (fixture del test). El report se persistió
en `reports/` y apareció en el UI como si fuese un Run real, con
"Niveles macro: Máx 7d 101.0000, Mín 90d 99.0000" — números que no
existen para BTC. Usuario detectó la inconsistencia comparando con
TradingView.

**Síntoma**: report con valores absurdos pegado en disco junto a los
reales, indistinguibles para el humano.

**Qué hacer.**
- **Nunca** llamar a `_save_report(...)` con fixtures sintéticos
  apuntando a `reports/` real. Si hace falta probar el render,
  monkey-patch el path (env var, `tempfile.TemporaryDirectory()`),
  o saltearse la persistencia y solo verificar el `RoutineResult`.
- Convención del proyecto: `reports/` contiene **solo** outputs de
  Runs reales contra servidores reales.
- Si por error se contamina la carpeta, borrar la entrada (también
  del `reports_index.json` si quedó indexada) antes de pedirle al
  usuario que valide nada.

---

### L11 — `validate_controller_config` rechaza `_config_name` que el GET inyecta (2026-05-12)

**Qué pasó.** Al construir el config mergeado para pasar a
`client.controllers.validate_controller_config(...)` en
`update_controller_config`, el endpoint rechaza el cuerpo entero con
`extra_forbidden` sobre el campo `_config_name`. Causa: `get_bot_controller_configs`
**inyecta** `_config_name` (el filename del YAML) en el response, pero
el modelo Pydantic `PMMisterConfig` del server NO lo declara y tiene
`model_config = ConfigDict(extra="forbid")`. Asimétrico.

**Síntoma**: pre-validation endpoint devuelve HTTP 400; si el tool
abortara al fallar la validación, el agente no podría escribir nada.

**Qué hacer.**
- El tool `update_controller_config` ya implementa "graceful degrade":
  loggea warning, sigue al apply. El apply funciona porque pasamos
  solo `{field: value}` y el endpoint de update no tiene `extra="forbid"`.
- **No** depender de `validate_controller_config` para correctness:
  tratarlo como una optimización (fast-fail) que puede no estar
  disponible. La validación de tipos primaria sigue siendo el
  type-coercion local + el merge shallow del server.
- Antes de pulir esto, ver si conviene cleanear `_config_name` (y
  cualquier otro `_*`-prefixed) antes de mandar al validate — pero es
  riesgoso porque podría haber más campos inyectados que no conocemos.
  Mantener el graceful-degrade es la opción más robusta.

---

### L12 — Backtest endpoint también rechaza `_config_name` y responde error con HTTP 200 (2026-05-12)

**Qué pasó.** Durante el smoke E2E de Fase 5.7, el `backtest_loop`
mandaba el config completo (tal como vino de `get_bot_controller_configs`)
al endpoint `/run-backtesting`. El response fue `{"error": "..."}` con
HTTP 200, no excepción.

Dos cosas pasaron al mismo tiempo:
1. **Mismo issue que L11**: el server inyecta `_config_name` en el GET
   pero el modelo Pydantic del backtest tiene `extra="forbid"` y lo
   rechaza con `extra_forbidden`.
2. **El endpoint no levanta excepción** ante este tipo de error de
   validación — devuelve HTTP 200 con `{"error": "<msg>"}`. Diferente
   del `validate_controller_config` que sí levanta HTTP 400 (L11).

**Síntoma**: cycle terminó con baseline `pnl=null`, todos los
candidatos con `pnl=null`, verdict `no_valid_candidates`. Sin errors
en el log porque ningún paso "falló" desde el punto de vista del
loop.

**Qué hacer.**
- `backtest_loop._run_backtest_cached` ahora strip cualquier campo
  que empiece con `_` antes de mandar al endpoint. Cubre
  `_config_name` y deja preparado el caso de futuros campos
  inyectados con el mismo patrón.
- También detecta `{"error": "..."}` en la respuesta (sin `results`)
  y lo convierte en `BacktestError` propio, así el verdict refleja
  el problema correctamente.
- **No cachear errores**: si la API falla, queremos reintentar la
  próxima vez. La cache solo recibe responses exitosos.

Después del fix: smoke E2E contra brigado funciona. Baseline + c1 +
c2 corrieron, devolvieron PnL real, el verdict se clasificó como
`all_candidates_negative_pnl` (la última hora fue mala para todos los
TPs en BTC-USDT). El sistema funcionó correctamente; el resultado
operativo (no_action) fue el correcto para esos datos.

---

## Retired learnings

(ninguno aún)
