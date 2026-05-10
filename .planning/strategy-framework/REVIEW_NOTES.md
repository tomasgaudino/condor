# Review Notes — auditoría crítica del corpus de specs

> Revisión hecha al cerrar la fase de diseño (15 specs, ~250 KB).
> Propósito: detectar inconsistencias, gaps, dependencias circulares y prioridades mal calibradas **antes** de implementar.
> Fecha: 2026-05-09

## Resumen ejecutivo

- **15 hallazgos clasificados** en 4 categorías: inconsistencias (8), gaps (10), dependencias mal ordenadas (4), prioridades mal calibradas (~10 sub-puntos).
- **7 acciones top-priority** identificadas como blockers antes de implementar.
- **8 cosas bien diseñadas** mencionadas para balance.

Severidad de los hallazgos (mi juicio, no del subagente):

| Severidad | Cantidad | Ejemplo |
|---|---|---|
| 🔴 Blocker | 3 | `controller_id` indefinido, `compute_match_score` ausente, schema drift de audit_log |
| 🟡 Importante | 7 | naming `subóptimo` con tilde, doble fuente de truth, cold-start, outcome cross-mes |
| 🟢 Nice-to-have | resto | over-engineering varios, mejoras incrementales |

---

## Hallazgos por categoría

### 1. INCONSISTENCIAS

#### 1.1 — `leverage` en whitelist y blacklist
- `MCP_TOOL_SPEC.md:262` lo incluye en `PMM_MISTER_UPDATABLE` y en `ABSOLUTE_FORBIDDEN:267`. Resuelto por precedencia, OK.
- Pero `AGENT_SCHEMA_SPEC.md` solo lo tiene en blacklist, no en whitelist → las dos listas no matchean.
- 🟡 **Acción**: unificar fuente de truth (ver 4.1).

#### 1.2 — `match_quality_avg_24h < 0.4` muy permisivo
- Significa "cuando el promedio es 'bad'", muy raro.
- 🟢 Re-evaluar threshold con datos reales.

#### 1.3 — Naming `subóptimo` con tilde en código
- 🟡 **Blocker práctico**: rompe linters ASCII, grep, serializadores estrictos.
- **Acción**: rename masivo a `suboptimal` antes de implementar.

#### 1.4 — `controller_id` ambiguo
- ¿Es el `id` Pydantic? ¿el `config_name` (filename)? ¿identificador compuesto?
- 🔴 **Blocker**: la primera llamada real va a fallar.
- **Acción**: definir formato canónico + helper `resolve_controller(controller_id)`.

#### 1.5 — `bot_name` ausente del schema de audit_log
- Pero `PROPOSE_MODE_SPEC.md` lo lee en `entry["bot_name"]`.
- 🟡 Schema drift simple de cerrar.

#### 1.6 — Cooldowns: campos `marker` y `cooldown_until` no documentados
- Inventados ad-hoc en handlers de reject/snooze.
- 🟡 Definir esquema unificado de cooldowns.

#### 1.7 — `field_type_map` clasifica segundos como `percentage_type`
- Clasificación engañosa pero funcional.
- 🟢 Renombrar a `multiplicative_type` / `additive_type`.

#### 1.8 — `expansion_table` referenciado en código pero no en YAML
- 🟢 Decidir: ¿config en YAML o constante en código?

### 2. GAPS

#### 2.1 — Pre-window snapshot no especificado
- `pnl_velocity` requiere leer snapshot histórico, no hay helper.
- 🔴 **Blocker** del disparador de D9.

#### 2.2 — `read_controller_performance_window` fantasma
- Invocado pero no definido.
- 🔴 **Blocker** del outcome loop.

#### 2.3 — `compute_match_score` sin fórmula
- Es **el corazón del feedback loop** del agente.
- 🔴 **Blocker**. Sin esto, los thresholds (0.7, 0.4) son arbitrarios.

