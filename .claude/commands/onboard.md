---
description: Cargar contexto del Adaptive Strategy Framework al arrancar una sesión nueva.
---

Vas a retomar el trabajo en el Adaptive Strategy Framework. Tu contexto
de la conversación anterior se perdió, pero el repo guarda todo lo que
necesitás. Seguí este protocolo determinístico.

## Protocolo

Ejecutalo **en orden**, sin saltar pasos:

1. **`LEARNINGS.md`** — `.planning/strategy-framework/LEARNINGS.md`.
   Lectura completa. Estos son los errores que ya cometí en sesiones
   anteriores. No quiero repetirlos.

2. **Última entrada de `SESSION_LOG.md`** —
   `.planning/strategy-framework/SESSION_LOG.md`.
   Solo la entrada más reciente (la de arriba — está en orden inverso).
   Trae el contexto interpretativo de la última sesión: qué se hizo,
   qué fricciones hubo, qué quedó pendiente, "para retomar mañana".

3. **`TASKS.md`** — `.planning/strategy-framework/TASKS.md`.
   Solo la sección "Fase activa" + próximas 3 subtareas. No hace falta
   leer todo el plan.

4. **Verificar estado del repo**: corré
   ```bash
   git status -uno && git log --oneline -5
   ```
   Confirmar que estoy en `feat/pmm_mister_supervisor` (no main) y ver
   los últimos commits.

5. **Verificar tests**: corré
   ```bash
   .venv/bin/python -m pytest tests/ -q 2>&1 | tail -3
   ```
   Confirmar que todos pasan. Si no pasan, **eso es lo primero a
   resolver**, no avanzar a feature nuevo.

## Reportar al usuario

Después de leer todo, devolvele un resumen conciso en este formato:

```
🟢 Onboard listo.

📍 Estado al cerrar la sesión anterior:
  <2-3 líneas extraídas del SESSION_LOG>

⚠️ Lecciones activas (no recaer):
  <listar 2-4 más relevantes para el próximo paso>

🎯 Próximo paso natural:
  <de "Para retomar mañana" del SESSION_LOG, o de TASKS.md fase activa>

🔧 Repo: branch `feat/pmm_mister_supervisor`, X tests pasando, Y commits.

¿Arranco con <próximo paso> o tenés otra prioridad?
```

## Reglas

- **No hagas nada más** hasta que el usuario confirme. El onboard es
  solo lectura + reporte.
- **Si encontrás algo raro** (tests fallando, branch incorrecta, commit
  pendiente sin commitear), reportalo en el resumen y ofrecé resolverlo
  antes de continuar.
- **Si no podés leer alguno de los archivos clave** (no existe, está
  roto), no inventes — decilo claramente: "no encontré
  SESSION_LOG.md, ¿es la primera sesión sobre este framework?".
- **El reporte es conciso**: cabe en una pantalla. Detalles los pedirás
  cuando los necesites.
