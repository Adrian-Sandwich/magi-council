"""Nodos debate_thread y decision desde la base `debate` de Postgres.

- Threads: un nodo por thread de messages, con conteos y autores en props.
  Los edges de sesiones (posted_to) NO se crean acá: los crean
  ingest_claude/ingest_kimi referenciando debate_thread:<name> por natural
  key (no importa el orden de ingestión, finalize() en export_kgraph.py tira
  los edges que queden colgando).
- Decisiones: un nodo por fila de decisions (dominio `decision`) con
  ruling/confidence/minority/mind_changes en props, y edge journal_of desde
  el debate_thread de su journal. Así el grafo sabe no sólo QUÉ se debatió
  sino qué se resolvió, con qué confianza y quién discrepó.
"""

from datetime import datetime, timezone
import hashlib
import json

import psycopg
from psycopg.rows import dict_row

import db
import settings
from explicit_memory import extract
from experience import build as experience_for

CONNINFO = settings.CONNINFO
SOURCE = "ingest_debate"


def node_id(thread: str) -> str:
    return f"debate_thread:{thread}"


def decision_node_id(decision_id) -> str:
    return f"decision:{decision_id}"


def _iso(ts) -> str | None:
    return ts.isoformat() if ts else None


def changed(conn, identifier, row):
    """Checkpoint and node writes commit together; failed runs remain retryable."""
    digest = hashlib.sha256(('experience-v1:' + json.dumps(row, sort_keys=True, default=str)).encode()).hexdigest()
    previous = conn.execute('SELECT digest FROM debate_checkpoints WHERE id=?', (identifier,)).fetchone()
    exists = conn.execute('SELECT 1 FROM nodes WHERE id=?', (identifier,)).fetchone()
    if exists and previous == (digest,):
        return False
    conn.execute('INSERT OR REPLACE INTO debate_checkpoints VALUES (?,?)', (identifier, digest))
    return True


def stale_ids(conn, marks: dict[str, str]) -> set[str]:
    """Qué nodos hay que reconsultar en Postgres. `marks` es {id de nodo:
    marca barata} calculada con consultas livianas (último message id del
    thread; md5 de la fila de la decisión). Cambió la marca, o el nodo no
    está en el grafo → hay que traer la fila completa. Sin esto, cada
    corrida (una cada 30s desde el relay) re-agregaba todos los mensajes de
    todos los threads, con un timeout duro de 60s esperándola crecer."""
    conn.execute('CREATE TABLE IF NOT EXISTS debate_marks (id TEXT PRIMARY KEY, mark TEXT NOT NULL)')
    previous = dict(conn.execute('SELECT id, mark FROM debate_marks').fetchall())
    present = {row[0] for row in conn.execute(
        "SELECT id FROM nodes WHERE domain IN ('debate_thread', 'decision')")}
    return {identifier for identifier, mark in marks.items()
            if previous.get(identifier) != mark or identifier not in present}


def write_decision(conn, r: dict, now: str) -> None:
    """Una decisión como nodo del grafo + edge journal_of desde su thread.

    Es función propia (y no inline en main) porque es la parte testeable sin
    Postgres: recibe una fila ya leída.
    """
    minority = r["minority_report"] or {}
    props = {
        "title": r["title"],
        "objective": r["title"],
        "artifact": r.get("artifact"),
        "evidence": r.get("evidence") or [],
        "explicit_memory": extract(r.get("human_messages") or []),
        "experience": experience_for(r),
        "approved_conditions": minority.get("approved_conditions") or [],
        "pending": "Awaiting human input" if r["status"] == "split" else
                   "Implementation in progress" if r["status"] == "executing" else
                   "Deliberation in progress" if r["status"] == "open" else None,
        "protocol": r["protocol"],
        "status": r["status"],
        "ruling": r["ruling"],
        "confidence": r["confidence"],
        "round": r["round"],
        "thread": r["thread"],
        "created_by": r["created_by"],
        "minority": minority.get("minority"),
        "mind_changes": minority.get("mind_changes") or [],
        "degraded": minority.get("degraded", False),
        "first_at": _iso(r["created_at"]),
        "last_at": _iso(r.get("last_message_at") or r["closed_at"] or r["created_at"]),
    }
    db.upsert_node(
        conn,
        id=decision_node_id(r["id"]),
        domain="decision",
        source=SOURCE,
        updated_at=now,
        label=f"#{r['id']} {r['title']}",
        tag="Decision",
        size=10,
        tooltip=f"{r['status']} · ruling={r['ruling']} · {r['protocol']} · ronda {r['round']}",
        props=props,
    )
    db.upsert_edge(
        conn,
        node_id(r["thread"]),
        decision_node_id(r["id"]),
        "journal_of",
        source=SOURCE,
        updated_at=now,
        label_forward="journal de",
    )


