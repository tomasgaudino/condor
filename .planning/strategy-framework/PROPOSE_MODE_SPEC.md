# Propose Mode — spec del flujo humano-en-el-loop

> Cómo se presenta una propuesta al humano via Telegram y cómo se procesa la respuesta.
> Última actualización: 2026-05-09

## Filosofía

El modo `propose` es el **default del MVP**. Cada propuesta del agente que pasó invariants + backtest se envía al humano via Telegram con botones inline. El humano:
- Aprueba (con elección de candidato).
- Rechaza.
- Pospone (snooze).
- Ignora (timeout 24h).

Nada se aplica al controller real sin clic explícito del humano.

## Pre-requisitos

Antes de entrar a propose, el patch ya pasó:
1. Disparador del backtest loop (`BACKTEST_LOOP_SPEC.md` § 2.5.1).
2. Expander → 1-3 candidatos válidos (`PATCH_CONTRACT_SPEC.md`).
3. Backtest loop → SELECTED PATCH con winner (`BACKTEST_LOOP_SPEC.md`).
4. Validación final contra invariants (cooldowns, no_oversub, circuit breaker).

Si todo OK → entra a propose.

## 3.1 — Formato del mensaje

### Layout completo (Q1=a confirmado, default MVP)

```
🤖 ADAPTIVE PROPOSAL — pmm-btc-1

Controller: 001_pmm_binance_BTC-USDT
Régimen: mean_reverting_high_vol (47 min)
Período subóptimo: 2h

📊 Análisis del LLM:
take_profit · increase · medium

> Régimen mean-reverting con NATR alto (0.014 vs p67 0.010).
> Las oscilaciones grandes hacen que el TP de 0.0003 capture
> solo parte del rebote.

⚠️ Solo afecta executors nuevos. Efecto gradual.

📈 Backtests del período subóptimo:

  Baseline:     PnL +0.12  Vol  12.4k  ▓▓▓▓▓
  c1: ×1.5      PnL +0.45  Vol   9.8k  ▓▓▓▓▓▓░ ★ winner (1.42)
  c2: ×2.0      PnL +0.78  Vol   8.2k  ▓▓▓▓▓░░  (1.35)
  c3: ×3.0      PnL +1.12  Vol   5.4k  ▓▓▓░░░░  (1.12)

Ganador: take_profit 0.0003 → 0.00045

[✅ Apply c1]  [c2]  [c3]
[❌ Reject]    [⏸ Snooze 1h]
```

### Decisiones del formato

- **MarkdownV2**: misma convención que el resto de Condor.
- **Header con bot + controller** identificables.
- **Régimen + persistencia** visible para contexto.
- **Reasoning del LLM en blockquote** (`>` lines) — diferencia visual.
- **Caveats con ⚠️** prominente.
- **Tabla de backtests** con barra ASCII proporcional al volumen + score.
- **Winner marcado con ★** + score numérico.
- **Botones inline en 2 filas**:
  - Fila 1: Apply c1 (winner pre-seleccionado) + alternativas como botones secundarios.
  - Fila 2: Reject + Snooze.

### Versión compacta (variante futura)

Si el mensaje resulta abrumador en mobile, alternativa:

```
🤖 PROPUESTA · BTC-USDT · mean_rev_high_vol

take_profit ↑ medium
0.0003 → 0.00045 (×1.5) ★

📈 PnL +0.45 (vs +0.12) Vol 9.8k

[✅ Apply ★]  [Detalle]
[c2]  [c3]
[❌]  [⏸]
```

El botón "Detalle" expande al formato completo. **No MVP**: solo lo agregamos si el formato completo da problemas en práctica.

### Callback data

Pattern compatible con Condor: `adaptive:<action>:<patch_id>:<candidate_id>`

| Botón | callback_data |
|---|---|
| Apply c1 (winner) | `adaptive:apply:p_xxx:c1` |
| Apply c2 | `adaptive:apply:p_xxx:c2` |
| Apply c3 | `adaptive:apply:p_xxx:c3` |
| Reject | `adaptive:reject:p_xxx` |
| Snooze 1h | `adaptive:snooze:p_xxx:1h` |
| Detail (futuro) | `adaptive:detail:p_xxx` |

### Construcción del mensaje

