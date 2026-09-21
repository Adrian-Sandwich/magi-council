"""Ejecutor de turnos para asientos type="api": OpenAI, Moonshot (kimi),
Anthropic (claude) y cualquier endpoint OpenAI-compatible local (Ollama).

A diferencia de un asiento CLI (un proceso con herramientas que lee el
journal por MCP), un asiento API no puede llamar al MCP: el relay le inyecta
el contexto en el prompt —journal inline + pregunta + artefacto— y el modelo
responde con un voto estructurado:

    POSITION: yes|no|conditional|info
    CONDITIONS: <condiciones separadas por ;>   (sólo si position=conditional)
    <razonamiento libre>

El tag POSITION debe ser explícito. Una respuesta sin voto válido es un
fallo de turno, nunca un voto informativo inventado por el coordinador.

Sólo stdlib (urllib): las cabezas API no le agregan dependencias al venv.
Funciona con Ollama (http://localhost:11434/v1), LM Studio, llama.cpp server
o cualquier endpoint OpenAI-compatible, local o remoto, y con la Messages
API de Anthropic. Un asiento se configura en heads.json:

    {"seat": "casper", "type": "api", "provider": "anthropic",
     "model": "claude-opus-4-1", "api_key_env": "ANTHROPIC_API_KEY",
     "pricing": {"input_per_mtok": 15, "output_per_mtok": 75}}

`provider` fija la URL base, el estilo de la API y la variable con la
clave (openai, moonshot, anthropic; `local` = OpenAI-compatible sin clave).
Cada turno devuelve tokens reales de entrada y salida y, con `pricing`
(USD por millón de tokens), el costo estimado. 429, 5xx y timeouts se
reintentan con backoff (respetando Retry-After); un 4xx de configuración
(401, 400) no.
"""

import json
import os
import re
import time
import urllib.error
import urllib.request

import personas

