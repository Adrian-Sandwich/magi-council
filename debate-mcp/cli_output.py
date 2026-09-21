"""Salida estructurada de las cabezas CLI: texto de la respuesta más uso real.

En modo texto un CLI no dice cuántos tokens gastó, así que metrics.py sólo
tenía la aproximación por caracteres. Con las cuentas de siempre (sin claves
API) dos de los tres CLIs sí lo cuentan, si se les pide JSON:

- claude: `--output-format json` devuelve UN objeto con `result` (la
  respuesta), `usage` (tokens de entrada, de caché y de salida),
  `total_cost_usd` (a precio de lista) e `is_error`.
- codex exec: `--json` emite JSONL: `item.completed` con los mensajes del
  agente y `turn.completed` con `usage`.
- kimi: no reporta uso en ningún formato; queda en modo texto.

`output_format` del asiento en heads.json elige el parser; `flags()` da los
argumentos que hay que agregar al comando. El relay lo aplica sólo a los
turnos por stdin (`_run_cli_inline`), no al ejecutor, cuyo log se lee a mano.
"""

import json

FORMATS = {
    "text": [],
    "claude-json": ["--output-format", "json"],
    "codex-jsonl": ["--json"],
}


def flags(fmt: str | None) -> list[str]:
    fmt = fmt or "text"
    if fmt not in FORMATS:
        raise ValueError(f"output_format desconocido {fmt!r} (válidos: {', '.join(FORMATS)})")
    return list(FORMATS[fmt])


def parse(fmt: str | None, raw: str) -> dict:
    """{'text', 'input_tokens', 'output_tokens', 'cached_input_tokens',
    'cost_usd', 'error'}. Sin formato o sin JSON reconocible devuelve el texto
    tal cual: un parser que no entiende no puede tirar un voto válido."""
    fmt = fmt or "text"
    empty = {"text": raw or "", "input_tokens": None, "output_tokens": None,
             "cached_input_tokens": None, "cost_usd": None, "error": None}
    if fmt == "claude-json":
        return _claude_json(raw, empty)
    if fmt == "codex-jsonl":
        return _codex_jsonl(raw, empty)
    return empty


def _json_objects(raw: str) -> list[dict]:
    """Objetos JSON por línea; stderr va mezclado en el mismo stdout, así que
    las líneas que no son JSON se ignoran."""
    out = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _claude_json(raw: str, empty: dict) -> dict:
    objs = [o for o in _json_objects(raw) if o.get("type") == "result"]
    if not objs:
        # claude imprime el objeto en una línea; si vino con saltos, último recurso
        start, end = (raw or "").find("{"), (raw or "").rfind("}")
        if start >= 0 and end > start:
            try:
                obj = json.loads(raw[start:end + 1])
                objs = [obj] if isinstance(obj, dict) and obj.get("type") == "result" else []
            except ValueError:
                objs = []
    if not objs:
        return empty
    obj = objs[-1]
    usage = obj.get("usage") or {}
    result = obj.get("result")
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    cached = _int(usage.get("cache_read_input_tokens"))
    context = sum(_int(usage.get(k)) or 0 for k in ("input_tokens", "cache_creation_input_tokens",
                                                     "cache_read_input_tokens"))
    error = None
    if obj.get("is_error") or (obj.get("subtype") and obj["subtype"] != "success"):
        error = text or str(obj.get("subtype") or "error")
    return {"text": text or "", "input_tokens": context if usage else None,
            "output_tokens": _int(usage.get("output_tokens")), "cached_input_tokens": cached,
            "cost_usd": _float(obj.get("total_cost_usd")), "error": error}


def _codex_jsonl(raw: str, empty: dict) -> dict:
    objs = _json_objects(raw)
    if not objs:
        return empty
    messages = []
    usage = None
    error = None
    for obj in objs:
        kind = obj.get("type")
        if kind == "item.completed":
            item = obj.get("item") or {}
            if item.get("type") == "agent_message" and item.get("text"):
                messages.append(item["text"])
        elif kind == "turn.completed":
            usage = obj.get("usage") or usage
        elif kind == "error":
            error = obj.get("message") or "error"
        elif kind == "turn.failed":
            error = (obj.get("error") or {}).get("message") or "turn.failed"
    usage = usage or {}
    return {"text": "\n\n".join(messages), "input_tokens": _int(usage.get("input_tokens")),
            "output_tokens": _int(usage.get("output_tokens")),
            "cached_input_tokens": _int(usage.get("cached_input_tokens")),
            "cost_usd": None, "error": error}


def _int(v):
    return int(v) if isinstance(v, (int, float)) else None


def _float(v):
    return float(v) if isinstance(v, (int, float)) else None
