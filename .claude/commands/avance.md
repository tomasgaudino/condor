---
description: Reportar el avance del Adaptive Strategy Framework leyendo TASKS.md
---

Leé `.planning/strategy-framework/TASKS.md` y reportá el estado del diseño del Adaptive Strategy Framework.

## Pasos

1. Leé el archivo `.planning/strategy-framework/TASKS.md`.
2. Contá el estado de cada tarea de nivel 1 (fases) — cuántas subtareas done vs total.
3. Identificá:
   - La fase activa (la que tiene work in progress o es la siguiente con pending).
   - Tareas marcadas `[~]` (in progress).
   - Tareas marcadas `[!]` (blocked).
   - Próximo paso natural (primera tarea `[ ]` después de las done).

## Formato de salida

```
📍 ADAPTIVE STRATEGY FRAMEWORK — Avance

Fase activa: <nombre de la fase>

Progreso por fase:
  ✅ Fase 0 — Discovery & Diseño         (X/Y)
  🔄 Fase 1 — Especificación de routines (X/Y)
  ⬜ Fase 2 — ...                         (X/Y)
  ...

🔄 En curso:
  (listar tareas [~] si hay; "ninguna" si no hay)

⏭ Próximo paso natural:
  <primera [ ] de la fase activa>

⚠️ Bloqueadores:
  (listar tareas [!] si hay; omitir sección si no hay)

📂 Docs activos en .planning/strategy-framework/:
  - <listar los .md presentes>
```

## Antes de reportar — chequeo de salud

Si esta sesión es nueva (es la primera vez que se invoca `/avance` en
ella), **leé también** `.planning/strategy-framework/LEARNINGS.md` y
mencioná en una línea al final del reporte algo como:
"💡 Recordá: revisé LEARNINGS.md, X lecciones activas." Esto fuerza
no recaer en errores ya cometidos.

## Reglas

- **No** listes cada subtarea done. Solo conteo agregado.
- **Sí** listá las `[~]` (in progress) y `[!]` (blocked).
- Sé conciso: el reporte debe entrar en una pantalla.
- Si TASKS.md no existe, decir "TASKS.md no encontrado en .planning/strategy-framework/" y no inventar.
- Después del reporte, ofrecer al usuario seguir con la próxima tarea o con la in-progress, sin empezar trabajo nuevo sin confirmación.