#### 2.4 — Multi-controller: política de fairness
- Si hay 5+ controllers en subóptimo simultáneo, no hay round-robin / FIFO / prioridad.
- 🟡 Definir política antes de prod.

#### 2.5 — Bot restart edge case
- `expected_old_value` puede cambiar, `controller_id` puede cambiar, cooldowns no se invalidan.
- 🟡 Operacional MVP, va a pasar la primera semana.

#### 2.6 — `validate_no_oversub` ¿pre-expander o pre-apply?
- Ambigüedad: ¿se valida oversub al expandir cada candidato, o solo al seleccionar uno?
- 🟢 Si se valida solo al final, podemos correr 3 backtests para descartar todos.

#### 2.7 — Mapping `controller_id → (connector, pair)`
- Lookups del LLM cruzan por controller_id, pero `regime_history` es por par.
- 🟡 Helper faltante.

#### 2.8 — `telegram_message_id` no en schema de audit_log
- 🟡 Schema drift simple.

#### 2.9 — `connector_name` en patch
- Se lee freshly del config en cada chequeo, hay que documentar.
- 🟢 Trivial.

#### 2.10 — Anti-evidence streak check con pseudocódigo inconsistente
- En `BACKTEST_LOOP_SPEC.md:69-75` el comentario dice "se sabe después del LLM" pero el código de abajo lo cuenta antes.
- 🟡 Reescribir.

### 3. DEPENDENCIAS / ORDEN DE FASES

#### 3.1 — `state/io.py` no tiene tarea explícita
- Las routines lo necesitan pero no hay subtarea.
- 🟡 Agregar 5.0 = "Implementar `state/io.py` y `state/queries.py`" antes de routines.

#### 3.2 — Cold-start: agente sin 4h de historia
- `pnl_velocity_4h` no se puede calcular hasta tener 4h de samples.
- 🔴 **Blocker** real (la primera vez que se prende el agente).
- **Acción**: política de cold-start (esperar? ventana adaptativa? leer histórico previo si existe?).

#### 3.3 — Outcome loop ↔ rotación mensual
- Patch del 30/m a las 23:50 con outcome a 30 min queda en mes nuevo.
- 🟡 **Bug**: outcome se pierde para entries cross-mes.
- **Acción**: leer también el archivo del mes anterior.

#### 3.4 — Doble fuente de truth para whitelist/blacklist
- `invariants.yaml` (agente) y `_controller_field_metadata.py` (MCP tool).
- 🟡 Pueden divergir. Decidir cuál es canónica.

### 4. PRIORIDADES MAL CALIBRADAS

#### 4.1 — SOBRE-DISEÑADO para MVP

| Cosa | Por qué es over | Acción |
|---|---|---|
| Modo `auto` completo | MVP solo usa `propose` | No implementar lógica de auto en Fase 5 |
| `with_executor_details` flag | No requerido en MVP | Marcar pendiente |
| Anti-evidence streak skip | Poco volumen, raramente dispara | OK dejar como está, pero no priorizar |
| 3 ventanas (1h/6h/24h) en controller_performance | El disparador solo usa 4h y 1h vs 24h | Borrar `last_6h` o postergar |
| ~50 KPIs en KPIS_AND_DASHBOARD | OK porque lista MVP es 15 | No acción |
| Implementar shadow + auto modes | Trabajo neto sin valor hasta validar propose | Mover a fase post-MVP |

#### 4.2 — SUB-DISEÑADO / debería ser MVP

| Cosa | Por qué es necesaria | Severidad |
|---|---|---|
| `read_controller_performance_window` + `compute_match_score` | Outcome loop es MVP | 🔴 |
| Política de cold-start | Va a pasar la 1ra vez que prende | 🔴 |
| Bot restart reconciliación | Va a pasar la 1ra semana | 🟡 |
| Mapping `controller_id → (connector, pair, bot_name)` | Usado por casi todo | 🔴 |
| Definición canónica de `controller_id` | Blocker | 🔴 |
| `bot_name`, `telegram_message_id` en audit_log schema | MVP gap simple | 🟡 |