```python
def build_propose_message(selected_patch: dict, controller_meta: dict) -> tuple[str, InlineKeyboardMarkup]:
    """Build the full propose message + buttons. Returns (text_md, keyboard)."""
    
    text = build_text(selected_patch, controller_meta)  # MarkdownV2
    keyboard = build_keyboard(selected_patch["patch_id"], selected_patch["candidates"])
    
    return text, keyboard


def build_keyboard(patch_id: str, candidates: list) -> InlineKeyboardMarkup:
    # Winner suele ser el primero en el orden de score
    winner = candidates[0]
    others = candidates[1:]
    
    apply_winner_btn = InlineKeyboardButton(
        f"✅ Apply {winner['id']}",
        callback_data=f"adaptive:apply:{patch_id}:{winner['id']}"
    )
    
    apply_alt_btns = [
        InlineKeyboardButton(
            o["id"],
            callback_data=f"adaptive:apply:{patch_id}:{o['id']}"
        )
        for o in others
    ]
    
    reject_btn = InlineKeyboardButton(
        "❌ Reject",
        callback_data=f"adaptive:reject:{patch_id}"
    )
    snooze_btn = InlineKeyboardButton(
        "⏸ Snooze 1h",
        callback_data=f"adaptive:snooze:{patch_id}:1h"
    )
    
    return InlineKeyboardMarkup([
        [apply_winner_btn] + apply_alt_btns,
        [reject_btn, snooze_btn],
    ])
```

### Envío del mensaje

Se envía al chat configurado en `config.yml#adaptive.notifications.propose_chat_id`:

```python
async def send_proposal(bot, selected_patch, controller_meta, chat_id):
    text, kb = build_propose_message(selected_patch, controller_meta)
    msg = await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=kb,
    )
    
    # Persistir el message_id en el audit_log para poder editar después
    update_audit_entry(selected_patch["patch_id"], {
        "telegram_chat_id": chat_id,
        "telegram_message_id": msg.message_id,
        "human_verdict": "pending",
    })
    
    return msg
```

## 3.2 — Handler de respuesta

### Registro

En `main.py`:

```python
application.add_handler(
    CallbackQueryHandler(adaptive_callback_handler, pattern="^adaptive:")
)
```

### Dispatcher

```python
async def adaptive_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()  # ack inmediato a Telegram
    
    parts = query.data.split(":")
    namespace, action, patch_id, *args = parts
    
    # 1. Cargar entry del audit_log
    entry = find_audit_entry(patch_id)
    if entry is None:
        await query.edit_message_text("❌ Propuesta no encontrada (¿expirada o archivada?)")
        return
    
    # 2. Prevención de doble-click / propuestas ya resueltas
    if entry["human_verdict"] != "pending":
        await query.edit_message_text(
            f"⚠️ Esta propuesta ya fue *{entry['human_verdict']}* a las "
            f"{entry['verdict_at']}",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return
    
    # 3. Routing por acción
    by = update.effective_user.id
    if action == "apply":
        await handle_apply(query, entry, candidate_id=args[0], by=by)
    elif action == "reject":
        await handle_reject(query, entry, by=by)
    elif action == "snooze":
        await handle_snooze(query, entry, duration=args[0], by=by)
    else:
        await query.edit_message_text(f"❌ Acción desconocida: {action}")
```

### `handle_apply`

```python
async def handle_apply(query, entry, candidate_id: str, by: int):
    # 1. Lookup el value del candidato elegido
    chosen = next(
        (c for c in entry["expanded_candidates"] if c["id"] == candidate_id),
        None
    )
    if chosen is None:
        await query.edit_message_text(f"❌ Candidato {candidate_id} no encontrado")
        return
    
    # 2. Update audit_log: verdict approved
    update_audit_entry(entry["patch_id"], {
        "human_verdict": "approved",
        "verdict_at": now().isoformat(),
        "verdict_chose_candidate": candidate_id,
        "verdict_by": by,
    })
    
    # 3. Invocar la MCP tool
    try:
        result = await client.update_controller_config(
            bot_name=entry["bot_name"],
            controller_id=entry["controller_id"],
            field=entry["llm_proposal"]["dimension"],
            value=chosen["value"],
        )
    except Exception as e:
        await query.edit_message_text(
            f"❌ Apply falló: {e}\n\n"
            f"El verdict quedó como `approved` pero el cambio NO se aplicó. "
            f"Revisá logs y aplicá manualmente si corresponde."
        )
        update_audit_entry(entry["patch_id"], {
            "applied": False,
            "apply_error": str(e),
        })
        return
    
    # 4. Update audit_log: applied
    update_audit_entry(entry["patch_id"], {
        "applied": True,
        "applied_at": now().isoformat(),
        "apply_result": result,
    })
    
    # 5. Update last_changes.json (cooldowns futuros)
    upsert_last_changes(
        controller_id=entry["controller_id"],
        field=entry["llm_proposal"]["dimension"],
        old=entry["old_value"],
        new=chosen["value"],
        patch_id=entry["patch_id"],
    )
    
    # 6. Editar el mensaje confirmando
    field = entry["llm_proposal"]["dimension"]
    await query.edit_message_text(
        f"✅ Aplicado: `{field}` {entry['old_value']} → {chosen['value']}\n\n"
        f"Outcome será medido en 30 min.\n"
        f"Cooldown activo hasta {compute_cooldown_end(entry['controller_id'], field)}.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
```

