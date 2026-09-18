#!/usr/bin/env python3
"""Evalúa la recuperación de memoria con conversaciones REALES del tablero.

La evaluación del paso 3 del roadmap usaba seis frases inventadas y el umbral
de coseno se ajustó sobre ellas mismas. Acá el conjunto sale de Postgres: cada
mensaje de seguimiento humano (`contexto`/`arbitraje` de adrian) es una
consulta, y la respuesta correcta es la decisión a la que pertenece.

Para que no sea trampa se hace *leave-one-out*: antes de consultar, el mensaje
se borra de los nodos del grafo (evidencia y memoria explícita del dossier y
de su thread) en una COPIA de memory.db y se reindexa lo tocado. Se consulta
sin `thread` (una decisión nueva no lo tiene) y, opcionalmente, con el mismo
repositorio. Se mide Recall@1 y Recall@3 léxico vs híbrido.

No es un conjunto calificado a mano: mide si el sistema reencuentra el dossier
del que salió un seguimiento, no si ese dossier era la mejor memoria posible.

Uso (con Postgres y el modelo instalado):
    ../debate-mcp/.venv/Scripts/python.exe eval_retrieval.py [--json] [--same-repo]
"""

import argparse
from contextlib import closing
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "debate-mcp"))

import settings  # noqa: E402

MIN_QUERY_CHARS = 25
# Reportes de resultado y mensajes de control no son preguntas sobre el tema.
SKIP_PREFIXES = ("resultado reportado por el usuario", "seguí", "segui", "retry", "reintent")


def build_cases(rows: list[dict]) -> list[dict]:
    """Filas (decision_id, thread, artifact, message_id, body) → casos con
    consulta, esperado y qué mensaje ocultar. Se descartan mensajes cortos y
    los de control, que no dicen nada del tema."""
    cases = []
    for r in rows:
        body = (r.get("body") or "").strip()
        if len(body) < MIN_QUERY_CHARS or body.lower().startswith(SKIP_PREFIXES):
            continue
        cases.append({
            "decision_id": r["decision_id"], "message_id": r["message_id"],
            "expected": {f"decision:{r['decision_id']}", f"debate_thread:{r['thread']}"},
            "artifact": r.get("artifact"), "query": body,
        })
    return cases


def strip_message(props: dict, message_id: int) -> dict:
    """El nodo sin rastro del mensaje: evidencia y memoria explícita que
    salieron de él. Lo demás (título, objetivo, otras fuentes) queda."""
    out = dict(props)
    out["evidence"] = [e for e in props.get("evidence") or [] if e.get("id") != message_id]
    explicit = props.get("explicit_memory") or {}
    out["explicit_memory"] = {
        key: [item for item in explicit.get(key) or [] if item.get("message_id") != message_id]
        for key in ("current", "history")
    }
    return out


