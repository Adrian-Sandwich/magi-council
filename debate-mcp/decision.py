"""Motor de decisiones MAGI: funciones puras, sin IO.

Reciben estado ya leído de Postgres (filas de decisions/positions como dicts)
y devuelven ÓRDENES: a quién disparar, con qué prompt, o cómo cerrar la ronda.
server.py aplica los cambios en la base; relay.py dispara los procesos. Que
sea puro es lo que lo hace testeable sin mocks de red ni de base.

Protocolos (referencia conceptual: fshiori/magi, reescrito a lo ClaMi):
- vote:     una ronda de posiciones; mayoría cierra; split va a arbitraje.
- critique: rondas de crítica mutua hasta acuerdo o MAX_ROUNDS.
- adaptive: vota primero; si hay split 3-vías, pasa a critique solo.

El cierre espera las posiciones de TODOS los asientos participantes de la
ronda: el minority report sólo es completo si las tres cabezas votaron.
"""

import re as _re

POSITIONS = ("yes", "no", "conditional", "info")
PROTOCOLS = ("vote", "critique", "adaptive")
MAX_ROUNDS = 3
# Las preguntas conceptuales no tienen un artefacto que aprobar. Una primera
# ronda de tres `info` va directo a la evaluación de contenido: el editor
# redacta una respuesta común y cada cabeza decide si la acepta; si alguna
# objeta, ESA objeción abre la ronda siguiente (content_resolution). Hasta el
# 2026-09-18 se forzaba una segunda ronda de contraste antes de evaluar: eran
# ~2.5 min y ~12k tokens extra por pregunta, también cuando las tres respuestas
# ya coincidían. INFO_MIN_ROUNDS > 1 restaura ese comportamiento.
INFO_MIN_ROUNDS = 1

# una sola cabeza (modo degradado): su voto manda, pero con confidence mínima —
# no es un veredicto MAGI, es el mejor esfuerzo disponible.
CONFIDENCE_UNANIMOUS = 1.0
CONFIDENCE_MAJORITY = 0.66
CONFIDENCE_DEGRADED = 0.5


def round_positions(positions: list[dict], round: int) -> list[dict]:
    return [p for p in positions if p["round"] == round]


def mind_changes(positions: list[dict]) -> list[dict]:
    """Cambios de parecer entre rondas consecutivas, por cabeza.

    Un recast con la misma posición no cuenta: mantenerse firme también es
    información, pero no es un cambio de parecer. Sólo se comparan rondas
    donde la cabeza votó en ambas — la que todavía no recasteó no entra.
    El dossier los reporta porque un consenso alcanzado DESPUÉS de un cambio
    vale distinto que uno sostenido desde la primera ronda.
    """
    by_head: dict[str, dict[int, dict]] = {}
    for p in positions:
        by_head.setdefault(p["head"], {})[p["round"]] = p
    changes = []
    for head, rounds in by_head.items():
        for r in sorted(rounds):
            prev = rounds.get(r - 1)
            if prev is not None and prev["position"] != rounds[r]["position"]:
                changes.append({
                    "head": head,
                    "from": prev["position"],
                    "to": rounds[r]["position"],
                    "round": r,
                })
    return changes


def missing_seats(decision: dict, positions: list[dict]) -> list[str]:
    """Asientos participantes que todavía no votaron en la ronda actual."""
    cast = {p["head"] for p in round_positions(positions, decision["round"])}
    return [h for h in decision["heads"] if h not in cast]


def errored_seats(decision: dict) -> list[str]:
    """Asientos con turno fallido registrado en la ronda actual (turn_errors
    del dossier). No votaron ni van a votar sin un reintento explícito."""
    errors = (decision.get("minority_report") or {}).get("turn_errors") or {}
    return [h for h in decision["heads"] if (errors.get(h) or {}).get("round") == decision["round"]]


def pending_turns(decision: dict, positions: list[dict]) -> list[dict]:
    """Turnos que el relay debe disparar ahora: los asientos que faltan en la
    ronda actual de una decisión abierta."""
    if decision["status"] != "open":
        return []
    kind = "answer" if decision["round"] == 1 else "recast"
    return [
        {"seat": h, "kind": kind, "round": decision["round"]}
        for h in missing_seats(decision, positions)
    ]


def resolve_votes(decision: dict, positions: list[dict]) -> dict | None:
    """Mayoría sobre las posiciones de la ronda actual.

    Devuelve {"ruling", "confidence", "minority", "degraded"} o None cuando
    no hay mayoría (todas distintas). Con una sola cabeza (degradado) su voto
    manda con confidence mínima; con dos divididas no hay mayoría posible.
    """
    votes = {p["head"]: p for p in round_positions(positions, decision["round"])}
    participating = [h for h in decision["heads"] if h in votes]
    if not participating:
        return None
    if len(participating) == 1:
        p = votes[participating[0]]
        return {
            "ruling": p["position"],
            "confidence": CONFIDENCE_DEGRADED,
            "minority": [],
            "degraded": True,
        }

    counts: dict[str, int] = {}
    for h in participating:
        pos = votes[h]["position"]
        counts[pos] = counts.get(pos, 0) + 1
    best = max(counts.values())
    if best < 2:
        return None  # todas distintas: split
    ruling = next(pos for pos, n in counts.items() if n == best)
    return {
        "ruling": ruling,
        "confidence": CONFIDENCE_UNANIMOUS if best == len(participating) else CONFIDENCE_MAJORITY,
        "minority": [
            {
                "head": h,
                "position": votes[h]["position"],
                "conditions": votes[h].get("conditions"),
            }
            for h in participating
            if votes[h]["position"] != ruling
        ],
        "degraded": len(participating) < len(decision["heads"]),
    }


