"""Escrituras sobre el tablero que no son tools MCP.

record_position concentra la lógica del voto de cast_position (row lock,
dedup por ronda, insert del message + position, advance del motor, cierre con
resultado) y start_decision la apertura de una decisión. Las usan tanto los
tools MCP (server.py) como la UI (magi_ui.py) y los turnos de asientos API:
una decisión es una decisión venga de donde venga.
"""

import re
import unicodedata
import uuid
from difflib import SequenceMatcher

from psycopg.types.json import Json

import decision
import heads


def start_decision(
    conn,
    title: str,
    artifact: str | None = None,
    protocol: str = "vote",
    created_by: str = "adrian",
    seats: list[str] | None = None,
    production: bool = False,
) -> dict:
    """Abre una decisión MAGI. El llamador maneja la transacción.

    production=true la marca como decisión de plan con ejecución: si el
    consejo la aprueba (ruling yes/conditional), pasa a 'executing' y el
    relay lanza al ejecutor (modo producción) en vez de cerrarla.
    """
    if created_by != "adrian":
        raise ValueError("solo adrian abre decisiones")
    if protocol not in decision.PROTOCOLS:
        raise ValueError(f"protocol inválido: {protocol!r} (válidos: {list(decision.PROTOCOLS)})")
    registry = heads.load()
    known = heads.seat_names(registry)
    participating = list(seats) if seats is not None else heads.seat_names(heads.active_seats(registry))
    unknown = [s for s in participating if s not in known]
    if unknown:
        raise ValueError(f"asientos desconocidos: {unknown} (válidos: {known})")
    if not participating:
        raise ValueError("no hay asientos para decidir: configurá heads.json o DEBATE_HEADS")
    live = set(heads.seat_names(heads.active_seats(registry)))
    degraded = [s for s in participating if s not in live]

    # thread provisional único: la columna es UNIQUE y el nombre final
    # (d<id>) recién se conoce tras el INSERT.
    provisional = f"_opening-{uuid.uuid4().hex[:12]}"
    row = conn.execute(
        """
        INSERT INTO decisions (title, artifact, protocol, thread, heads, created_by, production)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (title, artifact, protocol, provisional, Json(participating), created_by, production),
    ).fetchone()
    did = row["id"]
    thread = f"d{did}"
    msg = conn.execute(
        """
        INSERT INTO messages (thread, author, kind, body, artifact)
        VALUES (%s, %s, 'analisis', %s, %s)
        RETURNING id
        """,
        (thread, created_by, title, artifact),
    ).fetchone()
    conn.execute(
        "UPDATE decisions SET thread = %s, anchor_id = %s WHERE id = %s",
        (thread, msg["id"], did),
    )
    return {"decision_id": did, "thread": thread, "seats": participating, "degraded": degraded}


def record_position(
    conn,
    decision_id: int,
    author: str,
    position: str,
    body: str,
    conditions: list[str] | None = None,
    expected_round: int | None = None,
) -> tuple[dict, int]:
    """Registra el voto de una cabeza y aplica la orden del motor.

    Devuelve (orden, message_id). El llamador maneja la transacción y las
    validaciones de author/position (necesitan el registry); acá se vuelve a
    chequear estado y dedup porque entre la validación y el INSERT puede
    haber pasado otra cabeza.
    """
    d = conn.execute(
        "SELECT * FROM decisions WHERE id = %s FOR UPDATE", (decision_id,)
    ).fetchone()
    if d is None:
        raise ValueError(f"decisión {decision_id} no existe")
    if d["status"] != "open":
        raise ValueError(f"decisión {decision_id} está '{d['status']}'")
    if expected_round is not None and d['round'] != expected_round:
        raise ValueError('La ronda cambió mientras la cabeza estaba trabajando')
    ya = conn.execute(
        """
        SELECT 1 FROM positions
        WHERE decision_id = %s AND head = %s AND round = %s
        """,
        (decision_id, author, d["round"]),
    ).fetchone()
    if ya:
        raise ValueError(f"{author} ya votó en la ronda {d['round']}")

    msg = conn.execute(
        """
        INSERT INTO messages (thread, author, kind, body, artifact)
        VALUES (%s, %s, 'posicion', %s, NULL)
        RETURNING id
        """,
        (d["thread"], author, body),
    ).fetchone()
    conn.execute(
        """
        INSERT INTO positions (decision_id, head, round, position, conditions, message_id)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (decision_id, author, d["round"], position,
         Json(conditions) if conditions else None, msg["id"]),
    )
    positions = conn.execute(
        """
        SELECT head, round, position, conditions
        FROM positions WHERE decision_id = %s
        """,
        (decision_id,),
    ).fetchall()
    if author in ((d.get('minority_report') or {}).get('turn_errors') or {}):
        conn.execute("UPDATE decisions SET minority_report=minority_report #- %s WHERE id=%s",
                     (['turn_errors', author], decision_id))
    act = decision.advance(d, positions)
    if act["action"] == "close":
        act["mind_changes"] = decision.mind_changes(positions)
        approved_conditions = list(dict.fromkeys(
            c for p in positions
            if p["round"] == d["round"] and p["head"] in d["heads"]
            and p["position"] == "conditional"
            for c in (p.get("conditions") or [])
        ))
        if decision.debe_ejecutar(d, act):
            # modo producción: no es un cierre, es el pase a ejecución. El
            # relay detecta 'executing', lanza al ejecutor en la rama
            # magi/d<id> y al terminar abre la revisión del diff.
            conn.execute(
                """
                UPDATE decisions
                SET status = 'executing', ruling = %s, confidence = %s,
                    minority_report = %s
                WHERE id = %s
                """,
                (act["ruling"], act["confidence"],
                 Json({
                     "minority": act["minority"],
                     "approved_conditions": approved_conditions,
                     "degraded": act.get("degraded", False),
                     "mind_changes": act["mind_changes"],
                 }),
                 decision_id),
            )
        else:
            conn.execute(
                """
                UPDATE decisions
                SET status = 'closed', ruling = %s, confidence = %s,
                    minority_report = %s, closed_at = now()
                WHERE id = %s
                """,
                (act["ruling"], act["confidence"],
                 Json({
                     "minority": act["minority"],
                     "approved_conditions": approved_conditions,
                     "degraded": act.get("degraded", False),
                     "mind_changes": act["mind_changes"],
                 }),
                 decision_id),
            )
    elif act["action"] == "assess_content":
        conn.execute("""UPDATE decisions SET minority_report = COALESCE(minority_report,'{}'::jsonb) || %s::jsonb
            WHERE id=%s""", (Json({'content_check': {'state': 'pending', 'round': d['round']}}),decision_id))
    elif act["action"] == "next_round":
        conn.execute(
            "UPDATE decisions SET round = %s WHERE id = %s",
            (d["round"] + 1, decision_id),
        )
    elif act["action"] == "split":
        conn.execute(
            """
            UPDATE decisions SET status = 'split', minority_report = %s
            WHERE id = %s
            """,
            (Json({"minority": act["minority"]}), decision_id),
        )
    if act["action"] in ("close", "split"):
        if decision.debe_ejecutar(d, act):
            body = decision.resultado_ejecucion_texto(d, act)
        else:
            body = decision.resultado_text(d, act)
        conn.execute(
            """
            INSERT INTO messages (thread, author, kind, body, artifact)
            VALUES (%s, 'magi', 'resultado', %s, NULL)
            """,
            (d["thread"], body),
        )
    if act["action"] == "split":
        # Destrabe: el split no es un callejón sin salida. El consejo le
        # pide al operador una decisión concreta, con las dos vías: ruling
        # humano (arbitraje) o "seguí" para otra ronda con su contexto
        # (human_message reabre la decisión). Sin esto, el stalemate era
        # invisible hasta que el operador se diera cuenta solo.
        conn.execute(
            """
            INSERT INTO messages (thread, author, kind, body, artifact)
            VALUES (%s, 'magi', 'consulta', %s, NULL)
            """,
            (d["thread"], consulta_destrabe_texto(d["round"], act["minority"])),
        )
    return act, msg["id"]


