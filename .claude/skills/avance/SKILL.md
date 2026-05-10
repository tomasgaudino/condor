---
name: avance
description: Reporta el avance del diseño del Adaptive Strategy Framework. Lee TASKS.md y muestra un resumen jerárquico con qué está hecho, qué está en curso, qué bloquea y cuál es el próximo paso natural. Usar cuando el usuario pregunta "/avance", "cómo venimos", "estado", "qué falta", o similar — siempre en el contexto de este diseño.
---

# /avance — Reporte de avance del framework

## Tu misión

Cuando el usuario invoque este skill, reportá el estado del diseño del Adaptive Strategy Framework de forma rápida y útil. La fuente de verdad es `.planning/strategy-framework/TASKS.md`.

## Pasos

1. **Leé** `/Users/tomasgaudino/PycharmProjects/condor/.planning/strategy-framework/TASKS.md`.
2. **Contá** el estado de cada nivel:
   - Tareas de nivel 1 (Fases) — cuántas hay y cuántas tienen 100% de subtareas done.
   - Tareas de nivel 2 — cuántas done / in progress / pending / blocked en total.
3. **Identificá**:
   - La fase activa (la que tiene work in progress o es la siguiente con pending).
   - Tareas marcadas `[~]` (in progress) — siempre listalas.
   - Tareas marcadas `[!]` (blocked) — destacarlas.
   - Próximo paso natural (primera tarea `[ ]` después de la activa actual).
4. **Mostrá** un resumen estructurado, no el archivo completo.

## Formato de salida

```
📍 ADAPTIVE STRATEGY FRAMEWORK — Avance

Fase activa: <nombre de la fase>

Progreso por fase:
  ✅ Fase 0 — Discovery & Diseño         (6/6)
  🔄 Fase 1 — Especificación de routines (0/4)
  ⬜ Fase 2 — Especificación del agente  (0/4)
  ⬜ Fase 3 — Modo `propose`             (0/3)
  ⬜ Fase 4 — MCP tool                   (0/3)
  ⬜ Fase 5 — Implementación             (0/7)
  ⬜ Fase 6 — Validación                 (0/5)

🔄 En curso:
  (listar tareas [~] si hay; "ninguna" si no hay)

⏭ Próximo paso natural:
  <primera [ ] de la fase activa>

⚠️ Bloqueadores:
  (listar tareas [!] si hay; omitir sección si no hay)

📂 Docs activos en .planning/strategy-framework/:
  - DESIGN.md         (arquitectura)
  - DECISIONS.md      (D1–D8)
  - TASKS.md          (este avance)
  - CAPITAL_DYNAMICS.md
  - CONTROLLER_DEEP_DIVE.md
  - LESSONS_FROM_SUPERVISOR.md
  - OPEN_QUESTIONS.md
```

## Reglas

- **No** listes todas las subtareas done. Solo el conteo agregado.
- **Sí** listá las `[~]` (in progress) y `[!]` (blocked) — son las que importan.
- Si una fase no tiene avance pero la anterior sí está completa, marcala como "siguiente" no como "activa".
- Sé conciso. Este reporte tiene que entrar en una pantalla.
- Si hay algo curioso en TASKS.md que vale la pena destacar (ej: muchas tareas blocked, fase saltada), agregá una línea final tipo `💡 Nota: ...`.
- Si el archivo TASKS.md no existe, decir claramente "TASKS.md no encontrado en .planning/strategy-framework/" y no inventar.

## Después del reporte

Después de mostrar el avance, ofrecé al usuario opciones concretas:
- "¿Querés que arranque con <próximo paso natural>?"
- O si hay tareas in progress: "¿Seguimos con <tarea in progress>?"

No hagas más que reportar y ofrecer. No empezás trabajo nuevo sin que el usuario confirme.
