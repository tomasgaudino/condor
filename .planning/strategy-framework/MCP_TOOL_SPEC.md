# MCP Tool — `update_controller_config`

> Spec de la única MCP tool nueva que necesita el framework adaptativo. El resto reusa lo que Condor ya tiene.
> Última actualización: 2026-05-09

## Filosofía

Condor ya hace el 80% del trabajo. Solo necesitamos un wrapper que agregue:
- Validación de whitelist `is_updatable` (silent-ignore protection).
- Type coercion (la API server no valida tipos).
- Optimistic-lock opcional (detección de carreras).
- Caveats automáticos por campo (efecto gradual, latencia, etc.).

**No requiere cambios en Hummingbot ni en hummingbot-api**. Solo código nuevo en Condor.

## 4.1 — Ubicación

**Archivo nuevo**: `mcp_servers/hummingbot_api/tools/adaptive_agent.py`

Razón: encapsula tooling específico del agente adaptativo. Si en el futuro se agregan más tools del agente (ej: `pause_controller`, `query_audit_log`), van todas acá.

**Módulo de metadata**: `mcp_servers/hummingbot_api/tools/_controller_field_metadata.py`

Contiene:
- Whitelist de campos `is_updatable` por tipo de controller.
- Caveats por (controller_type, field).
- Type map por (controller_type, field) — opcional, fallback al tipo del valor actual.

**Registro**: agregar a `mcp_servers/hummingbot_api/__init__.py` o equivalente, como las demás tools.

## 4.2 — API de la tool

### Signature

```python
async def update_controller_config(
    client: Any,                      # HummingbotAPIClient
    bot_name: str,
    controller_id: str,               # = config_name dentro del bot
    field: str,
    value: Any,
    expected_old_value: Any | None = None,  # optimistic-lock opcional
) -> dict[str, Any]:
    """Update a single is_updatable field of a running controller.
    
    Returns a dict with success/error structure (never raises for domain errors).
    """
```

### Inputs

| Parámetro | Tipo | Descripción |
|---|---|---|
| `bot_name` | `str` | Nombre del bot Hummingbot corriendo (ej: `pmm-btc-1`) |
| `controller_id` | `str` | ID del controller dentro del bot (= config_name del YAML) |
| `field` | `str` | Nombre del campo a modificar |
| `value` | `Any` | Nuevo valor (será coerced al tipo correcto) |
| `expected_old_value` | `Any \| None` | Si está presente, valida que el valor actual coincide antes de aplicar (default `None` = sin lock) |

### Outputs — éxito

```python
{
    "success": True,
    "bot_name": "pmm-btc-1",
    "controller_id": "001_pmm_binance_BTC-USDT",
    "field": "take_profit",
    "old_value": 0.0003,
    "new_value": 0.00045,
    "applied_at": "2026-05-09T14:35:42Z",
    "hot_reload_eta_seconds": 10,
    "caveats": [
        "take_profit only affects executors created after reload (~10s). Active executors retain their original TP."
    ],
    "raw_api_result": {...}      # respuesta cruda del API por si hace falta
}
```

### Outputs — error

```python
{
    "success": False,
    "error_code": "<código>",
    "message": "<descripción humano-readable>",
    "details": {...}              # contexto extra para debugging
}
```

#### Códigos de error

| `error_code` | Cuándo |
|---|---|
| `bot_not_found` | El `bot_name` no existe o no está corriendo |
| `controller_not_found` | El `controller_id` no existe en ese bot |
| `field_not_updatable` | El `field` no está en la whitelist `is_updatable` (silent-ignore protection) |
| `field_forbidden` | El `field` está en la blacklist absoluta (manual_kill_switch, leverage, etc.) |
| `type_coercion_failed` | El `value` no se puede coerce al tipo esperado |
| `value_changed_externally` | `expected_old_value` no coincide con el valor actual (carrera detectada) |
| `validation_failed` | El config mergeado no pasa Pydantic validation del controller |
| `api_error` | Error genérico del API de Hummingbot al escribir |

**Convención**: dominio = dict, bugs internos = raise. El agente puede leer los códigos y decidir.

## Algoritmo paso a paso