# Palabras que reabren una decisión en STALEMATE en vez de arbitrarla.
# El resto del texto va como contexto para la ronda nueva.
_RE_SEGUI = re.compile(r"^\s*(segu[ií]|continu[aá]|seguimos|retry|reintent[aá]|otra ronda)\b", re.IGNORECASE)

# Una conversación aprobada puede evolucionar al ejecutor aislado sin que el
# operador haya tenido que elegir un "modo" al abrirla. Se consulta sólo para
# decisiones ya cerradas, aprobadas y con repositorio.
_RE_EJECUTAR = re.compile(
    r"^\s*(?:pues\s+|bueno\s+|entonces\s+|ok[,;:]?\s+)?"
    r"(?:arr[eé]gl(?:ar|alo|ala|enlo)|implement(?:ar|a|alo|enlo)|hazlo|h[aá]ganlo|"
    r"ejecut(?:ar|a|alo|enlo)|aplic(?:ar|a|alo|enlo)|procede|"
    r"apruebo\s+(?:tu\s+plan|el\s+plan|ese\s+plan|la\s+propuesta)|"
    r"vamos\s+con\s+(?:eso|los\s+cambios|tu\s+plan|el\s+plan|ese\s+plan|tu\s+propuesta|la\s+propuesta)|"
    r"sigamos\s+con\s+(?:eso|los\s+cambios|tu\s+plan|el\s+plan)|"
    r"adelante\s+con\s+(?:el\s+plan|tu\s+plan|eso)|haz\s+lo\s+que\s+propones)\b",
    re.IGNORECASE,
)


