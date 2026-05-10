# Progress

## Estado: Discovery / Diseño inicial

## Hecho
- [x] Mapear el repo: identificar `pmm_mister` controller, `pmm_mister_supervisor` routine y agente actual.
- [x] Borrador inicial de `DESIGN.md` con arquitectura de 3 capas (routines / agente / controller).
- [x] Lista inicial de open questions (`OPEN_QUESTIONS.md`).
- [x] Descartar `pmm_mister_supervisor` del scope; extraer lecciones a `LESSONS_FROM_SUPERVISOR.md` (6 lecciones).
- [x] Deep dive del controller `pmm_mister` con código de Hummingbot → `CONTROLLER_DEEP_DIVE.md`.
- [x] Confirmar Q1: hot-reload soportado nativamente via `is_updatable`.
- [x] Resolver primera ronda de open questions (D1–D8) → `DECISIONS.md`.
- [x] Refinar `DESIGN.md` con decisiones tomadas y MVP redefinido.
- [x] Investigar dinámica de capital del controller (worst case = `nominal × max_active_executors_by_level`, sin validación de balance interna).
- [x] Investigar API disponible en hummingbot-api para balances/posiciones/executors (todo está disponible).
- [x] Diseñar `capital_state` en 3 planos (controller / inter-controller / cross-pair) en `CAPITAL_DYNAMICS.md`.
- [x] Diseñar visualización de utilización compacta tipo network monitor.

## Próximo paso

Empezar a especificar contratos concretos del MVP. Tres frentes:

1. **Routines del MVP**: especificar `market_regime` y `controller_performance` (D10 reemplazó `volatility_metrics`).

2. **Schema del patch + invariants**: definir el contrato del "patch de config" (JSON), la lista de campos whitelisted, y los invariants iniciales (cooldown, max delta por campo).

3. **Flujo de modo `propose`**: diseñar el formato del mensaje a Telegram + botones inline + handler de respuesta + persistencia del veredicto.

## Después
- [ ] Diseñar la persistencia del estado del agente (último cambio por controller, cooldowns activos) y el audit log inmutable.
- [ ] MCP tool `update_controller_config`: especificación.
- [ ] Implementación de modo `shadow` y `auto`.
- [ ] Plan de implementación por fases (`phases/01-*.md`, etc.).

## Backlog (ideas futuras, no MVP)
- Sistema de "memoria" del agente: qué cambios funcionaron históricamente, qué no.
- A/B de configs (correr 2 controllers similares con configs distintas, agente decide cuál seguir).
- Generalización a otros controllers (no solo `pmm_mister`).