#### 4.3 — Cosas BIEN diseñadas

- **`PATCH_CONTRACT_SPEC.md` con 4 estados**: separación limpia LLM ↔ determinístico ↔ scoring ↔ apply.
- **`MCP_TOOL_SPEC.md`**: error codes bien tabulados, atomicidad argumentada, política dict vs raise clara.
- **3 planos de capital**: modelo conceptual maduro.
- **`MARKET_REGIME_SPEC.md` multi-timeframe**: propósito explícito por TF.
- **`MEMORY_SPEC.md` 3 categorías de stores**: piensa el ciclo de vida.
- **Backtest cache key con canonicalización**: detalle correcto.
- **Circuit breaker manual-only**: consistente con D7.
- **Outcome learning como feedback loop al score**: pensado a futuro.

---

## TOP 7 acciones — blockers antes de implementar

Por orden de criticidad:

1. **🔴 Definir `controller_id` canónico** + helper `resolve_controller(controller_id)`.
2. **🔴 Especificar `compute_match_score`** y `read_controller_performance_window` (gap crítico del outcome loop).
3. **🔴 Política de cold-start del agente** — primeras 4h sin historia. Tres opciones a evaluar:
   - Esperar pasivamente (no proponer hasta tener 4h).
   - Ventanas adaptativas (si hay 1h, usar 1h).
   - Levantar del histórico previo si existe (preferido).
4. **🟡 Renombrar `subóptimo` → `suboptimal`** en todo el corpus (trivial pero importante).
5. **🟡 Unificar fuente de truth de whitelist/blacklist**: `invariants.yaml` canónica + `_controller_field_metadata.py` derivado en runtime, o al revés.
6. **🟡 Cerrar schema drift de `audit_log`**: agregar `bot_name`, `telegram_message_id`, `marker`, `cooldown_until`, `apply_result`, `apply_error`.
7. **🟡 Outcome loop cross-archivo**: leer también el archivo del mes anterior para entries cuyo `applied_at + 30min` cruza el boundary.

---

## Cómo abordar esto

Tres caminos posibles:

### Opción A — Arreglar los 7 blockers ahora
Trabajo: ~1-2 sesiones. Edita los specs existentes para resolver. Output: corpus consistente, listo para implementar sin sorpresas.

### Opción B — Arreglar solo los 🔴 (3) y dejar los 🟡 para resolver durante implementación
Trabajo: ~30-45 min. Los 🔴 son blockers reales. Los 🟡 pueden detectarse y arreglarse cuando se chocan en código.

### Opción C — Implementar primero un MVP-de-MVP (ej: solo `capital_state` y CLI dashboard) y dejar que los gaps emerjan empíricamente
Trabajo: empezamos a codear ya. Los problemas se descubren con datos reales. Riesgo: deuda técnica grande si los gaps son estructurales.

**Mi recomendación**: **Opción A**. Los blockers son reales, no inventados. Arreglarlos cuesta poco respecto al ahorro en debugging futuro. Y tener un corpus consistente facilita pasar a implementación con confianza.

---

## Pendientes futuras (no MVP, capturar acá)

Cosas detectadas en la revisión que no son prioritarias pero conviene anotar:

- **Threshold de `match_quality_avg < 0.4`**: re-evaluar con datos reales. Probable que sea muy laxo.
- **Renombre `percentage_type` → `multiplicative_type`** y `absolute_type` → `additive_type` (claridad semántica).
- **`expansion_table`**: decidir si va a `invariants.yaml` o queda hardcoded.
- **Bot restart reconciliation**: spec dedicada cuando sea recurrente.
- **Política de fairness multi-controller**: cuando haya 5+ controllers.
- **Drilldowns en dashboard**: clic en KPI → detalle de últimas N entradas.

## Próximos pasos

Esperar decisión del usuario sobre las 3 opciones (A/B/C). Una vez decidido, ejecutar las acciones correspondientes y actualizar TASKS.md.