POSITION_RE = re.compile(
    r"^[ \t]*(?:[•*-][ \t]+)?POSITION[ \t]*:[ \t]*(yes|no|conditional|info)[ \t\r]*$", re.IGNORECASE | re.MULTILINE
)
CONDITIONS_RE = re.compile(r"^\s*CONDITIONS\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)

DEFAULT_TIMEOUT_SECS = 600
JOURNAL_LIMIT = 15        # mensajes del journal que entran al prompt
JOURNAL_CHAR_LIMIT = 18_000
JOURNAL_MESSAGE_CHAR_LIMIT = 6_000
BODY_CHARS = 2000         # tope por mensaje inlineado
# Posiciones de rondas ANTERIORES en el journal de una cabeza: se resumen a
# cabeza + cola (el voto y las condiciones sobreviven; la argumentación larga
# no). Tres posiciones de 6k chars costaban ~4.5k tokens por cabeza en cada
# ronda 2+; con esto quedan en ~1k.
POSITION_DIGEST_HEAD = 900
POSITION_DIGEST_TAIL = 400


def parse_vote(text: str) -> dict:
    """Extrae position/conditions de la respuesta del modelo.

    Rechaza respuestas vacías o sin un voto explícito; usa el voto final.
    """
    matches = list(POSITION_RE.finditer(text or ""))
    if not matches:
        raise ValueError('La cabeza terminó sin un voto POSITION válido. Revisa el formato de la respuesta.')
    m = matches[-1]
    position = m.group(1).lower()
    conditions = None
    if position == "conditional":
        cm = CONDITIONS_RE.search(text, m.end())
        if cm:
            conditions = [c.strip() for c in cm.group(1).split(";") if c.strip()]
    return {"position": position, "conditions": conditions, "body": (text or "").strip()}


PROVIDERS = {
    "openai": {"base_url": "https://api.openai.com/v1", "key_env": "OPENAI_API_KEY", "style": "openai"},
    "moonshot": {"base_url": "https://api.moonshot.ai/v1", "key_env": "MOONSHOT_API_KEY", "style": "openai"},
    "anthropic": {"base_url": "https://api.anthropic.com", "key_env": "ANTHROPIC_API_KEY", "style": "anthropic"},
    # Ollama, LM Studio, llama.cpp: OpenAI-compatible sin clave; base_url obligatoria
    "local": {"base_url": None, "key_env": None, "style": "openai"},
}
ANTHROPIC_VERSION = "2023-06-01"
MAX_OUTPUT_TOKENS = 4096
RETRY_ATTEMPTS = 3
RETRY_BASE_SECS = 2.0
RETRY_STATUSES = {408, 409, 429, 500, 502, 503, 504}
_sleep = time.sleep  # los tests lo reemplazan


class ApiError(OSError):
    """Fallo definitivo de una llamada API (tras los reintentos que tocaban)."""

    def __init__(self, message: str, status: int | None = None, attempts: int = 1):
        super().__init__(message)
        self.status = status
        self.attempts = attempts


def provider_of(seat: dict) -> dict:
    """Proveedor efectivo del asiento: `provider` explícito, o `local` cuando
    sólo hay base_url (un Ollama), o `openai` por defecto."""
    name = seat.get("provider") or ("local" if seat.get("base_url") else "openai")
    if name not in PROVIDERS:
        raise ValueError(f"proveedor desconocido {name!r} (válidos: {', '.join(PROVIDERS)})")
    spec = dict(PROVIDERS[name])
    spec["name"] = name
    spec["base_url"] = seat.get("base_url") or spec["base_url"]
    spec["key_env"] = seat.get("api_key_env") or spec["key_env"]
    return spec


def api_key_for(seat: dict) -> str | None:
    env = provider_of(seat)["key_env"]
    return os.environ.get(env) if env else None


def is_configured(seat: dict) -> tuple[bool, str]:
    """(ok, por qué no). Lo que healthcheck imprime y lo que decide si el
    asiento se sienta en decisiones nuevas."""
    if not seat.get("model"):
        return False, "falta `model`"
    try:
        spec = provider_of(seat)
    except ValueError as exc:
        return False, str(exc)
    if not spec["base_url"]:
        return False, "falta `base_url` (proveedor local)"
    if spec["key_env"] and not os.environ.get(spec["key_env"]):
        return False, f"falta la variable {spec['key_env']} (clave del proveedor {spec['name']})"
    return True, f"{spec['name']} · {seat['model']}"


def _retry_after(headers) -> float | None:
    try:
        value = headers.get("Retry-After") if headers else None
        return float(value) if value else None
    except (TypeError, ValueError):
        return None


def _post(url: str, headers: dict, payload: dict, timeout_secs: int) -> tuple[dict, int]:
    """POST JSON con reintentos: 429/5xx/timeout/conexión rechazada se
    reintentan con backoff exponencial (o Retry-After); otros 4xx no, son de
    configuración. Devuelve (respuesta, intentos)."""
    body = json.dumps(payload).encode("utf-8")
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout_secs) as resp:
                return json.loads(resp.read().decode("utf-8")), attempt
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            if exc.code not in RETRY_STATUSES or attempt == RETRY_ATTEMPTS:
                raise ApiError(f"HTTP {exc.code} de {url}: {detail}", exc.code, attempt) from None
            delay = _retry_after(exc.headers) or RETRY_BASE_SECS * 2 ** (attempt - 1)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt == RETRY_ATTEMPTS:
                raise ApiError(f"sin respuesta de {url}: {exc}", None, attempt) from exc
            delay = RETRY_BASE_SECS * 2 ** (attempt - 1)
        _sleep(delay)
    raise ApiError(f"sin respuesta de {url}", None, RETRY_ATTEMPTS)  # inalcanzable


