"""Journal de decisiones de ajuste de grillas chessboard_lite (Fase A, sqlite local).

Cada apply/decisión guarda el draft, el cuadro de impacto completo (KPIs +
reglas disparadas + versión del motor) y si el operador siguió o contradijo el
semáforo. Es el insumo de la routine de outcome (Fase C): PnL real vs
contrafáctico → calidad de decisión y calibración del veredicto.

sqlite en ``data/grid_adjustments.sqlite`` (root del repo condor). La decisión
sqlite-vs-Postgres del server sigue abierta en la spec; este módulo aísla el
storage para que migrar sea cambiar solo este archivo.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# condor/web/chessboard_journal.py → repo root = tres niveles arriba.
_DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "grid_adjustments.sqlite"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS adjustments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    server        TEXT NOT NULL,
    grid_id       TEXT NOT NULL,
    draft_json    TEXT,
    impact_json   TEXT,
    verdict       TEXT,
    accepted      INTEGER,
    contradicted  INTEGER,
    note          TEXT,
    rules_version TEXT
)
"""


def _connect() -> sqlite3.Connection:
    """Conexión con el schema garantizado (crea dir/tabla si no existen)."""
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.execute(_SCHEMA)
    return conn


def record_adjustment(server: str, grid_id: str, draft: dict, impact: dict,
                      accepted: bool, contradicted: bool,
                      note: Optional[str] = None) -> int:
    """Registra una decisión y devuelve su id. verdict/rules_version se
    desnormalizan del impact para poder consultar sin parsear JSON."""
    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT INTO adjustments (ts, server, grid_id, draft_json, impact_json,"
            " verdict, accepted, contradicted, note, rules_version)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(),
                server,
                grid_id,
                json.dumps(draft, default=str),
                json.dumps(impact, default=str),
                (impact or {}).get("verdict"),
                int(bool(accepted)),
                int(bool(contradicted)),
                note,
                (impact or {}).get("rules_version"),
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def list_adjustments(server: Optional[str] = None, limit: int = 50) -> list[dict[str, Any]]:
    """Últimas decisiones (más nuevas primero), opcionalmente filtradas por server."""
    conn = _connect()
    conn.row_factory = sqlite3.Row
    try:
        if server:
            rows = conn.execute(
                "SELECT * FROM adjustments WHERE server = ? ORDER BY id DESC LIMIT ?",
                (server, int(limit)),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM adjustments ORDER BY id DESC LIMIT ?", (int(limit),)
            ).fetchall()
    finally:
        conn.close()

    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        for key in ("draft_json", "impact_json"):
            try:
                d[key.replace("_json", "")] = json.loads(d.pop(key) or "null")
            except Exception:
                d[key.replace("_json", "")] = None
        d["accepted"] = bool(d.get("accepted"))
        d["contradicted"] = bool(d.get("contradicted"))
        out.append(d)
    return out