```python
async def update_controller_config(client, bot_name, controller_id, field, value, expected_old_value=None):
    # 1. Verificar bot existe
    bot_status = await client.bot_orchestration.get_bot_status(bot_name)
    if not bot_status:
        return error("bot_not_found", f"Bot {bot_name} not found or not running")
    
    # 2. Fetch configs del bot
    configs = await client.controllers.get_bot_controller_configs(bot_name)
    cfg = next((c for c in configs if c.get("id") == controller_id or c.get("_config_name") == controller_id), None)
    if cfg is None:
        return error("controller_not_found", f"Controller {controller_id} not in bot {bot_name}")
    
    controller_type = cfg["controller_type"]
    controller_name = cfg["controller_name"]
    old_value = cfg.get(field)
    
    # 3. Whitelist + blacklist check
    metadata = get_metadata_for(controller_name)  # del módulo _controller_field_metadata
    
    if field in metadata["forbidden_fields"]:
        return error("field_forbidden", f"Field {field} is on the absolute blacklist")
    
    if field not in metadata["updatable_fields"]:
        return error(
            "field_not_updatable",
            f"Field {field} is not is_updatable for controller {controller_name}. "
            f"Writing it would silently no-op until next bot restart."
        )
    
    # 4. Type coercion
    try:
        coerced = coerce_value(value, expected_type=type(old_value), field=field, controller=controller_name)
    except (ValueError, TypeError) as e:
        return error("type_coercion_failed", str(e), details={"field": field, "value": value})
    
    # 5. Optimistic-lock (si aplica)
    if expected_old_value is not None:
        if not values_equal_with_tolerance(old_value, expected_old_value):
            return error(
                "value_changed_externally",
                f"Expected old value {expected_old_value} but found {old_value}",
                details={"current_value": old_value, "expected": expected_old_value}
            )
    
    # 6. Validate via HB API (config completo mergeado)
    merged = {**cfg, field: coerced}
    try:
        validation = await client.controllers.validate_controller_config(controller_type, controller_name, merged)
        if not validation.get("valid", True):
            return error("validation_failed", validation.get("message", "Pydantic validation failed"), details=validation)
    except Exception as e:
        # Si el endpoint de validación no existe o falla, seguimos pero loggeamos
        log.warning(f"Validation endpoint failed: {e}. Proceeding without pre-validation.")
    
    # 7. Apply (payload mínimo, aprovechando merge shallow del endpoint HB)
    try:
        result = await client.controllers.update_bot_controller_config(
            bot_name=bot_name,
            config_name=controller_id,
            config={field: coerced}    # <-- solo el field a cambiar
        )
    except Exception as e:
        return error("api_error", str(e))
    
    # 8. Build response con caveats
    caveats = metadata.get("caveats_by_field", {}).get(field, [])
    return {
        "success": True,
        "bot_name": bot_name,
        "controller_id": controller_id,
        "field": field,
        "old_value": old_value,
        "new_value": coerced,
        "applied_at": datetime.utcnow().isoformat(),
        "hot_reload_eta_seconds": 10,    # config_update_interval del strategy
        "caveats": caveats,
        "raw_api_result": result,
    }
```

## 4.3 — Manejo de errores

### Política unificada

Todos los errores de dominio devuelven dict con `success: False`. **Nunca raise** para que el agente pueda razonar sobre el error.

Bugs internos del MCP server (ej: TypeError no esperado) sí raise — eso es responsabilidad del que llama.

### Atomicidad

Como el endpoint de Hummingbot hace merge shallow y solo mandamos `{field: value}`, la operación es **atómica naturalmente**:
- Si valida → escribe → success.
- Si no valida → no escribe → error.
- No hay estados intermedios donde el config quede parcialmente escrito.

### Sin rollback automático

Si después del apply, el strategy v2 falla a parsear el YAML (caso raro: validación pasó pero hay bug en strategy), **no hay rollback automático**.

Mitigación:
- El agente persiste `old_value` en `audit_log.jsonl` (ya está en spec).
- En caso de problema, el agente puede llamar la tool de nuevo con `old_value` para revertir.
- Outcome loop puede detectar deterioro y alertar.

### Caveats globales (siempre devueltos cuando aplican)

Por field específico, además del caveat técnico:

