# Decisions Log

Log inmutable de decisiones tomadas con el usuario. Cada decisión se mantiene aunque después se cambie — los cambios se agregan abajo con fecha.

---

## 2026-05-09 — Primera ronda de open questions

### D1 — Hot-reload de configs (Q1) ✅ CONFIRMADO

**Decisión**: el controller `pmm_mister` soporta hot-reload nativamente.

**Evidencia**: campos marcados con `json_schema_extra={"is_updatable": True}` en Pydantic. El controller lee `self.config.<field>` cada tick sin caching. Mutación in-place del config object surte efecto en el próximo tick. Ver [`CONTROLLER_DEEP_DIVE.md`](CONTROLLER_DEEP_DIVE.md).

**Implicancia**: no hay que construir infra de hot-reload. Solo necesitamos una MCP tool / endpoint que aplique un patch (con validación de `is_updatable`).

### D2 — Campos modificables (Q2) → DEFERIDO

**Estado**: el deep dive del controller está hecho. La shortlist provisional queda definida en [`CONTROLLER_DEEP_DIVE.md` § "Implicancias para el framework LLM"](CONTROLLER_DEEP_DIVE.md). La decisión final de qué campos efectivamente toca el agente la posponemos a la siguiente iteración, una vez definido el MVP.

**Bordes claros ya decididos**:
- ❌ `manual_kill_switch` — siempre humano (D7).
- ❌ `connector_name`, `trading_pair`, `position_mode`, `controller_name`, `controller_type` — requieren restart, fuera de scope.

### D3 — Forma de las reglas del usuario (Q3) ✅

**Decisión**: opción **(c) híbrido** — prosa para la lógica fuzzy (que el LLM interpreta), límites duros estructurados como guardrails (código que el LLM no puede pasar).

**Aplicación**:
- `policy.md` (o sección de `agent.md`): reglas en prosa.
- `invariants.yaml` (o equivalente): cooldowns mínimos, max delta por campo, blacklist de campos, max cambios por hora.

### D4 — Modo de operación y cadencia (Q4) ✅

**Decisión**: implementar **los 3 modos** (propose / shadow / auto). A priori usar **propose** como default y único modo activo en MVP.

Detalle de los 3 modos: ver sección "Modos de operación" más abajo en este mismo doc.

### D5 — MVP rule (Q5) ✅ REDEFINIDA

**Decisión**: el MVP rule no es la propuesta original del asistente. Es:

> **"Si el precio se mantiene lateral y tenemos una desviación estándar grande, con un NATR que supera cierto threshold, aumentar el `take_profit`."**

**Análisis de la regla**:
- **Lateral** = régimen ranging, no tendencial.
- **Desviación estándar grande** = volatilidad alta dentro del rango.
- **NATR alto** = Normalized Average True Range alto, confirma volatilidad relativa.
- **Acción**: subir `take_profit`.

**Caveat técnico** (de [`CONTROLLER_DEEP_DIVE.md`](CONTROLLER_DEEP_DIVE.md)): cambiar `take_profit` solo afecta **executors nuevos**. Los activos retienen el TP con el que nacieron. El cambio se propaga gradualmente a medida que los executors viejos se cierran y se crean nuevos. Esto **no es un blocker** pero el agente debe entenderlo y reportarlo.

**Routines necesarias para esta rule**:
1. **`market_regime`** — clasifica direccionalidad × volatilidad (multi-timeframe). Incluye todos los indicadores de vol que la rule necesita (NATR, std dev, BB width).
2. ~~`volatility_metrics`~~ — descartada: solapamiento total con `market_regime`. Reemplazada en MVP por `controller_performance` (que sí es complementaria, no redundante).

Decisión 2026-05-09: el slot de la 3ra routine MVP pasa de `volatility_metrics` a `controller_performance` (market share, fill rate, PnL velocity, etc.). Ver D10.

### D6 — Multi-controller (Q6) ✅

**Decisión**: **1 agente para todos los controllers**, con flag para forzar 1-por-controller si en el futuro hace falta.

**Implicancia**: el agente itera sobre todos los controllers que está supervisando, evalúa rules para cada uno con sus inputs específicos, y emite N propuestas/acciones. Todo en un solo tick.

### D7 — Kill switch y circuit breaker (Q7) ✅

**Decisión**:
- **Kill switch** (`manual_kill_switch`): **siempre lo decide el humano**, el agente nunca lo toca.
- **Circuit breaker** del agente: **booleano simple** — si está activo, se autobloquea bajo ciertas condiciones; si no, no se preocupa por eso. No vamos a meternos a definir lógica fina de "K cambios en T minutos" en el MVP.

Cuándo se activa el booleano: por definir en la siguiente iteración. Default: `False` en MVP.

### D11 — Circuit breaker manual-only + Fases futuras F1 (UI) y F2 (análisis histórico) ✅ (2026-05-09)

**Decisión 1 — Circuit breaker**: el activado/desactivado del agente es **siempre manual**. No hay auto-triggers.