def cost_usd(seat: dict, input_tokens, output_tokens) -> float | None:
    """Costo estimado con `pricing` del asiento (USD por millón de tokens);
    None si el asiento no trae precios o la API no devolvió uso."""
    pricing = seat.get("pricing") or {}
    if input_tokens is None or output_tokens is None or not pricing:
        return None
    return round(input_tokens / 1e6 * float(pricing.get("input_per_mtok") or 0)
                 + output_tokens / 1e6 * float(pricing.get("output_per_mtok") or 0), 6)


def complete(seat: dict, system: str, user: str, timeout_secs: int | None = None) -> dict:
    """Una respuesta del modelo del asiento. Devuelve texto, tokens reales,
    costo estimado, intentos y proveedor: lo que metrics.py necesita para
    poner una columna de costo con datos de verdad."""
    spec = provider_of(seat)
    key = api_key_for(seat)
    if spec["key_env"] and not key:
        raise ApiError(f"falta la variable {spec['key_env']} (clave del proveedor {spec['name']})", 401)
    if not spec["base_url"]:
        raise ApiError("falta base_url (proveedor local)")
    timeout_secs = timeout_secs or seat.get("timeout_secs", DEFAULT_TIMEOUT_SECS)
    temperature = seat.get("temperature", 0.2)  # votos, no poesía
    if spec["style"] == "anthropic":
        url = spec["base_url"].rstrip("/") + "/v1/messages"
        headers = {"content-type": "application/json", "x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}
        payload = {"model": seat["model"], "max_tokens": int(seat.get("max_tokens") or MAX_OUTPUT_TOKENS),
                   "system": system, "messages": [{"role": "user", "content": user}], "temperature": temperature}
        data, attempts = _post(url, headers, payload, timeout_secs)
        text = "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text")
        usage = data.get("usage") or {}
        tokens_in, tokens_out = usage.get("input_tokens"), usage.get("output_tokens")
    else:
        url = spec["base_url"].rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        payload = {"model": seat["model"], "messages": [{"role": "system", "content": system},
                                                         {"role": "user", "content": user}],
                   "stream": False, "temperature": temperature}
        data, attempts = _post(url, headers, payload, timeout_secs)
        text = data["choices"][0]["message"]["content"]
        usage = data.get("usage") or {}
        tokens_in, tokens_out = usage.get("prompt_tokens"), usage.get("completion_tokens")
    return {"text": text or "", "input_tokens": tokens_in, "output_tokens": tokens_out,
            "cost_usd": cost_usd(seat, tokens_in, tokens_out), "attempts": attempts,
            "provider": spec["name"], "model": seat["model"]}


def chat(base_url: str, model: str, system: str, user: str, timeout_secs: int = DEFAULT_TIMEOUT_SECS) -> str:
    """Un chat completion contra un endpoint OpenAI-compatible sin clave
    (Ollama y compañía). Misma ruta con reintentos que `complete`."""
    return complete({"seat": "?", "provider": "local", "model": model, "base_url": base_url},
                    system, user, timeout_secs)["text"]


USAGE_FIELDS = ("input_tokens", "output_tokens", "cost_usd", "attempts", "provider")


def _record_usage(stats: dict | None, result: dict) -> None:
    if stats is not None:
        stats.update({k: result.get(k) for k in USAGE_FIELDS})


def _persona(seat: str) -> str:
    """Prompt de persona con fallback genérico: un asiento custom del registry
    sin persona dedicada no puede quedar sin identidad."""
    try:
        return personas.system_prompt(seat)
    except ValueError:
        return f"Sos el asiento '{seat}' del sistema MAGI."


def _history(journal: list[dict], vacio: str) -> str:
    """Journal inlineado para el prompt: mismos campos para cada mensaje y
    cuerpo acotado a BODY_CHARS."""
    return "\n\n".join(
        f"[{m['author']} · {m['kind']}]:\n{(m['body'] or '')[:BODY_CHARS]}"
        for m in journal
    ) or vacio