### `handle_reject`

```python
async def handle_reject(query, entry, by: int):
    update_audit_entry(entry["patch_id"], {
        "human_verdict": "rejected",
        "verdict_at": now().isoformat(),
        "verdict_by": by,
    })
    
    # El rechazo TAMBIÉN dispara cooldown.
    # Razón: si el humano rechazó, no queremos volver a proponer lo mismo en el próximo tick.
    upsert_last_changes(
        controller_id=entry["controller_id"],
        field=entry["llm_proposal"]["dimension"],
        old=entry["old_value"],
        new=entry["old_value"],  # mismo valor — registramos solo para cooldown
        patch_id=entry["patch_id"],
        marker="rejected",
    )
    
    field = entry["llm_proposal"]["dimension"]
    cooldown_min = invariants.cooldowns.per_controller_per_field_minutes
    await query.edit_message_text(
        f"❌ Propuesta rechazada.\n\n"
        f"Cooldown de {cooldown_min} min para `{field}` en `{entry['controller_id']}`.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
```

**Nota**: el rechazo dispara cooldown estándar (Q4 confirmado: cooldowns son duros).

**Q2 confirmado**: no se pide razón al rechazar. Si el humano quiere comentar, lo hace en `learnings.md` manualmente.

### `handle_snooze`

```python
async def handle_snooze(query, entry, duration: str, by: int):
    parsed = parse_duration(duration)  # "1h" → timedelta(hours=1)
    snoozed_until = now() + parsed
    
    update_audit_entry(entry["patch_id"], {
        "human_verdict": "snoozed",
        "verdict_at": now().isoformat(),
        "snoozed_until_ts": snoozed_until.isoformat(),
        "verdict_by": by,
    })
    
    # Cooldown hasta el snooze_until (no el cooldown estándar)
    upsert_last_changes(
        controller_id=entry["controller_id"],
        field=entry["llm_proposal"]["dimension"],
        old=entry["old_value"],
        new=entry["old_value"],
        patch_id=entry["patch_id"],
        marker="snoozed",
        cooldown_until=snoozed_until.isoformat(),
    )
    
    await query.edit_message_text(
        f"⏸ Pospuesto hasta {snoozed_until.strftime('%H:%M')}\n\n"
        f"El agente puede volver a proponer después si el régimen persiste."
    )
```

### Timeout de propuestas pending (Q3 confirmado: 24h)

Tarea diferida en cada tick del agente:

```python
def expire_old_pending_proposals(audit_log_path: Path, now: datetime):
    timeout = timedelta(hours=24)
    for entry in read_jsonl(audit_log_path):
        if entry["human_verdict"] == "pending":
            sent_at = parse(entry["ts"])
            if now - sent_at > timeout:
                update_audit_entry(entry["patch_id"], {
                    "human_verdict": "expired",
                    "verdict_at": now.isoformat(),
                    "verdict_by": "auto_timeout",
                })
                # Opcional: editar el mensaje de Telegram para marcarlo como expired
                # await edit_telegram_msg_expired(entry)
```

Se ejecuta junto con el outcome loop (cada tick).

## Estados de la propuesta — state machine

```
                      ┌── apply ──┐
                      │           │
              pending ─├── reject  ┼─→ (final, audit_log update_in_place)
                      │           │
                      ├── snooze ─┤
                      │           │
                      └── expire ─┘
                                  ↓
                  applied: true (only after apply + MCP success)
                                  ↓
                  outcome_30min: {...} (after 30 min)
```

### Transiciones