| Field | Caveat |
|---|---|
| `take_profit` | "Solo afecta executors creados después del reload (~10s). Los activos retienen su TP original." |
| `take_profit_order_type` | "Idem `take_profit`: solo executors nuevos." |
| `open_order_type` | "Idem `take_profit`: solo executors nuevos." |
| `tick_mode` | "Cambio de `tick_mode` afecta el cálculo de spreads. Verificar que los spreads tengan sentido en el nuevo modo." |
| `position_profit_protection` | "Cambio binario. Si pasaba a `true` con inventory adverso, puede generar bloqueo temporal de un lado." |

Plus el caveat global por hot-reload:
- "Hot-reload puede tardar hasta 10 segundos en aplicarse al bot corriendo."

## Módulo de metadata — `_controller_field_metadata.py`

```python
"""Metadata estática por controller para la tool update_controller_config.

Esta tabla es la fuente de verdad técnica:
- updatable_fields: del Pydantic schema de Hummingbot (is_updatable: True).
- forbidden_fields: política nuestra (decisiones D7, D11).
- caveats_by_field: documentado en CONTROLLER_DEEP_DIVE.md.
"""

from typing import Any

# ─────────────────────────────────────────────────────────────────────
# pmm_mister
# ─────────────────────────────────────────────────────────────────────

PMM_MISTER_UPDATABLE = {
    # Spreads y amounts
    "buy_spreads", "sell_spreads", "buy_amounts_pct", "sell_amounts_pct",
    # Take profit
    "take_profit", "take_profit_order_type", "open_order_type",
    # Inventory targets
    "target_base_pct", "min_base_pct", "max_base_pct", "min_skew",
    # Capital
    "portfolio_allocation", "total_amount_quote", "max_active_executors_by_level",
    # Timing
    "executor_refresh_time", "buy_cooldown_time", "sell_cooldown_time",
    "buy_position_effectivization_time", "sell_position_effectivization_time",
    # Tolerancias
    "price_distance_tolerance", "refresh_tolerance", "tolerance_scaling",
    # Modo y protección
    "tick_mode", "position_profit_protection",
    # Globals (technically updatable, used by ControllerBase)
    "leverage",
}

# Política nuestra: nunca tocar via agent (consistente con D7 y AGENT_SCHEMA_SPEC)
ABSOLUTE_FORBIDDEN = {
    "manual_kill_switch",
    "leverage",                 # is_updatable pero política humano-only
    "connector_name",
    "trading_pair",
    "position_mode",
    "controller_name",
    "controller_type",
    "id",
    "global_take_profit",
    "global_stop_loss",
}

CAVEATS = {
    ("pmm_mister", "take_profit"): [
        "Solo afecta executors creados después del reload (~10s). Los activos retienen su TP original."
    ],
    ("pmm_mister", "take_profit_order_type"): [
        "Solo afecta executors creados después del reload."
    ],
    ("pmm_mister", "open_order_type"): [
        "Solo afecta executors creados después del reload."
    ],
    ("pmm_mister", "tick_mode"): [
        "Cambio de tick_mode afecta cálculo de spreads. Verificar que los spreads existentes tengan sentido en el nuevo modo."
    ],
    ("pmm_mister", "position_profit_protection"): [
        "Cambio binario. Pasar a true con inventory adverso puede bloquear temporalmente un lado."
    ],
}

GLOBAL_CAVEATS = [
    "Hot-reload puede tardar hasta 10 segundos en aplicarse al bot corriendo.",
]


def get_metadata_for(controller_name: str) -> dict[str, Any]:
    if controller_name == "pmm_mister":
        return {
            "updatable_fields": PMM_MISTER_UPDATABLE,
            "forbidden_fields": ABSOLUTE_FORBIDDEN,
            "caveats_by_field": {
                field: CAVEATS.get((controller_name, field), [])
                for field in PMM_MISTER_UPDATABLE
            },
            "global_caveats": GLOBAL_CAVEATS,
        }
    raise NotImplementedError(f"Controller {controller_name} not yet supported")
```

## Type coercion

