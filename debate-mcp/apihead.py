"""Ejecutor de turnos para asientos type="api" (Ollama y OpenAI-compatibles).

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
o cualquier endpoint OpenAI-compatible, local o remoto.
"""

import json
import re
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


def chat(base_url: str, model: str, system: str, user: str, timeout_secs: int = DEFAULT_TIMEOUT_SECS) -> str:
    """Un chat completion contra un endpoint OpenAI-compatible. Stdlib only."""
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        # votos, no poesía: temperatura baja para respuestas consistentes
        "temperature": 0.2,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_secs) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


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
             memory: str | None = None) -> dict:
    """Un turno completo: prompt → chat → voto parseado.

    `seat` es la entrada del registry (type='api', model, base_url,
    timeout_secs opcional). `journal` ya viene acotado por el llamador;
    `memory` es el bloque opcional del grafo.
    """
    system, user = build_api_prompt(seat["seat"], decision, journal, memory=memory)
    text = chat(
        seat["base_url"], seat["model"], system, user,
        seat.get("timeout_secs", DEFAULT_TIMEOUT_SECS),
    )
    return parse_vote(text)


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
                  thread: str | None = None) -> str:
    """Un turno de chat: prompt → chat → texto de la respuesta."""
    system, user = build_chat_prompt(seat["seat"], journal, artifact, thread)
    return chat(
        seat["base_url"], seat["model"], system, user,
        seat.get("timeout_secs", DEFAULT_TIMEOUT_SECS),
    )


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