def build_api_prompt(seat: str, decision: dict, journal: list[dict],
                     memory: str | None = None) -> tuple[str, str]:
    """(system, user) para el modelo. La persona va al system con el contrato
    de respuesta; el contexto completo va al user. `memory` es el bloque
    opcional del grafo (decisiones previas, archivos tocados): contexto, no
    verdad."""
    persona = _persona(seat)
    history = _history(journal, "(journal vacío)")
    system = (
        f"{persona}\n\n"
        "Respondé SIEMPRE con esta estructura y nada fuera de ella:\n"
        "POSITION: yes|no|conditional|info\n"
        "CONDITIONS: <condiciones separadas por ;> (sólo si position=conditional)\n"
        "<tu razonamiento completo>"
    )
    memoria_txt = f"{memory}\n\n" if memory else ""
    user = (
        f"Decisión #{decision['id']} (protocolo {decision['protocol']}, ronda {decision['round']}): "
        f"{decision['title']}\n"
        f"Artefacto sobre el que se decide: {decision.get('artifact') or '—'}\n\n"
        f"{memoria_txt}"
        f"Journal del debate hasta ahora:\n{history}\n\n"
        "Votá desde tu eje, no desde el consenso esperado. Si es la ronda 2 o más, "
        "revisá tu posición anterior a la luz de las otras cabezas: cambiala sólo "
        "si sus argumentos son mejores que los tuyos."
    )
    return system, user


def build_inline_active_prompt(seat: str, decision: dict, journal: list[dict],
                               memory: str | None, cwd: str) -> str:
    """Prompt de decisión para una cabeza CLI-sin-MCP que SÍ tiene
    herramientas (codex exec con sandbox, p.ej.). Mismo contrato de voto que
    las demás, pero con capacidad de investigación simétrica a la cabeza MCP:
    las personalidades sesgan el criterio, no las capacidades — si sólo una
    cabeza puede mirar el repo, el consejo entero queda sesgado a lo que esa
    cabeza ve."""
    history = _history(journal, "(journal vacío)")
    memoria_txt = f"\n\n{memory}" if memory else ""
    revision = decision.get('artifact_revision') or 'no disponible'
    investigation = (
        # Sin artefacto, el cwd es el del sistema MAGI y no el tema: una cabeza
        # que "investiga el repo" para una pregunta filosófica termina leyendo
        # personas.py (visto en la decisión #26 sobre la condición humana).
        "No hay repositorio ni documento seleccionado: NO busques archivos en "
        "tu directorio de trabajo (es el del sistema MAGI, no el tema). Razoná "
        "desde el journal y tu conocimiento; citá fuentes comprobables cuando "
        "puedas y marcá lo que no puedas verificar."
        if not decision.get('artifact') else
        "Antes de votar, INVESTIGÁ con tus herramientas: leé los archivos del "
        "repo o documento que importen, corré comandos de SOLO LECTURA (git "
        "status/diff, grep, tests si aplican). No modifiques nada."
        if decision['round'] <= 1 else
        "Esta es una ronda posterior. Primero compará el HEAD actual con la "
        "evidencia y los SHA ya citados en el journal. Si no cambió, reutilizá "
        "los hechos verificados y revisá sólo objeciones o afirmaciones nuevas; "
        "no repitas la auditoría ni la suite completa. Si cambió, investigá el "
        "delta con comandos de SOLO LECTURA. No modifiques nada."
    )
    return (
        f"{_persona(seat)}\n\n"
        f"Decisión #{decision['id']} (protocolo {decision['protocol']}, ronda {decision['round']}): {decision['title']}\n"
        f"Artefacto sobre el que se decide: {decision.get('artifact') or '—'} "
        f"(tu directorio de trabajo es {cwd})\n"
        f"Revisión actual del artefacto: {revision}\n"
        f"{memoria_txt}\n\n"
        f"Journal del debate hasta ahora:\n{history}\n\n"
        f"{investigation}\n"
        "Tu respuesta FINAL termina SIEMPRE con esta estructura y nada fuera "
        "de ella después:\n"
        "POSITION: yes|no|conditional|info\n"
        "CONDITIONS: <condiciones separadas por ;> (sólo si position=conditional)\n"
        "<tu razonamiento completo, citando lo que viste>\n\n"
        "Votá desde tu eje, no desde el consenso esperado. Si es la ronda 2 o "
        "más, revisá tu posición anterior a la luz de las otras cabezas."
    )