def is_execution_request(body: str) -> bool:
    if _RE_EJECUTAR.match(body or ""):
        return True
    # El operador escribe desde móvil y con frecuencia manda typos como
    # "vmaos con los camios". Para una decisión ya aprobada, toleramos una
    # errata por palabra, pero exigimos la pareja intención + objeto.
    normalized = unicodedata.normalize('NFKD', body or '').encode('ascii', 'ignore').decode().lower()
    words = re.findall(r'[a-z]+', normalized)
    def resembles(word, choices):
        return any(SequenceMatcher(None, word, choice).ratio() >= .78 for choice in choices)
    intent = any(resembles(w, ('vamos', 'sigamos', 'implementa', 'implementar',
                               'arreglalo', 'ejecuta', 'aplica', 'apruebo')) for w in words)
    target = any(resembles(w, ('cambios', 'plan', 'propuesta', 'eso')) for w in words)
    return intent and target


def consulta_destrabe_texto(round_: int, posiciones: list[dict]) -> str:
    """El mensaje que le pide al operador una decisión concreta cuando el
    consejo no se puso de acuerdo: qué dijo cada cabeza y las dos vías."""
    pos = "; ".join(
        f"{m['head']}={m['position']}"
        + (f" ({', '.join(m['conditions'])})" if m.get("conditions") else "")
        for m in posiciones
    )
    return (
        f"CONSULTA AL OPERADOR — el consejo no se puso de acuerdo tras "
        f"{round_} ronda(s). Posiciones: {pos}.\n"
        f"Respondé con tu ruling y justificación para cerrar la decisión, "
        f"o escribí 'seguí' (opcionalmente con contexto nuevo) para abrir "
        f"otra ronda: las cabezas recastan teniéndolo en cuenta."
    )