| From | To | Trigger | Side effects |
|---|---|---|---|
| `pending` | `approved` | clic Apply + MCP success | `applied=true`, `last_changes.json` updated |
| `pending` | `rejected` | clic Reject | cooldown estándar registrado |
| `pending` | `snoozed` | clic Snooze | cooldown hasta `snoozed_until_ts` |
| `pending` | `expired` | timeout 24h | log only, sin cooldown |

## 3.3 — Persistencia del flow

Toda la persistencia ya está cubierta en `MEMORY_SPEC.md`. Acá solo aclaro el flow específico:

### Archivos involucrados

| Archivo | Cuándo se escribe | Operación |
|---|---|---|
| `state/audit_log.jsonl` | Al crear proposal, al recibir verdict, al aplicar, al medir outcome | append (creación) + update in-place (verdict, applied, outcome) |
| `state/last_changes.json` | Al apply exitoso o al reject/snooze | upsert atómico |
| `state/anti_evidence_log.jsonl` | Si todos los candidatos empeoraron al baseline (no llega a propose) | append |

### Update in-place del audit_log

```python
def update_audit_entry(patch_id: str, updates: dict, agent_dir: Path):
    """Atomic update: read all → mutate matching entry → atomic rewrite."""
    path = agent_dir / "state" / "audit_log.jsonl"
    entries = list(read_jsonl(path))
    found = False
    for entry in entries:
        if entry["patch_id"] == patch_id:
            entry.update(updates)
            found = True
            break
    if not found:
        log.error(f"audit_log: patch_id {patch_id} not found for update")
        return
    
    # Atomic rewrite (tempfile + rename)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    tmp.rename(path)
```

**Caveat de costo**: reescribir todo el archivo cada update. Aceptable mientras el archivo sea chico (<10k entries). Si crece, ver pendiente "audit_log split" en `MEMORY_SPEC.md`.

## Cooldowns — interacción con propose mode (Q4 confirmado: duros)

- Al rechazar → cooldown estándar (`per_controller_per_field_minutes`).
- Al snoozear → cooldown hasta `snoozed_until_ts`.
- Al aplicar → cooldown estándar.
- Cooldown activo bloquea propuestas nuevas en el disparador (`BACKTEST_LOOP_SPEC.md` § 2.5.1).

**No hay override** del cooldown via Telegram en MVP. Si en el futuro se necesita, se agrega con confirmación doble.

## Multi-controller en un mismo tick

Si en un tick varios controllers disparan proposals, **cada uno genera su propio mensaje de Telegram**. No se agrupan.

Razones:
- Cada uno tiene su patch_id y audit entry independiente.
- Botones de elección entre candidatos no escalan bien con grouping.
- Más simple parsear y trackear.

**Si en el futuro hay muchos controllers (5+) generando propuestas simultáneas**, considerar:
- Agrupar en un mensaje resumen + comando `/show <patch_id>` para detalle.
- Pero esto es post-MVP.

## Caveats y pendientes

### Caveats conocidos

1. **Mensaje no se edita después de timeout**: si el humano nunca responde, el mensaje en Telegram queda con botones "vivos" (aunque el handler los rechazará por verdict != pending). Mejorable: editar el mensaje al expirar para reflejar el estado, pero requiere persistir `message_id`.

2. **Apply falla silenciosamente para outcome loop**: si el apply falla, el outcome loop no debería medir nada. Implementar guard: solo medir outcome si `applied=true`.

3. **Concurrencia de clicks**: si dos humanos clickean a la vez (o el mismo doble-click), el guard `human_verdict != "pending"` previene doble-apply. Pero hay race condition entre el read y el write del audit_log. **Riesgo bajo** dado que las propuestas son raras y un único humano. Si en el futuro hay multi-usuario, agregar lock.

### Pendientes (revisar más adelante)

- **Mensaje compacto** (variante futura) para casos donde el formato completo es abrumador.
- **Botones de razón pre-armados** al rechazar (3 razones comunes).
- **Override de cooldown** via flujo de confirmación doble.
- **Edit del mensaje al expirar** para reflejar el estado en Telegram.
- **Multi-usuario / multi-chat**: hoy 1 chat. Si se necesitan varios humanos aprobando, requiere replanteo.
- **Group chats con voting**: 1 propuesta, N humanos votan, mayoría aplica. Post-MVP.
- **Schedule de auto-apply**: "aplicar automáticamente si no rechazo en 1h". Post-MVP, requiere mucho cuidado.

## Próximos pasos

1. **Fase 4** — MCP tool `update_controller_config` (lo que `handle_apply` invoca).
2. **Fase 5** — Implementación: handlers + persistence helpers.