def run_turn(seat: dict, decision: dict, journal: list[dict],
             memory: str | None = None, stats: dict | None = None) -> dict:
    """Un turno completo: prompt → chat → voto parseado.

    `seat` es la entrada del registry (type='api', model, base_url,
    timeout_secs opcional). `journal` ya viene acotado por el llamador;
    `memory` es el bloque opcional del grafo.
    """
    system, user = build_api_prompt(seat["seat"], decision, journal, memory=memory)
    result = complete(seat, system, user)
    _record_usage(stats, result)
    vote = parse_vote(result["text"])
    vote["usage"] = {k: result.get(k) for k in USAGE_FIELDS}
    return vote


def build_chat_prompt(seat: str, journal: list[dict], artifact: str | None = None,
                      thread: str | None = None) -> tuple[str, str]:
    """(system, user) para un turno de CHAT LIBRE (thread sin decisión).

    Mismo contrato que una cabeza CLI en un thread libre: un mensaje de
    conversación desde su eje. Sin tag POSITION acá — no hay nada que votar.
    `artifact`/`thread` acotan la memoria del grafo al repo y la conversación
    en curso, igual que en una decisión: sin ellos el chat recibía recuerdos
    de cualquier proyecto.
    """
    persona = _persona(seat)
    import memory_ctx
    question = next((m.get('body') or '' for m in reversed(journal)
                     if m.get('author') == 'adrian'), '')
    memory = memory_ctx.memoria_para(question, artifact, thread=thread) if question else ''
    if artifact:
        brief = memory_ctx.repo_brief(artifact)
        if brief:
            memory = (brief + '\n\n' + memory).strip()
    history = _history(journal, "(conversación vacía)")
    system = (
        f"{persona}\n\n"
        "Estás en una conversación abierta con el operador humano y las otras "
        "cabezas del consejo. Respondé UN SOLO mensaje, conciso, desde tu eje "
        "de decisión: no votes ni uses tags — es charla, no una decisión formal."
    )
    user = f"Conversación hasta ahora:\n{history}\n\nRespondé al último mensaje."
    if memory:
        user = memory + '\n\n' + user
    return system, user


def run_chat_turn(seat: dict, journal: list[dict], artifact: str | None = None,
                  thread: str | None = None, stats: dict | None = None) -> str:
    """Un turno de chat: prompt → chat → texto de la respuesta."""
    system, user = build_chat_prompt(seat["seat"], journal, artifact, thread)
    result = complete(seat, system, user)
    _record_usage(stats, result)
    return result["text"]


def strip_echo(text: str, prompt: str | None = None) -> str:
    """Limpia el stdout de una cabeza CLI para guardar sólo su respuesta.

    Algunos CLIs en modo no interactivo imprimen el prompt completo y
    metadatos de sesión junto a la respuesta (codex exec: banner, 'user',
    el prompt, 'codex', la respuesta y de nuevo la respuesta tras
    'tokens used'). El journal y el voto quieren la respuesta, no el eco:
    sacamos el prompt si aparece verbatim, el preámbulo hasta la marca de
    turno del asistente, y la cola desde 'tokens used'. Texto sin esas
    marcas queda intacto.
    """
    out = text or ""
    if prompt and prompt in out:
        out = out.replace(prompt, "", 1)
    m = re.search(r"^codex\s*$", out, re.MULTILINE)
    if m:
        out = out[m.end():]
    m = re.search(r"^tokens used\b", out, re.MULTILINE)
    if m:
        out = out[:m.start()]
    return out.strip()