Razón: consistente con D7 ("siempre lo decide el humano"). Los auto-triggers que se barajaron (frequency, anti-evidence streak, capital crítico, outcome catastrófico, verdict streak) tienen falsos positivos posibles y agregan complejidad sin claro beneficio.

Lo que sí hay: **métricas de health** calculadas on-demand que el humano consulta para decidir cuándo pausar. El agente nunca se autobloquea, pero el humano tiene info clara para tomar la decisión. Implementación detallada en `CIRCUIT_BREAKER_SPEC.md`.

**Decisión 2 — Fases futuras explícitas**: dos cosas que el usuario quiere que **no perdamos de vista** se elevan a fases dedicadas (no quedan como ítems sueltos en backlog):

- **Fase F1 — Pantallas de UI**: panel de control del PMM Supervisor + pantalla detallada por controller. Documentado en TASKS.md sección "Fases futuras".

- **Fase F2 — Análisis histórico de bots**: pipeline offline que cruza datos históricos de bots ya corridos × régimen retroactivo × performance real → priors empíricos para el agente. **Esto es el corazón de la mejora continua con información empírica** (cita textual del usuario). Tiene entidad propia y se planificará como fase con diseño dedicado cuando llegue el momento.

Ambas fases quedan **fuera del MVP** pero capturadas para abordar después.

### D10 — Reemplazo de `volatility_metrics` por `controller_performance` ✅ (2026-05-09)

**Decisión**: descartar la routine MVP `volatility_metrics` y reemplazar el slot por `controller_performance`.

**Razón**:
- `volatility_metrics` (NATR, std dev, BB width) **quedó completamente cubierta** por `market_regime` cuando se bajó a spec multi-timeframe. Mantenerla es redundancia.
- `controller_performance` aparecía en el catálogo "no MVP" pero ya se identificaron 2 lugares donde es necesaria:
  1. **Disparador del backtest evidence loop (D9)**: necesitamos métricas reales de PnL/volumen del controller para decidir "¿estamos en período subóptimo?".
  2. **Market share** (volumen propio / volumen total del par): el usuario lo identificó como métrica que el agente debería perseguir.
  3. **Modo `shadow`**: para evaluar la calidad del agente sin riesgo, hace falta comparar performance esperada vs real.

**Alcance** (a especificar en 1.3):
- Métricas del controller: realized PnL, unrealized PnL, volume, num fills, accuracy, executor turnover, fill rate.
- Cruzadas con mercado: market share del par, gross spread captured (ratio fills favorables / fills adversos).
- Persistencia mínima para PnL velocity (derivada temporal del PnL).

**Implicancia para fases posteriores**:
- El disparador del backtest (2.5.1) ahora tiene fuente clara: `controller_performance` provee los números reales.
- El audit log de modo `shadow` (3.x) puede comparar propuesta vs `controller_performance` después de N días.

### D9 — Backtest Evidence Loop ✅ (2026-05-09)

**Decisión**: cada propuesta del agente que vaya al humano (modo `propose`) debe estar respaldada por backtests del período subóptimo detectado: 1 baseline (config actual) + hasta 3 alternativas (propuestas del agente).

**Disparador**: el agente solo entra al ciclo de backtest cuando detecta período subóptimo. **Disparador (c) con confirmación de market_regime**:
- Métricas reales del período reciente (PnL flat + volumen bajo + tiempo en régimen adverso > X) → señal.
- `market_regime` confirma "estamos en zona desfavorable" → confirmación.
- Si ambos → arranca el ciclo. Si solo uno → no.

**Ventana del backtest**: el período subóptimo detectado, sin cap. Razón: queremos calibrar exactamente sobre lo que está pasando mal. Si el período es muy largo (>24h, >7d) lo evaluamos cuando aparezca el caso real.

**Generación de las 3 alternativas — modelo (c) híbrido**:
- El LLM razona sobre el régimen y elige la **dimensión** (qué campo tocar y dirección): "subir take_profit", "ampliar buy_spreads", "reducir max_active_executors".
- Un componente **determinístico** expande esa decisión semántica en 3 valores discretos a backtestear.
- Ejemplo: LLM dice "subir take_profit" → expansor genera `[actual×1.5, actual×2.0, actual×3.0]`.
- Ventaja: el LLM no tira números arbitrarios; los valores son predecibles, auditables, y reproducibles.

**Función de score** (criterio del usuario: PnL ≈ 0+ con mucho volumen > PnL alto con poco volumen):
- PnL negativo → rechazo automático.
- Volumen pesa más que PnL absoluto.
- PnL muy alto vs baseline → bandera de overfitting.
- Fórmula tentativa (afinable):
  ```
  if pnl < 0: score = -inf
  pnl_norm = min(pnl, baseline.pnl × 2) / max(baseline.pnl, 1)
  vol_norm = vol / max(baseline.vol, 1)
  score = vol_norm + 0.3 × pnl_norm
  ```