def hide_message(conn: sqlite3.Connection, node_ids: set[str], message_id: int) -> list[str]:
    """Aplica strip_message a los nodos dados en la base (copia). Devuelve los
    ids realmente modificados, para reindexarlos."""
    touched = []
    for node_id in sorted(node_ids):
        row = conn.execute("SELECT props FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if not row:
            continue
        props = json.loads(row[0])
        stripped = strip_message(props, message_id)
        if stripped != props:
            conn.execute("UPDATE nodes SET props = ? WHERE id = ?", (json.dumps(stripped), node_id))
            touched.append(node_id)
    conn.commit()
    return touched


def rank(hits: list[dict], expected: set[str]) -> int | None:
    for position, hit in enumerate(hits, start=1):
        if hit["id"] in expected:
            return position
    return None


def evaluate(cases: list[dict], source_db: Path, retrieve, reindex, same_repo: bool = False) -> dict:
    """Corre cada caso contra una copia limpia de la base. `retrieve(query,
    artifact, semantic: bool)` y `reindex(path)` se inyectan para poder probar
    esto sin modelo ni Postgres."""
    results = []
    with tempfile.TemporaryDirectory(prefix="magi-eval-") as tmp:
        for case in cases:
            path = Path(tmp) / f"memory-{case['message_id']}.db"
            shutil.copy(source_db, path)
            # closing(): en Windows una conexión abierta impide borrar la copia
            with closing(sqlite3.connect(path)) as conn:
                hide_message(conn, case["expected"], case["message_id"])
            reindex(path)
            artifact = case["artifact"] if same_repo else None
            lexical = rank(retrieve(path, case["query"], artifact, False), case["expected"])
            hybrid = rank(retrieve(path, case["query"], artifact, True), case["expected"])
            results.append({**case, "expected": sorted(case["expected"]),
                            "lexical_rank": lexical, "hybrid_rank": hybrid})
    n = len(results)

    def recall(key, k):
        return round(sum(1 for r in results if r[key] is not None and r[key] <= k) / n, 3) if n else None

    return {
        "cases": n,
        "lexical": {"recall@1": recall("lexical_rank", 1), "recall@3": recall("lexical_rank", 3)},
        "hybrid": {"recall@1": recall("hybrid_rank", 1), "recall@3": recall("hybrid_rank", 3)},
        "same_repo": same_repo,
        "results": results,
    }


def summarize_labels(rows: list[dict]) -> dict:
    """Etiquetas humanas (memory_feedback): cuántas hay, qué fracción marcó
    la memoria como útil, y qué fuentes aparecen más en calificaciones
    negativas. Son juicios del operador sobre el bloque completo que vio,
    no una verdad por fuente: una fuente en un bloque "no sirvió" puede
    haber sido la única útil."""
    total = len(rows)
    useful = sum(1 for r in rows if r.get("useful"))
    negative: dict[str, int] = {}
    for r in rows:
        if r.get("useful"):
            continue
        for source in r.get("sources") or []:
            negative[source] = negative.get(source, 0) + 1
    return {
        "labels": total,
        "useful": useful,
        "useful_rate": round(useful / total, 3) if total else None,
        "decisions": len({r.get("decision_id") for r in rows}),
        "most_rejected": sorted(negative.items(), key=lambda kv: (-kv[1], kv[0]))[:5],
    }


def _load_labels_from_postgres() -> list[dict]:
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(settings.CONNINFO, row_factory=dict_row, connect_timeout=10) as pg:
        return pg.execute(
            "SELECT decision_id, useful, note, sources FROM memory_feedback ORDER BY id"
        ).fetchall()


def _load_cases_from_postgres() -> list[dict]:
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(settings.CONNINFO, row_factory=dict_row, connect_timeout=10) as pg:
        return pg.execute(
            """
            SELECT d.id AS decision_id, d.thread, d.artifact, m.id AS message_id, m.body
            FROM decisions d JOIN messages m ON m.thread = d.thread
            WHERE m.author = 'adrian' AND m.kind IN ('contexto', 'arbitraje')
            ORDER BY d.id, m.id
            """
        ).fetchall()


def _real_retrieve(path: Path, query: str, artifact, semantic: bool) -> list[dict]:
    import memory_ctx
    memory_ctx.DB_PATH = path
    os.environ["MEMORY_SEMANTIC"] = "1" if semantic else "0"
    return memory_ctx.retrieve(query, artifact)


def _real_reindex(path: Path) -> None:
    import semantic_memory
    semantic_memory.index(path)


def render(report: dict) -> str:
    lines = [f"[eval_retrieval] {report['cases']} consultas reales"
             + (" (mismo repositorio)" if report["same_repo"] else " (sin ámbito)")]
    for mode in ("lexical", "hybrid"):
        r = report[mode]
        lines.append(f"  {mode:<8} recall@1={r['recall@1']}  recall@3={r['recall@3']}")
    misses = [r for r in report["results"] if r["hybrid_rank"] is None or r["hybrid_rank"] > 3]
    if misses:
        lines.append("  fuera del top-3 híbrido:")
        for r in misses:
            lines.append(f"    #{r['decision_id']} msg {r['message_id']}: {r['query'][:70]!r} "
                         f"(léxico {r['lexical_rank']}, híbrido {r['hybrid_rank']})")
    labels = report.get("labels")
    if labels and labels["labels"]:
        lines.append(f"  etiquetas humanas: {labels['labels']} calificaciones en {labels['decisions']} decisiones; "
                     f"útil en {labels['useful_rate'] * 100:.0f}%")
        for source, n in labels["most_rejected"]:
            lines.append(f"    {n:>3}× en bloques marcados 'no sirvió': {source}")
    else:
        # sin emoji: la consola de Windows (cp1252) no los imprime
        lines.append("  etiquetas humanas: ninguna todavía (Sirvió / No sirvió en la tarjeta de síntesis)")
    lines.append("  (mide si se reencuentra el dossier de origen; no si era la mejor memoria)")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--same-repo", action="store_true",
                        help="consultar con el artifact de la decisión de origen")
    args = parser.parse_args(argv)
    cases = build_cases(_load_cases_from_postgres())
    if not cases:
        print("[eval_retrieval] sin seguimientos humanos en el tablero", file=sys.stderr)
        return 1
    report = evaluate(cases, settings.DB_PATH, _real_retrieve, _real_reindex, args.same_repo)
    try:
        report["labels"] = summarize_labels(_load_labels_from_postgres())
    except Exception as exc:  # base sin la migración 006: la evaluación sigue valiendo
        print(f"[eval_retrieval] sin etiquetas humanas ({type(exc).__name__})", file=sys.stderr)
    print(json.dumps(report, ensure_ascii=False, indent=1, default=str) if args.json else render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
