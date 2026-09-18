"""Vuelca el layer de código desde las SQLite de codebase-memory-mcp
(~/.cache/codebase-memory-mcp/<project>.db) — se leen directo, sin pasar por
el protocolo MCP. Labels estructurales + Function/Method con sus edges
CALLS/DEFINES_METHOD; Section/Variable/Field/Decorator/EnvVar quedan afuera
(ruido puro para el visor)."""

import json
import sqlite3
from datetime import datetime, timezone

import db
from code_lookup import CACHE_DIR

SOURCE = "ingest_code"

KEEP_LABELS = {
    "Project", "Package", "Folder", "File", "Module", "Class", "Route",
    "Enum", "Interface", "Function", "Method",
}
KEEP_EDGE_TYPES = {
    "CONTAINS_FILE", "CONTAINS_FOLDER", "IMPORTS", "DEPENDS_ON",
    "INHERITS", "DEFINES", "CALLS", "DEFINES_METHOD",
}


def code_node_id(project: str, qualified_name: str) -> str:
    return f"code:{project}:{qualified_name}"


def ingest_project_db(conn, db_path, seen: set[str]) -> tuple[int, int]:
    now = datetime.now(timezone.utc).isoformat()
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row

    proj_row = src.execute("SELECT name, root_path FROM projects LIMIT 1").fetchone()
    if not proj_row:
        src.close()
        return 0, 0
    project = proj_row["name"]

    placeholders = ",".join("?" for _ in KEEP_LABELS)
    nodes = src.execute(
        f"SELECT id, label, name, qualified_name, file_path, start_line, end_line, properties "
        f"FROM nodes WHERE label IN ({placeholders})",
        tuple(KEEP_LABELS),
    ).fetchall()

    id_to_qn: dict[int, str] = {}
    n_nodes = 0
    for r in nodes:
        id_to_qn[r["id"]] = r["qualified_name"]
        seen.add(code_node_id(project, r["qualified_name"]))
        props = json.loads(r["properties"] or "{}")
        props.update({
            "file_path": r["file_path"],
            "start_line": r["start_line"],
            "end_line": r["end_line"],
            "project": project,
        })
        db.upsert_node(
            conn,
            id=code_node_id(project, r["qualified_name"]),
            domain="code",
            source=SOURCE,
            updated_at=now,
            label=r["name"],
            tag=r["label"],
            size=14 if r["label"] in ("Project", "Package") else 6 if r["label"] in ("Function", "Method") else 8,
            tooltip=f"{r['label']} · {r['file_path']}",
            props=props,
        )
        n_nodes += 1

    placeholders_e = ",".join("?" for _ in KEEP_EDGE_TYPES)
    edges = src.execute(
        f"SELECT source_id, target_id, type FROM edges WHERE type IN ({placeholders_e})",
        tuple(KEEP_EDGE_TYPES),
    ).fetchall()

    n_edges = 0
    for r in edges:
        src_qn = id_to_qn.get(r["source_id"])
        tgt_qn = id_to_qn.get(r["target_id"])
        if src_qn is None or tgt_qn is None:
            continue  # el otro extremo es un Function/Method/etc que dejamos afuera
        db.upsert_edge(
            conn,
            code_node_id(project, src_qn),
            code_node_id(project, tgt_qn),
            r["type"],
            source=SOURCE,
            updated_at=now,
        )
        n_edges += 1

    src.close()
    return n_nodes, n_edges


def main() -> None:
    conn = db.connect()
    seen: set[str] = set()
    total_nodes = total_edges = 0
    for db_path in sorted(CACHE_DIR.glob("*.db")):
        if db_path.stem == "_config":
            continue
        n_nodes, n_edges = ingest_project_db(conn, db_path, seen)
        print(f"[ingest_code] {db_path.stem}: {n_nodes} nodos, {n_edges} edges")
        total_nodes += n_nodes
        total_edges += n_edges
    # Un proyecto reindexado pierde funciones que ya no existen, y uno
    # borrado del cache desaparece entero: sin esto quedaban para siempre.
    n_swept = db.sweep_domain(conn, "code", seen)
    db.record_run(conn, "ingest_code")
    conn.commit()
    conn.close()
    print(f"[ingest_code] total: {total_nodes} nodos, {total_edges} edges, {n_swept} borrados")


if __name__ == "__main__":
    main()
