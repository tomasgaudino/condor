# Open Questions

Preguntas que necesito resolver con vos antes de avanzar al diseño detallado.

## Bloque 1 — Mecánica de aplicación de cambios

### Q1. ¿Cómo se modifica una controller config de `pmm_mister` en vivo?
- ¿Hummingbot soporta hot-reload de la config? ¿O hay que reiniciar el controller?
- ¿Hay un endpoint en `mcp-hummingbot` que actualice configs? Si no, hay que agregarlo.
- ¿Se puede aplicar un *delta* (solo los campos que cambian) o hay que mandar la config completa?

### Q2. ¿Qué campos tiene sentido que el agente modifique?
Mirando los 30+ campos de `pmm_mister/config.py`, mi shortlist inicial:
- **Spreads**: `buy_spreads`, `sell_spreads` (ampliar/comprimir según volatilidad).
- **Skew**: `target_base_pct`, `min_base_pct`, `max_base_pct` (mover target si el inventory drift es persistente).
- **Take profit**: `take_profit` (aflojar si los TP no se ejecutan).
- **Refresh times**: `executor_refresh_time`, `*_cooldown_time` (tickear más rápido en alta volatilidad).
- **Allocation**: `total_amount_quote` (reducir exposición si el régimen es adverso).

¿Hay campos que NO querés que toque nunca? ¿`leverage`, `position_mode`, `manual_kill_switch`?

## Bloque 2 — Reglas del usuario

### Q3. ¿Cómo querés expresar las reglas?
Opciones:
- **(a)** Prosa libre en `agent.md` ("si la volatilidad sube y el inventory está sesgado long, ampliar buy_spreads en 50%"). El LLM las interpreta.
- **(b)** DSL estructurado (YAML/JSON con condiciones y acciones). Más predecible pero menos flexible.
- **(c)** Híbrido: prosa para lógica fuzzy, hard-limits estructurados como guardrails.

Mi voto: **(c)**. Las reglas en prosa para la lógica fina, y limites duros (max ampliación de spread, max desviación de target_base_pct, cooldown entre cambios) como guardrails que el LLM no puede pasar.

### Q4. ¿Cuál es la cadencia de decisión?
El agente puede tickear cada N segundos/minutos. Pero los cambios deberían ser raros (no querés thrashing). Propongo:
- **Tick rápido** (cada minuto): lee routines, evalúa si hay que actuar.
- **Cooldown duro**: no más de 1 cambio cada K minutos (configurable, default 30min?).
- **Modo "propose"**: en vez de aplicar, manda al usuario un mensaje de Telegram con botones aprobar/rechazar.

## Bloque 3 — Alcance MVP

### Q5. ¿Qué rule querés que el agente pueda ejecutar primero, end-to-end?
Necesito un caso de uso concreto y mínimo para validar la arquitectura antes de generalizar. Mi propuesta:

> "Si `hold_pct` está fuera del rango [min_base_pct, max_base_pct] por más de 30 minutos seguidos, **mover el `target_base_pct` 5% en la dirección del drift** (con tope: nunca pasar de min/max)."

Es testeable, simple, y ejercita las 3 capas. ¿Te sirve como MVP o tenés algo mejor en mente?

### Q6. ¿Una sola instancia del agente para múltiples controllers, o uno por controller?
Hoy `pmm_mister_supervisor` corre 1 agente que mira N controllers. Para el supervisor adaptativo, las opciones son:
- **(a)** 1 agente por controller — más aislado, más caro, decisiones independientes.
- **(b)** 1 agente para todos — puede correlacionar (BTC y ETH al mismo tiempo), más barato.

Mi voto: **(b)** con flag para forzar (a) si hace falta.

## Bloque 4 — Seguridad

### Q7. ¿Kill switch?
- ¿Querés que el agente pueda pausar el controller (`manual_kill_switch=true`) si detecta algo grave? ¿O eso siempre lo decide el humano?
- ¿Un "circuit breaker": si el agente hizo K cambios en T minutos, se autobloquea y pide intervención humana?

### Q8. ¿Auditabilidad?
Cada cambio debería loggearse con: timestamp, config antes, config después, evidencia (snapshot de routines), razón del LLM. ¿Dónde lo guardamos? ¿`trading_agents/<agent>/sessions/.../journal.md` (que ya existe) sirve?
