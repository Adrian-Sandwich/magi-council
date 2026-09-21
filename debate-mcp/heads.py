"""Registry de cabezas: qué asiento MAGI corre con qué proveedor.

La persona vive en el asiento (personas.py); el modelo que lo ocupa es
intercambiable. Este módulo resuelve, por asiento, QUÉ ejecutar: el binario
CLI y sus args estáticos. Si mañana Melchior pasa de claude a codex, se toca
sólo heads.json — el eje de decisión no se mueve.

Config (en orden de prioridad):
1. env DEBATE_HEADS — JSON inline, para pruebas y overrides puntuales;
2. debate-mcp/heads.json — la fuente del repo (fuente única);
3. DEFAULT_HEADS — claude y kimi en los mismos paths que usaba relay.py
   antes del registry (casper comparte el binario de claude: la persona, no
   el proveedor, es lo que lo diferencia de melchior).

Un asiento cuyo binario no existe queda inactivo: las decisiones se abren con
los asientos vivos (modo degradado, anotado en el dossier) en vez de colgar
esperando un proceso que nunca va a arrancar.
"""

import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

DEFAULT_HEADS = [
    {
        "seat": "melchior",
        "name": "claude",
        "type": "cli",
        "bin": "~/.local/bin/claude",
        "args": ["-p", "--allowedTools", "mcp__debate__*,Read,Grep,Glob"],
    },
    {
        "seat": "balthasar",
        "name": "kimi",
        "type": "cli",
        "bin": "~/.kimi-code/bin/kimi",
        "args": ["-p"],
    },
    {
        "seat": "casper",
        "name": "claude",
        "type": "cli",
        "bin": "~/.local/bin/claude",
        "args": ["-p", "--allowedTools", "mcp__debate__*,Read,Grep,Glob"],
    },
]

CONFIG_PATH = BASE_DIR / "heads.json"


def load() -> list[dict]:
    """La lista de asientos normalizada (bin con ~ expandido)."""
    raw = os.environ.get("DEBATE_HEADS")
    if raw:
        data = json.loads(raw)
    elif CONFIG_PATH.exists():
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    else:
        data = {"seats": DEFAULT_HEADS}
    seats = data.get("seats", data) if isinstance(data, dict) else data
    out = []
    for s in seats:
        seat = dict(s)
        if seat.get("bin"):
            seat["bin"] = str(Path(seat["bin"]).expanduser())
        seat.setdefault("type", "cli")
        seat.setdefault("args", [])
        out.append(seat)
    return out


def seat_names(seats: list[dict] | None = None) -> list[str]:
    return [s["seat"] for s in (seats if seats is not None else load())]


def is_active(seat: dict) -> bool:
    """Puede dispararse de verdad. CLI: binario presente en disco. API:
    model, URL del proveedor y, si el proveedor la pide, la clave en su
    variable de entorno (un Ollama local no la necesita)."""
    if seat.get("type") == "api":
        import apihead  # proveedor, URL y clave: la regla vive junto al cliente
        return apihead.is_configured(seat)[0]
    return bool(seat.get("bin") and Path(seat["bin"]).exists())


def active_seats(seats: list[dict] | None = None) -> list[dict]:
    return [s for s in (seats if seats is not None else load()) if is_active(s)]


def seat_by_name(name: str, seats: list[dict] | None = None) -> dict | None:
    for s in (seats if seats is not None else load()):
        if s["seat"] == name:
            return s
    return None