def main() -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn = db.connect()
    # connect_timeout acotado: sin él, un Postgres caído cuelga refresh.sh
    # ~2 minutos por corrida (mismo criterio que debate-mcp/config.py).
    conn.execute('CREATE TABLE IF NOT EXISTS debate_checkpoints (id TEXT PRIMARY KEY, digest TEXT NOT NULL)')
    with psycopg.connect(CONNINFO, row_factory=dict_row, connect_timeout=10) as pg:
        # Marcas baratas primero: un thread cambia sólo con mensajes nuevos;
        # una decisión, con mensajes en su thread o con su propia fila
        # (síntesis, estado de ejecución, continuación).
        last_ids = {r["thread"]: r["last_id"] for r in pg.execute(
            "SELECT thread, max(id) AS last_id FROM messages GROUP BY thread").fetchall()}
        decision_rows = pg.execute(
            "SELECT d.id, d.thread, md5(row_to_json(d)::text) AS digest FROM decisions d").fetchall()
        thread_marks = {node_id(t): str(last) for t, last in last_ids.items()}
        decision_marks = {decision_node_id(r["id"]): f"{r['digest']}:{last_ids.get(r['thread'])}"
                          for r in decision_rows}
        stale = stale_ids(conn, {**thread_marks, **decision_marks})
        stale_threads = [t for t in last_ids if node_id(t) in stale]
        stale_decisions = [r["id"] for r in decision_rows if decision_node_id(r["id"]) in stale]

        rows = [] if not stale_threads else pg.execute(
            """
            WITH kc AS (
                SELECT thread, kind, count(*) AS kind_count
                FROM messages GROUP BY thread, kind
            )
            SELECT m.thread,
                   COALESCE((SELECT jsonb_agg(to_jsonb(o) ORDER BY o.id) FROM decision_outcomes o JOIN decisions d ON d.id=o.decision_id WHERE d.thread=m.thread), '[]'::jsonb) AS outcome_reports,
                   (SELECT artifact FROM decisions WHERE thread=m.thread ORDER BY id DESC LIMIT 1) AS artifact,
                   count(*) AS n_messages,
                   min(m.created_at) AS first_at,
                   max(m.created_at) AS last_at,
                   array_agg(DISTINCT m.author) AS authors,
                   COALESCE((SELECT jsonb_agg(jsonb_build_object(
                       'id',h.id,'author',h.author,'body',h.body,'created_at',h.created_at) ORDER BY h.id)
                       FROM messages h WHERE h.thread=m.thread AND h.author='adrian'), '[]'::jsonb) AS human_messages,
                   (SELECT json_object_agg(kind, kind_count) FROM kc WHERE kc.thread = m.thread) AS kind_counts
            FROM messages m
            WHERE m.thread = ANY(%s)
            GROUP BY m.thread
            """,
            (stale_threads,),
        ).fetchall()
        decisions = [] if not stale_decisions else pg.execute(
            """
            SELECT d.*,
                   COALESCE((SELECT jsonb_agg(to_jsonb(o) ORDER BY o.id) FROM decision_outcomes o WHERE o.decision_id=d.id), '[]'::jsonb) AS outcome_reports,
                   COALESCE((SELECT jsonb_agg(jsonb_build_object('id',id,'author',author,'body',left(body,2000),'created_at',created_at) ORDER BY id)
                       FROM messages WHERE thread=d.thread AND author='magi' AND kind IN ('resultado','consulta')), '[]'::jsonb) AS system_messages,
                   COALESCE((SELECT jsonb_agg(jsonb_build_object(
                       'id',id,'author',author,'body',body,'created_at',created_at) ORDER BY id)
                       FROM messages WHERE thread=d.thread AND author='adrian'), '[]'::jsonb) AS human_messages,
                   (SELECT max(created_at) FROM messages WHERE thread=d.thread) AS last_message_at,
                   COALESCE((SELECT jsonb_agg(to_jsonb(e) ORDER BY e.id) FROM (
                       SELECT DISTINCT ON (author) id,author,kind,left(body,1800) AS body,created_at
                       FROM messages WHERE thread=d.thread
                         AND kind IN ('analisis','contexto','arbitraje','posicion','consulta','resultado')
                       ORDER BY author,id DESC
                   ) e), '[]'::jsonb) AS evidence
            FROM decisions d
            WHERE d.id = ANY(%s)
            ORDER BY d.id
            """,
            (stale_decisions,),
        ).fetchall()

    # Lo visto sale de las consultas livianas: el barrido de nodos borrados
    # tiene que ver el universo completo aunque sólo se reconsulten los
    # cambiados.
    seen: set[str] = set(thread_marks)
    seen_decisions: set[str] = set(decision_marks)
    n = 0
    for r in rows:
        n += 1
        if not changed(conn, node_id(r['thread']), r):
            continue
        db.upsert_node(
            conn,
            id=node_id(r["thread"]),
            domain="debate_thread",
            source=SOURCE,
            updated_at=now,
            label=r["thread"],
            tag="DebateThread",
            size=min(30, max(8, 6 + r["n_messages"])),
            tooltip=f"{r['n_messages']} mensajes, {r['first_at'].isoformat()} - {r['last_at'].isoformat()}",
            props={
                "thread": r['thread'],
                "experience": experience_for(r),
                "artifact": r['artifact'],
                "explicit_memory": extract(r['human_messages']),
                "evidence": [dict(m, kind='human_context', body=m['body'][:1800]) for m in r['human_messages'][-3:]],
                "n_messages": r["n_messages"],
                "authors": r["authors"],
                "kind_counts": r["kind_counts"],
                "first_at": r["first_at"].isoformat(),
                "last_at": r["last_at"].isoformat(),
            },
        )

    for r in decisions:
        identifier = decision_node_id(r['id'])
        if changed(conn, identifier, r):
            write_decision(conn, r, now)

    n_swept = db.sweep_domain(conn, "debate_thread", seen)
    n_swept_decisions = db.sweep_domain(conn, "decision", seen_decisions)
    conn.execute('DELETE FROM debate_checkpoints WHERE id NOT IN (SELECT id FROM nodes)')
    # Las marcas se confirman junto con los nodos: una corrida cortada a la
    # mitad no deja marcas de trabajo que no se escribió.
    conn.executemany('INSERT OR REPLACE INTO debate_marks VALUES (?, ?)',
                     [(i, m) for i, m in {**thread_marks, **decision_marks}.items() if i in stale])
    conn.execute('DELETE FROM debate_marks WHERE id NOT IN (SELECT id FROM nodes)')
    db.record_run(conn, "ingest_debate")
    conn.commit()
    conn.close()
    print(
        f"[ingest_debate] {len(thread_marks)} threads ({n} reconsultados, {n_swept} borrados), "
        f"{len(decision_marks)} decisiones ({len(decisions)} reconsultadas, {n_swept_decisions} borradas)"
    )


if __name__ == "__main__":
    main()
