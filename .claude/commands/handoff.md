---
description: Cerrar la sesión actual con la entrega completa al próximo arranque (entrada de SESSION_LOG, lecciones nuevas, push).
---

Vas a cerrar la sesión de trabajo en el Adaptive Strategy Framework.
El objetivo es **dejar todo capturado** para que un /onboard mañana
arranque sin pérdida de contexto. Seguí este ritual paso a paso, sin
saltar ninguno.

## Paso 1 — Recolectar metadata determinística

Sin pedirle nada al usuario todavía, ejecutá:

```bash
# Commits desde el último día que haya entry en SESSION_LOG
# (fallback: últimas 24h si no podés determinar la fecha)
git log --oneline --since="00:00 today" feat/pmm_mister_supervisor

# Cambios sin commitear
git status --short

# Branch y push status
git rev-parse --abbrev-ref HEAD
git status -sb | head -1

# Tests
.venv/bin/python -m pytest tests/ -q 2>&1 | tail -3

# Lectura de la última entrada de SESSION_LOG.md para no duplicar fecha
head -30 .planning/strategy-framework/SESSION_LOG.md
```

Tené esa info a mano para los pasos siguientes. No la mostrés al
usuario todavía.

## Paso 2 — Confirmar que hay algo para cerrar

- Si **no hay commits nuevos del día** Y **no hay cambios sin
  commitear** Y **la última entrada de SESSION_LOG es de hoy**, decir:
  > "No hay actividad nueva desde la última entrada del log. Nada para
  > registrar. ¿Cierro igual con un push?"
  Y esperar respuesta. No avanzar a paso 3.
- Si la última entrada de SESSION_LOG es de hoy pero hubo commits
  posteriores, ofrecer **editar la entrada existente** en lugar de
  crear una nueva.
- Si hay commits pero ninguna entrada de hoy, avanzar al paso 3.

## Paso 3 — Borrador automático de la entrada

Construí un draft de la nueva entrada de SESSION_LOG que se va a poner
**arriba** del archivo (orden inverso). Estructura:

```markdown
## YYYY-MM-DD · <título corto del día — yo lo propongo, el usuario afina>

**Contexto inicial**: <inferido del SESSION_LOG anterior — 1 frase>.

### Lo que se hizo
- (auto-generado a partir de los mensajes de commit del día,
  agrupados por intención)

### Decisiones operativas
- (placeholder — pedirle al usuario)

### Fricciones / aprendizajes
- (placeholder — pedirle al usuario, ofrecerse a sumarlas a LEARNINGS.md)

### Estado al cerrar
- Branch, tests, commits del día, push status.
- (auto-generado de la metadata del paso 1)

### Para retomar mañana
- (placeholder — la parte más importante, pedírsela explícitamente)

### Commits del día
- (auto-generado, listado con hash + mensaje)
```

## Paso 4 — Pedirle al usuario lo que requiere juicio

Mostrale el draft y pedile específicamente:

1. **Título del día**: una frase corta que capture el tema central
   (ej. "Cierre del primer slice de capital_state end-to-end").
2. **Decisiones operativas**: qué decisiones se tomaron hoy que el
   yo de mañana debería conocer (no las que ya están en DECISIONS.md
   formales — las del día a día).
3. **Fricciones**: 1-3 momentos donde algo salió mal o el usuario tuvo
   que corregirme. Para cada una preguntarle:
   "¿Esto amerita una entrada nueva en LEARNINGS.md? Si sí, ¿cómo la
   redactarías?".
4. **Para retomar mañana**: la recomendación operativa concreta. No
   "seguir con la fase X" sino algo accionable como "antes de codear
   market_regime, validar shape de candles con condor-express contra
   brigado".

## Paso 5 — Persistir y commitear

- Escribir la entrada nueva al **principio** de
  `.planning/strategy-framework/SESSION_LOG.md` (después del header
  introductorio y antes de las entradas anteriores).
- Si el usuario aprobó nuevas lecciones, agregarlas a
  `.planning/strategy-framework/LEARNINGS.md` con la fecha de hoy.
- Commit atómico:
  ```
  docs(planning): close-of-day session log for YYYY-MM-DD
  ```
  con cuerpo breve resumiendo qué cubrió la entrada. **Nunca**
  `git add .` — solo los archivos editados (SESSION_LOG.md y opcional
  LEARNINGS.md).

## Paso 6 — Push

```bash
git push drupman feat/pmm_mister_supervisor
```

Si falla por cualquier razón (auth, conectividad), mostrale el error
al usuario y dejarle el comando para que lo corra él manualmente. No
intentar trucos.

## Paso 7 — Reportar cierre

Cerrar con un mensaje breve para el usuario:

```
✅ Handoff completo.

Registrado en SESSION_LOG: <título del día>
Commit nuevo: <hash> · push a drupman ✓
<N> lecciones nuevas en LEARNINGS.md.

Mañana: ejecutá /onboard al arrancar para retomar el contexto.
```

## Reglas

- **No inventes** decisiones, fricciones o "para retomar mañana". Si el
  usuario no las da, el draft queda con placeholders explícitos
  (`(pendiente — agregar manualmente)`).
- **Confirmá antes de commitear**: mostrale al usuario la entrada
  final completa antes de persistir, ofrecele editar.
- **Si el usuario dice "ya, cierralo rápido"**, igual hacé el commit
  con la metadata determinística y un mínimo placeholder en las
  partes interpretativas. Mejor entrada incompleta que ninguna.
- **No pushees a `origin`** (hummingbot upstream). Push solo a
  `drupman` (el fork del usuario).