def human_message(conn, thread: str, body: str, action: str | None = None) -> dict:
    """Mensaje del operador humano desde la UI: un solo campo de texto, y el
    kind lo decide el estado del thread — cero protocolo que memorizar.

    - decisión 'split'   → dos vías: si el texto empieza con "seguí"/"retry",
      REABRE la decisión (ronda siguiente, tu texto va como contexto y las
      cabezas recastan); si no, es 'arbitraje': cierra con tu ruling;
    - decisión 'open'    → 'contexto': las cabezas lo leen en el journal de
      su próximo turno;
    - thread libre       → 'analisis': abre ronda y el relay dispara a la
      primera cabeza (los mensajes de adrian con otros kinds no disparan).
    """
    d = conn.execute(
        "SELECT id, status, round, minority_report FROM decisions WHERE thread = %s FOR UPDATE", (thread,)
    ).fetchone()
    if action is not None and (action not in ("resume", "arbitrate") or d is None or d["status"] != "split"):
        raise ValueError("This response action requires a decision awaiting your input")
    resume = action == "resume" if action is not None else bool(_RE_SEGUI.match(body))
    if d is not None and d["status"] == "split" and not resume:
        kind = "arbitraje"
    elif d is not None and d["status"] == "split":
        kind = "contexto"
    elif d is not None and d["status"] == "executing":
        # Sólo un reintento explícito habilita otra ejecución. Se conserva
        # el journal completo del fallo para auditoría.
        kind = "contexto"
        if _RE_SEGUI.match(body):
            current_state = (d.get("minority_report") or {}).get("execution_state")
            retry_state = "reviewing" if current_state == "merge_blocked" else "pending"
            conn.execute(
                """UPDATE decisions SET minority_report =
                   COALESCE(minority_report, '{}'::jsonb) ||
                   jsonb_build_object('execution_state', %s::text)
                   WHERE id = %s""", (retry_state, d["id"]),
            )
    elif d is not None:
        kind = "contexto"
    else:
        kind = "analisis"
    row = conn.execute(
        """
        INSERT INTO messages (thread, author, kind, body, artifact)
        VALUES (%s, 'adrian', %s, %s, NULL)
        RETURNING id
        """,
        (thread, kind, body),
    ).fetchone()
    arbitrated = None
    reopened = None
    if kind == "arbitraje":
        closed = conn.execute(
            """
            UPDATE decisions SET status = 'closed', closed_at = now()
            WHERE thread = %s AND status = 'split'
            RETURNING id
            """,
            (thread,),
        ).fetchone()
        arbitrated = closed["id"] if closed else None
    elif kind == "contexto" and d is not None and d["status"] == "split":
        # destrabe: nueva ronda, las cabezas recastan con el contexto nuevo
        conn.execute(
            """UPDATE decisions SET status = 'open', round = %s,
                minority_report=COALESCE(minority_report,'{}'::jsonb) || jsonb_build_object('round_budget_start',round+1)
                WHERE id = %s""",
            (d["round"] + 1, d["id"]),
        )
        reopened = d["id"]
    return {
        "id": row["id"], "kind": kind,
        "arbitrated_decision": arbitrated, "reopened_decision": reopened,
    }


def follow_up_decision(conn, decision_id: int, body: str) -> dict:
    """Continue a closed decision on its existing thread and dossier."""
    d = conn.execute(
        "SELECT id, thread, status, round FROM decisions WHERE id = %s FOR UPDATE",
        (decision_id,),
    ).fetchone()
    if d is None:
        raise ValueError(f"decisión {decision_id} no existe")
    if d["status"] != "closed":
        raise ValueError(f"decisión {decision_id} todavía está '{d['status']}'")
    row = conn.execute(
        """
        INSERT INTO messages (thread, author, kind, body, artifact)
        VALUES (%s, 'adrian', 'contexto', %s, NULL)
        RETURNING id
        """,
        (d["thread"], body),
    ).fetchone()
    conn.execute(
        """
        UPDATE decisions
        SET status = 'open', round = round + 1, ruling = NULL,
            confidence = NULL, closed_at = NULL,
            minority_report = (COALESCE(minority_report, '{}'::jsonb)
                               - 'approved_conditions' - 'synthesis' - 'content_check') ||
                               jsonb_build_object('follow_up', true, 'round_budget_start', round+1)
        WHERE id = %s
        """,
        (decision_id,),
    )
    return {"id": row["id"], "decision_id": decision_id,
            "thread": d["thread"], "action": "follow_up"}


