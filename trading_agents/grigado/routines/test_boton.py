# ⚠️ RECUPERACIÓN PARCIAL — solo el fragmento de un Edit; archivo incompleto
"""Test de botón inline."""
import json
import os
import httpx
from pydantic import BaseModel
from telegram.ext import ContextTypes
from routines.base import RoutineResult

class Config(BaseModel):
    """Test botón inline."""
    pass

async def run(config: Config, context: ContextTypes.DEFAULT_TYPE) -> RoutineResult:
    chat_id = context._chat_id if hasattr(context, "_chat_id") else None
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not chat_id or not token:
        return RoutineResult(text=f"sin chat_id={chat_id} o token")

    payload = {
        "chat_id": chat_id,
        "text": "¿Ves el botón abajo?",
        "reply_markup": json.dumps({
            "inline_keyboard": [[
                {"text": "✅ Test botón", "callback_data": "routines:test_boton:ok"}
            ]]
        }),
    }
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json=payload,
        )
    data = resp.json()
    return RoutineResult(text=f"resp: ok={data.get('ok')} desc={data.get('description','')}")

async def handle_callback(update, context: ContextTypes.DEFAULT_TYPE, action: str, params: list) -> None:
    query = update.callback_query
    await query.answer("✅ Funciona!")
    await query.edit_message_text("¡Botón funcionando! ✅")