```python
def coerce_value(value: Any, expected_type: type, field: str, controller: str) -> Any:
    """Coerce value to expected type, raising ValueError if impossible."""
    
    if expected_type is bool:
        # Strict bool — no truthy
        if not isinstance(value, bool):
            raise ValueError(f"Field {field} requires strict bool, got {type(value).__name__}")
        return value
    
    if expected_type is int:
        if isinstance(value, bool):
            raise ValueError(f"Field {field} expects int, got bool")
        return int(value)
    
    if expected_type is float:
        return float(value)
    
    if expected_type is str:
        return str(value)
    
    if expected_type is list:
        # Listas como buy_spreads — debe venir como list o str CSV
        if isinstance(value, list):
            return [float(v) for v in value]
        if isinstance(value, str):
            return [float(v.strip()) for v in value.split(",")]
        raise ValueError(f"Field {field} expects list or CSV string, got {type(value).__name__}")
    
    # Fallback: try direct construction
    try:
        return expected_type(value)
    except Exception as e:
        raise ValueError(f"Cannot coerce {value} to {expected_type.__name__} for field {field}: {e}")
```

## Optimistic-lock — values_equal_with_tolerance

```python
def values_equal_with_tolerance(actual: Any, expected: Any, tol: float = 1e-9) -> bool:
    """Equality with float tolerance."""
    if isinstance(actual, float) and isinstance(expected, float):
        return abs(actual - expected) < tol
    return actual == expected
```

## Cómo lo usa el agente

### Llamada típica

```python
result = await update_controller_config(
    client=mcp_client,
    bot_name="pmm-btc-1",
    controller_id="001_pmm_binance_BTC-USDT",
    field="take_profit",
    value=0.00045,
    expected_old_value=0.0003,    # opcional, para detectar carreras
)

if result["success"]:
    log.info(f"Applied: {result['old_value']} → {result['new_value']}")
    for caveat in result["caveats"]:
        log.warn(caveat)
else:
    log.error(f"Apply failed: {result['error_code']} — {result['message']}")
```

### Integración con el handler de propose

En `handle_apply` del modo `propose` (`PROPOSE_MODE_SPEC.md` § 3.2):

```python
result = await update_controller_config(
    client=client,
    bot_name=entry["bot_name"],
    controller_id=entry["controller_id"],
    field=entry["llm_proposal"]["dimension"],
    value=chosen["value"],
    expected_old_value=entry["old_value"],   # del audit_log
)

if result["success"]:
    update_audit_entry(entry["patch_id"], {
        "applied": True,
        "applied_at": result["applied_at"],
        "apply_result": result,
    })
    # Editar mensaje Telegram con confirmación + caveats
    caveats_text = "\n".join(f"• {c}" for c in result["caveats"])
    await query.edit_message_text(
        f"✅ Aplicado: `{result['field']}` {result['old_value']} → {result['new_value']}\n\n"
        f"⏱️ Hot-reload ETA: {result['hot_reload_eta_seconds']}s\n\n"
        f"⚠️ Caveats:\n{caveats_text}\n\n"
        f"Outcome será medido en 30 min.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
else:
    update_audit_entry(entry["patch_id"], {
        "applied": False,
        "apply_error": result,
    })
    await query.edit_message_text(
        f"❌ Apply falló: `{result['error_code']}`\n\n"
        f"{result['message']}\n\n"
        f"El verdict quedó como `approved` pero el cambio NO se aplicó.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
```

## Pendientes (revisar más adelante)

- **Otros controllers**: hoy solo `pmm_mister`. Cuando se agreguen otros adaptables, extender `_controller_field_metadata.py` con sus whitelists y caveats.
- **Validación de rangos por field**: además del tipo, validar rangos (ej: `target_base_pct ∈ [0, 1]`). Hoy lo hace Pydantic via `validate_controller_config` API call. Considerar si vale la pena hacer pre-check local antes de llamar al API server.
- **Multi-field update atómico**: hoy single-field por call. Para multi-field patches (post-MVP), wrappear en una llamada que aplique todos o ninguno.
- **Caveats dinámicos**: caveats que dependen del estado actual (ej: "estás cerca del oversub") deberían generarse en runtime, no de tabla estática. Postpuesto.
- **Rollback helper**: tool dedicada `revert_last_change` que lea `last_changes.json` y revierta. Útil en emergencias.
- **Testing**: el endpoint hace merge shallow — verificar comportamiento ante listas y nested dicts. Para `pmm_mister`, todos los `is_updatable` son scalars o listas top-level, así que el merge es seguro.

## Próximos pasos

1. **Fase 5** — Implementación: escribir `adaptive_agent.py` y `_controller_field_metadata.py`, registrar en MCP server.
2. Testing: unit tests con mocked client + integration test contra un bot real en testnet.
