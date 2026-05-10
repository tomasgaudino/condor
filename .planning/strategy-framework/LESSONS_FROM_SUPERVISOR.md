# Lecciones del `pmm_mister_supervisor` (descartado)

> El agente `pmm_mister_supervisor` queda **fuera del scope** del nuevo framework, pero su existencia y sus fallas dejan aprendizajes que el nuevo diseño debe respetar.

## Lo que el supervisor intentaba hacer

Per `agent.md`:
1. Cada tick correr la routine `pmm_mister_supervisor`.
2. Para cada controller, chequear si `hold_pct` está fuera de `[min_base_pct, max_base_pct]`.
3. Si está fuera, mandar alerta a un chat de Telegram.
4. Si todo OK, no hacer nada.
5. **Nunca modificar configs.**

## Lo que pasó en la práctica (de los journals)

### Patrón 1 — El bug del `chat_id`
- Ticks 1-12 (~50 minutos): la routine devolvió `"No chat_id available"` repetidas veces.
- Causa: la routine se ejecutaba en contexto del agente (no de un chat de Telegram), y el guard de `chat_id` saltaba antes de usar `target_chat_id`.
- Fix tardío: mover el `send_to = target_chat_id or chat_id` antes del guard.

### Patrón 2 — `send_notification` MCP roto en contexto de agente
- A partir del tick 13 la routine empezó a entregar bien, pero el agente intentaba mandar la alerta también via `send_notification` MCP tool.
- Esa tool requiere `CONDOR_CHAT_ID` env var, que en contexto de agente no está seteada.
- El agente loopeó "fail → retry con workaround" durante muchos ticks antes de aprender a no usar la tool y confiar en el delivery interno de la routine.

### Patrón 3 — Loop de ruido perpetuo
- Ticks 59-78: el controller estaba `BELOW MIN` (hold 0%) ininterrumpidamente. El agente mandó **20 alertas idénticas** en ~30 minutos.
- No había deduplicación, ni cooldown, ni hysteresis.
- Cada tick era una decisión nueva, sin memoria del estado anterior.

### Patrón 4 — "Action" sin acción
- Todos los ticks loggean `actions=0`. El agente "decide" que el controller está fuera de rango pero **no hace nada accionable** — solo manda un mensaje.
- La detección no estaba acoplada a ninguna respuesta operativa.

### Patrón 5 — Detección de un síntoma, no de una causa
- `hold_pct = 0%` durante días seguidos no es un problema en sí mismo — puede significar que el controller está esperando entries y el mercado no le da. O que está fundido. O que se reinició.
- Sin contexto de mercado (régimen, volatilidad, distancia al precio), la alerta es un dato sin interpretación.

## Lecciones que el nuevo framework debe incorporar

### L1 — El contrato data-source / agente debe ser robusto a contextos
Las routines deben funcionar igual cuando las invoca un humano por Telegram, un agente con MCP, o un test. No depender de env vars implícitas. El `pmm_mister_supervisor` se rompió porque mezclaba "data fetching" con "delivery channel".

**Aplicación**: las routines del nuevo framework devuelven **JSON estructurado** y **no tienen side effects de delivery**. La capa que decide qué hacer con los datos es separada.

### L2 — Detección sola es ruido. Detección + contexto + acción es señal.
Mandar 20 alertas iguales no aporta nada. El agente tiene que combinar señales (ej: "hold 0% **+** régimen ranging **+** sin fills hace 2h" → causa probable: spreads muy lejos del mid → acción: comprimir spreads).

**Aplicación**: el nuevo agente nunca emite una alerta "raw"; siempre emite un **diagnóstico + acción propuesta**. Si no hay acción posible, no emite nada.

### L3 — Memoria entre ticks es esencial
Sin memoria, el agente no puede distinguir "primer tick fuera de rango" de "tick #20 consecutivo fuera de rango". El segundo amerita escalación, el primero no.

**Aplicación**: el nuevo agente mantiene un **state mínimo persistente** (por controller): última detección, último cambio aplicado, último cooldown. Esto puede vivir en el journal o en un archivo dedicado.

### L4 — Hysteresis y cooldowns son obligatorios
Sin cooldown: cada tick produce un cambio. Resultado: thrashing, alertas duplicadas, decisiones que se pisan.

**Aplicación**: cada tipo de acción tiene un cooldown mínimo configurable. Y para detección, hysteresis: si entró a "BELOW MIN" hace <5 ticks no escala todavía.

### L5 — Distinguir delivery de decisión
El agente no debería tener que pelear con cómo entregar mensajes. Eso lo resuelve la infraestructura.

**Aplicación**: la capa de decisión emite un evento estructurado (`{type: "config_patch", ...}` o `{type: "alert", severity: "warn", ...}`). Un dispatcher separado decide a qué canal va (Telegram, log, auto-apply, propose).

### L6 — Las rules en prosa libre están bien para razonamiento, mal para guardrails
El `agent.md` actual dice "manda alerta si X" en prosa. Funciona, pero no hay nada que impida al LLM saltarse la regla o aplicar 50 veces.

**Aplicación**: separar **policy** (LLM razona en prosa) de **invariants** (código duro: cooldown N, max delta M, no tocar el campo X). Los invariants se chequean fuera del LLM.