def advance(decision: dict, positions: list[dict]) -> dict:
    """Qué hacer con la ronda actual. Devuelve una ORDEN:

    - wait:        falta gente, no toca nada.
    - close:       mayoría → ruling/confidence/minority; hay que cerrar la decisión.
    - next_round:  split con protocolo critique/adaptive y rondas disponibles.
    - split:       no hay acuerdo y no queda mecanismo: pasa a arbitraje humano.
    - none:        la decisión ya está cerrada.
    """
    if decision["status"] != "open":
        return {"action": "none"}
    missing = missing_seats(decision, positions)
    if missing:
        # Una cabeza en ERROR no vuelve sola: casper chocó con su límite de
        # sesión a media revisión (#96) y la ronda quedó abierta para siempre
        # con dos votos iguales. Si todo lo que falta está en error y los que
        # votaron son mayoría y coinciden, la ronda cierra degradada con
        # confianza de mayoría: un plan o una revisión así no se integra sin
        # que el operador lo autorice (2/3). Dos votos distintos esperan al
        # reintento; un solo voto también.
        errored = errored_seats(decision)
        voted = [h for h in decision["heads"] if h not in missing]
        if errored and set(missing) <= set(errored) and len(voted) >= 2:
            res = resolve_votes(decision, positions)
            if res is not None and res["ruling"] != "info":
                res["confidence"] = min(res["confidence"], CONFIDENCE_MAJORITY)
                return {"action": "close", **res, "degraded": True, "errored": errored}
        return {"action": "wait"}
    rp = round_positions(positions, decision["round"])
    all_info = bool(rp) and all(p["position"] == "info" for p in rp)
    rounds_used = decision['round'] - (decision.get('minority_report') or {}).get('round_budget_start', 1) + 1
    if all_info and rounds_used < INFO_MIN_ROUNDS:
        return {
            "action": "next_round",
            "minority": [
                {"head": p["head"], "position": p["position"], "conditions": p.get("conditions")}
                for p in rp
            ],
        }
    if all_info:
        return {'action': 'assess_content'}
    res = resolve_votes(decision, positions)
    if res is not None:
        if res['ruling'] == 'info':
            return {'action': 'assess_content'}
        return {"action": "close", **res}

    minority = [
        {"head": p["head"], "position": p["position"], "conditions": p.get("conditions")}
        for p in rp
    ]
    if decision["protocol"] == "vote" or rounds_used >= MAX_ROUNDS:
        return {"action": "split", "minority": minority}
    return {"action": "next_round", "minority": minority}


def content_resolution(round_, expected, reviews, round_start=1):
    """Editorial fidelity alone cannot authorize a shared INFO answer."""
    by_head = {r['seat']: r for r in reviews}
    if len(expected) < 2:
        return 'unavailable'
    if len(expected) >= 2 and all(by_head.get(h, {}).get('approve') is True
           and by_head[h].get('accept_answer') is True and not by_head[h].get('error')
           for h in expected):
        return 'consensus'
    if any(h not in by_head or by_head[h].get('error') or type(by_head[h].get('accept_answer')) is not bool for h in expected):
        return 'unavailable'
    return 'next_round' if round_ - round_start + 1 < MAX_ROUNDS else 'budget_exhausted'