def execute_approved_decision(conn, decision_id: int, body: str) -> dict:
    """Pasa un análisis aprobado al pipeline de ejecución y revisión."""
    d = conn.execute(
        "SELECT * FROM decisions WHERE id = %s FOR UPDATE", (decision_id,)
    ).fetchone()
    if d is None:
        raise ValueError(f"decisión {decision_id} no existe")
    if d["status"] != "closed":
        raise ValueError(f"decisión {decision_id} todavía está '{d['status']}'")
    if d.get("ruling") not in ("yes", "conditional"):
        raise ValueError("sólo una decisión aprobada puede pasar a ejecución")
    # Una revisión aprobada no es un segundo plan independiente. Si pertenece
    # a una ejecución que sigue abierta, sus condiciones vuelven al dossier
    # original para que el mismo worktree se corrija y se revise de nuevo.
    parent = conn.execute(
        """
        SELECT * FROM decisions
        WHERE status = 'executing'
          AND minority_report->'execution'->>'review_id' = %s
        ORDER BY id DESC LIMIT 1 FOR UPDATE
        """,
        (str(decision_id),),
    ).fetchone()
    if parent is not None:
        review_report = d.get("minority_report") or {}
        conditions = list(review_report.get("approved_conditions") or [])
        synthesis = (review_report.get("synthesis") or {}).get("answer")
        plan = synthesis or (
            f"Aplicar las condiciones aprobadas por la revisión #{decision_id} "
            "que pertenezcan al alcance del plan original. Los hallazgos "
            "explícitamente diferidos o ajenos al plan se documentan, pero no "
            "se implementan en esta ejecución."
            + ("\n- " + "\n- ".join(conditions) if conditions else "")
        )
        row = conn.execute(
            """
            INSERT INTO messages (thread, author, kind, body, artifact)
            VALUES (%s, 'adrian', 'contexto', %s, NULL)
            RETURNING id
            """,
            (d["thread"], body),
        ).fetchone()
        patch = {
            "execution_state": "pending",
            "execution_requested": True,
            "execution_activity": None,
            "approved_plan": plan,
            "approved_conditions": conditions,
            "correction_review_id": decision_id,
        }
        conn.execute(
            """
            UPDATE decisions
            SET minority_report = (COALESCE(minority_report, '{}'::jsonb)
                                   - 'execution_error') || %s::jsonb
            WHERE id = %s
            """,
            (Json(patch), parent["id"]),
        )
        notice = (
            f"CORRECCIONES SOLICITADAS — las condiciones de la revisión "
            f"#{decision_id} vuelven a la ejecución #{parent['id']} en su "
            "mismo worktree; al terminar se abrirá una revisión nueva."
        )
        for thread in dict.fromkeys((d["thread"], parent["thread"])):
            conn.execute(
                """
                INSERT INTO messages (thread, author, kind, body, artifact)
                VALUES (%s, 'magi', 'resultado', %s, NULL)
                """,
                (thread, notice),
            )
        return {"id": row["id"], "decision_id": parent["id"],
                "review_id": decision_id, "thread": parent["thread"],
                "action": "corrections_requested"}
    row = conn.execute(
        """
        INSERT INTO messages (thread, author, kind, body, artifact)
        VALUES (%s, 'adrian', 'contexto', %s, NULL)
        RETURNING id
        """,
        (d["thread"], body),
    ).fetchone()
    approved_plan = ((d.get("minority_report") or {}).get("synthesis") or {}).get("answer")
    execution_state = {"execution_state": "pending", "execution_requested": True}
    if approved_plan:
        execution_state["approved_plan"] = approved_plan
    conn.execute(
        """
        UPDATE decisions
        SET status = 'executing', production = true, closed_at = NULL,
            minority_report = (COALESCE(minority_report, '{}'::jsonb) - 'synthesis') ||
                              %s::jsonb
        WHERE id = %s
        """,
        (Json(execution_state), decision_id),
    )
    conn.execute(
        """
        INSERT INTO messages (thread, author, kind, body, artifact)
        VALUES (%s, 'magi', 'resultado', %s, NULL)
        """,
        (d["thread"],
         f"EJECUCIÓN SOLICITADA — el plan aprobado pasa a la rama "
         f"{decision.rama_ejecucion(decision_id)}; después se revisará el diff."),
    )
    return {"id": row["id"], "decision_id": decision_id,
            "thread": d["thread"], "action": "execution_requested"}


def abort_decision(conn, decision_id: int) -> dict | None:
    """Aborta una decisión abierta, en STALEMATE o en ejecución: cierra con
    el flag 'aborted' en el dossier (no es un ruling, es un corte del
    operador) y deja el mensaje en el journal. El relay sierra los procesos
    de sus cabezas/ejecutor en su próximo ciclo (ven el status cerrado).
    Devuelve la fila afectada o None si ya estaba cerrada."""
    row = conn.execute(
        """
        UPDATE decisions
        SET status = 'closed', closed_at = now(),
            minority_report = COALESCE(minority_report, '{}'::jsonb) || '{"aborted": true}'::jsonb
        WHERE id = %s AND status IN ('open', 'split', 'executing')
        RETURNING id, thread, title
        """,
        (decision_id,),
    ).fetchone()
    if row is None:
        return None
    conn.execute(
        """
        INSERT INTO messages (thread, author, kind, body, artifact)
        VALUES (%s, 'adrian', 'arbitraje', %s, NULL)
        """,
        (row["thread"], "ABORTADA por el operador — la deliberación se corta acá."),
    )
    return dict(row)