- **La fórmula no es definitiva — hay que iterarla con datos reales.**

**Cadencia del agente**: subimos de 1 min a **10 minutos**. Razón: backtests cuestan 12-40s cada uno × (1 + 3) = hasta 3 minutos por ciclo. 10 min da espacio para que el ciclo termine y queda margen para el resto de routines. Latencia aceptable hasta 5 min según usuario.

**Cache**: hash-based contra `BacktestStore`:
```
key = hash(controller_id + config_snapshot + start_time + end_time + resolution)
```
Si la key existe → reusar. Si no → correr fresh.

**Caveats técnicos** (de la investigación):
- Backtest async tiene mismatch cliente↔servidor → usar **sync**.
- Sin paralelismo seguro → backtests **en serie**.
- No funciona en hyperliquid, dydx, kraken, coinbase_advanced_trade.
- Datos OHLCV se bajan del REST público sin caché local — primer call es lento.
- El engine de backtest es CPU-bound, semi-singleton — concurrencia es riesgo.

**Por qué esta decisión es importante de revisar en el futuro**:
- La función de score es heurística — datos reales pueden mostrar que volumen vs pnl tiene otro peso óptimo.
- El expansor determinístico (×1.5, ×2, ×3) puede ser muy crudo — quizás convenga grid adaptive según el régimen.
- El cap de ventana (sin cap hoy) puede tener que limitarse si los backtests largos se vuelven prohibitivos.

### D8 — Auditabilidad y persistencia (Q8) ✅

**Decisión**:
- Adaptarse al framework existente lo más posible (no inventar infra nueva si la actual sirve).
- **Persistir info entre sesiones**: cada decisión y cada cambio sobrevive al fin de la sesión del agente.
- **Análisis inmutable**: una vez escrita una entrada de auditoría (config antes/después + evidencia + razón), no se modifica.

**Aplicación**: revisaremos qué hace hoy `trading_agents/<agent>/sessions/` y vemos si su esquema de journal sirve. Si necesitamos algo aparte para los patches de config, lo agregamos como append-only log dedicado.

---

## Modos de operación (detalle de D4)

### Modo 1 — `propose` (recomendado como default y MVP)

El agente detecta una situación, diagnostica, y emite una **propuesta de patch** a Telegram con botones inline:

```
🟡 PROPOSAL — pmm_mister @ binance BTC-USDT
Régimen: ranging | NATR: 0.0142 (>0.012 threshold)
Std dev (1h): alta

Acción propuesta:
  take_profit: 0.0003 → 0.0008 (+167%)
  
Razón: precio lateral con volatilidad alta — más espacio antes de TP debería capturar más rebote.

Caveat: solo aplica a executors nuevos.

[✅ Aplicar]  [❌ Rechazar]  [⏸ Snooze 1h]
```

**Pros**:
- Cero riesgo en MVP — el humano valida cada cambio.
- El usuario aprende el comportamiento del agente antes de soltarle el volante.
- Genera un dataset valioso de "el LLM propuso X, el humano aprobó/rechazó" → input para tunear policy.

**Contras**:
- Latencia humana — si dormís, no se aplican cambios.
- Trabajo manual.

### Modo 2 — `shadow` (validación silenciosa)

El agente hace todo el ciclo (detectar, diagnosticar, proponer patch) pero **no aplica nada** y **no le manda nada al usuario**. Solo loggea en el journal.

```
[shadow] Hubiese aplicado: take_profit 0.0003 → 0.0008
         Razón: ranging + NATR alto.
         Resultado hipotético (a evaluar después): N/A
```

**Pros**:
- Cero riesgo absoluto — invisible al sistema.
- Permite evaluar la calidad del agente con N días de logs antes de pasar a `propose` o `auto`.

**Contras**:
- No hay feedback, no aprende nada del usuario.
- Sirve solo como fase de validación, no de uso real.

### Modo 3 — `auto` (full agency, con guardrails duros)

El agente aplica patches automáticamente, sin pedir permiso. **Pero los invariantes de D3 + circuit breaker (D7) actúan como red de seguridad**:

- Cooldown mínimo entre cambios.
- Max delta por campo.
- Blacklist de campos (`manual_kill_switch`, etc.).
- Si circuit breaker activado por algún criterio, autobloquea.

**Pros**:
- 24/7 sin intervención humana.
- Velocidad de reacción óptima.

**Contras**:
- Si la policy tiene un bug, puede destruir un bot rápido.
- Solo después de tener confianza con `propose` durante semanas.

### Selección de modo

El modo es config del agente. Cambiar de modo es trivial (es solo flipear un campo en `agent.md`). El MVP arranca en `propose`. La progresión natural sería:
1. Implementar los 3 modos.
2. Correr en `propose` durante semanas, recolectando datos.
3. Si la calidad de las propuestas es alta, pasar a `auto` con confianza.
4. `shadow` queda como modo de testing post-cambios al policy.