def build_head_prompt(seat: str, persona: str, decision: dict, since_id: int,
                      memory: str | None = None) -> str:
    """Prompt del disparo a una cabeza: su persona + el estado de la decisión +
    las instrucciones concretas de turno. `memory` es el bloque opcional del
    grafo (memory_ctx): contexto de decisiones previas, no verdad."""
    d = decision
    lines = [
        persona,
        "",
        f"Decisión #{d['id']} (protocolo {d['protocol']}, ronda {d['round']}): {d['title']}",
    ]
    if d.get("artifact"):
        lines.append(f"Artefacto sobre el que se decide: {d['artifact']}")
    if memory:
        lines.append("")
        lines.append(memory)
    lines.append("")
    lines.append(
        f"1. Leé el journal del debate: read_thread(thread='{d['thread']}', since_id={since_id})."
    )
    if d["round"] > 1:
        prev = d["round"] - 1
        summary = ", ".join(
            f"{p['head']}={p['position']}" for p in round_positions(d.get("positions") or [], prev)
        )
        lines.append(
            f"2. Las posiciones de la ronda {prev} están en el journal"
            + (f": {summary}." if summary else ".")
        )
        lines.append(
            "   Revisá la tuya a la luz de las otras dos cabezas: cambiala sólo si sus "
            "argumentos son mejores que los tuyos; si te mantenés, reforzá tu posición "
            "contra ellos. Cambiar de parecer es legítimo — queda registrado."
        )
    elif d.get("artifact"):
        lines.append(
            "2. Investigá el artefacto con tus herramientas (Read/Grep/Glob) antes de votar — "
            "con criterio: el journal ya trae el contexto del debate, así que andá a lo que sólo "
            "vos podés ver (correr tests, leer archivos clave). Evitá loops de lectura."
        )
    else:
        lines.append(
            "2. No hay repositorio ni documento seleccionado: no busques archivos en tu "
            "directorio de trabajo (es el del sistema MAGI, no el tema). Razoná desde el "
            "journal y tu conocimiento; citá fuentes comprobables cuando puedas."
        )
    lines.append(
        f"3. Publicá tu análisis y votá: cast_position(decision_id={d['id']}, "
        "position=<yes|no|conditional|info>, conditions=[...] si aplica, "
        "body=<tu razonamiento completo>)."
    )
    lines.append("Posteá UN SOLO mensaje. No uses wait_messages.")
    return "\n".join(lines)


def resultado_text(decision: dict, outcome: dict) -> str:
    """Texto del message 'resultado' que resume el cierre en el journal."""
    d = decision
    if outcome["action"] == "close":
        minor = outcome.get("minority") or []
        if minor:
            detalle = "; ".join(
                f"{m['head']} votó {m['position']}"
                + (f" ({', '.join(m['conditions'])})" if m.get("conditions") else "")
                for m in minor
            )
            minor_txt = f"Minority report: {detalle}."
        else:
            minor_txt = "Sin minoría: votación unánime."
        cambios = outcome.get("mind_changes") or []
        if cambios:
            detalle = "; ".join(
                f"{c['head']} {c['from']}→{c['to']} (ronda {c['round']})" for c in cambios
            )
            minor_txt += f" Cambios de posición: {detalle}."
        degradado = " [MODO DEGRADADO]" if outcome.get("degraded") else ""
        return (
            f"DECISIÓN #{d['id']} CERRADA{degradado} — ruling: {outcome['ruling']} "
            f"(confidence {outcome['confidence']}). {minor_txt}"
        )
    if outcome["action"] == "split":
        detalle = "; ".join(
            f"{m['head']}={m['position']}" for m in outcome.get("minority", [])
        )
        return (
            f"DECISIÓN #{d['id']} SIN ACUERDO tras {d['round']} ronda(s) — posiciones: {detalle}. "
            f"El sistema no inventa consenso: adrian, cerrá con post_message(thread='{d['thread']}', "
            "author='adrian', kind='arbitraje', body=<tu ruling y justificación>)."
        )
    raise ValueError(f"outcome sin texto: {outcome['action']}")


# ------------------------------------------------------------ modo producción
# Ciclo completo: deliberar (protocolos de siempre) → al aprobarse un plan de
# decisión production, ejecutar (un ejecutor implementa en la rama magi/d<N>)
# → revisar (una decisión normal sobre el diff) → mergear si el consejo
# aprueba unánime. Las funciones de acá son puras: la ejecución vive en el
# relay, el estado en board.

RE_REVIEW = _re.compile(r"^Revisar implementación de #(\d+)", _re.IGNORECASE)


def debe_ejecutar(d: dict, act: dict) -> bool:
    """Un cierre de plan dispara ejecución sólo si la decisión es production
    y el ruling es aprobatorio (yes o conditional — las condiciones van en el
    prompt del ejecutor)."""
    return (
        bool(d.get("production"))
        and act.get("action") == "close"
        and act.get("ruling") in ("yes", "conditional")
    )


def rama_ejecucion(decision_id) -> str:
    """La rama donde el ejecutor trabaja: derivable del id, auditable."""
    return f"magi/d{decision_id}"


def revision_de(titulo: str):
    """Si el título es el de una revisión de ejecución, devuelve el id de la
    decisión original que implementa (para saber qué rama mergear)."""
    m = RE_REVIEW.match(titulo or "")
    return int(m.group(1)) if m else None


def resultado_ejecucion_texto(d: dict, act: dict) -> str:
    """El 'resultado' cuando un plan production es aprobado: no es un cierre,
    es el pase a ejecución."""
    minor = act.get("minority") or []
    minor_txt = ""
    if minor:
        detalle = "; ".join(
            f"{m['head']} votó {m['position']}"
            + (f" ({', '.join(m['conditions'])})" if m.get("conditions") else "")
            for m in minor
        )
        minor_txt = f" Minority report: {detalle}."
    return (
        f"PLAN APROBADO (confidence {act['confidence']}).{minor_txt} "
        f"Pasando a ejecución: el ejecutor trabaja en la rama "
        f"{rama_ejecucion(d['id'])} sobre {d.get('artifact') or 'el repositorio'}; "
        f"al terminar se abre la revisión del diff."
    )
