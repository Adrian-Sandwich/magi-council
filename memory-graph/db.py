"""Store compartido del grafo de memoria: nodos/edges en SQLite, natural-key
upsert para que correr los ingestors dos veces sea un no-op sobre el contenido.

Además de nodos y edges hay dos tablas de servicio:

- `file_cache`: los hechos ya extraídos de cada archivo de log, con su mtime y
  tamaño. Antes cada corrida de `refresh.sh` re-parseaba TODOS los `.jsonl` de
  Claude y Kimi desde cero, cosa que crece lineal con el histórico. Ahora sólo
  se re-parsea lo que cambió; el resto se lee de acá.
- `sweep_domain()`: el grafo sólo sabía crecer. `reset_source_edges()` limpiaba
  los edges salientes de una entidad, pero los NODOS de sesiones, docs o
  proyectos borrados quedaban para siempre. Ahora cada ingestor declara qué vio
  y lo que no aparece se borra.
"""

import json
import sqlite3
from pathlib import Path

from settings import DB_PATH  # noqa: F401  (re-exportado: db.DB_PATH era la API previa)

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    tag TEXT NOT NULL DEFAULT 'Unknown',
    domain TEXT NOT NULL,
    size REAL,
    tooltip TEXT,
    props TEXT NOT NULL DEFAULT '{}',
    source TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nodes_domain ON nodes(domain);
CREATE INDEX IF NOT EXISTS idx_nodes_source ON nodes(source);

CREATE TABLE IF NOT EXISTS edges (
    from_id TEXT NOT NULL,
    to_id TEXT NOT NULL,
    type TEXT NOT NULL,
    label_forward TEXT,
    label_backward TEXT,
    weight REAL NOT NULL DEFAULT 1.0,
    source TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (from_id, to_id, type)
);
CREATE INDEX IF NOT EXISTS idx_edges_from ON edges(from_id);
CREATE INDEX IF NOT EXISTS idx_edges_to ON edges(to_id);

CREATE TABLE IF NOT EXISTS file_cache (
    source TEXT NOT NULL,
    path TEXT NOT NULL,
    mtime REAL NOT NULL,
    size INTEGER NOT NULL,
    facts TEXT NOT NULL,
    PRIMARY KEY (source, path)
);

CREATE TABLE IF NOT EXISTS ingest_runs (
    source TEXT PRIMARY KEY,
    finished_at REAL NOT NULL,
    summary TEXT
);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def upsert_node(
    conn: sqlite3.Connection,
    id: str,
    domain: str,
    source: str,
    updated_at: str,
    label: str | None = None,
    tag: str = "Unknown",
    size: float | None = None,
    tooltip: str | None = None,
    props: dict | None = None,
) -> None:
    """Merge semantics: label/tag/tooltip/size se pisan, props se mergea
    (no se reemplaza) para que dos ingestors distintos tocando el mismo nodo
    (ej. un `file` citado por un doc y tocado por una sesión) no se borren
    los props del otro."""
    label = label if label is not None else id
    props = props or {}
    row = conn.execute("SELECT props FROM nodes WHERE id = ?", (id,)).fetchone()
    if row:
        merged = json.loads(row[0])
        merged.update(props)
        props = merged
    conn.execute(
        """
        INSERT INTO nodes (id, label, tag, domain, size, tooltip, props, source, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            label=excluded.label, tag=excluded.tag, domain=excluded.domain,
            size=excluded.size, tooltip=excluded.tooltip, props=excluded.props,
            source=excluded.source, updated_at=excluded.updated_at
        """,
        (id, label, tag, domain, size, tooltip, json.dumps(props), source, updated_at),
    )


def upsert_edge(
    conn: sqlite3.Connection,
    from_id: str,
    to_id: str,
    type: str,
    source: str,
    updated_at: str,
    label_forward: str | None = None,
    label_backward: str | None = None,
    weight: float = 1.0,
) -> None:
    conn.execute(
        """
        INSERT INTO edges (from_id, to_id, type, label_forward, label_backward, weight, source, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(from_id, to_id, type) DO UPDATE SET
            label_forward=excluded.label_forward, label_backward=excluded.label_backward,
            weight=excluded.weight, source=excluded.source, updated_at=excluded.updated_at
        """,
        (from_id, to_id, type, label_forward, label_backward, weight, source, updated_at),
    )


def reset_source_edges(conn: sqlite3.Connection, source: str, from_id: str) -> None:
    """Antes de reinsertar los edges salientes de una entidad, borra los
    viejos de ese mismo source/from_id — evita doble-conteo de weight al
    re-correr el ingestor sobre la misma sesión/doc."""
    conn.execute("DELETE FROM edges WHERE source = ? AND from_id = ?", (source, from_id))


# ------------------------------------------------------------ corridas

def record_run(conn: sqlite3.Connection, source: str, summary: str | None = None,
               finished_at: float | None = None) -> None:
    """Deja constancia de que un ingestor terminó. Existe porque el mtime de
    memory.db dejó de servir para saber si el refresh horario está vivo: el
    relay sincroniza conversaciones cada 30s y toca el archivo aunque
    sesiones, docs y código lleven días sin re-ingestarse. healthcheck mira
    la edad de cada fuente por separado."""
    import time
    conn.execute(
        """
        INSERT INTO ingest_runs (source, finished_at, summary) VALUES (?, ?, ?)
        ON CONFLICT(source) DO UPDATE SET
            finished_at=excluded.finished_at, summary=excluded.summary
        """,
        (source, finished_at if finished_at is not None else time.time(), summary),
    )


def last_runs(conn: sqlite3.Connection) -> dict[str, float]:
    """{source: finished_at (epoch)} de la última corrida de cada ingestor."""
    try:
        rows = conn.execute("SELECT source, finished_at FROM ingest_runs").fetchall()
    except sqlite3.OperationalError:
        return {}  # base anterior a la tabla: nadie registró corridas todavía
    return {source: finished_at for source, finished_at in rows}


# ------------------------------------------------------------ cache de archivos

def cached_facts(conn: sqlite3.Connection, source: str, path: Path) -> dict | None:
    """Hechos ya extraídos de este archivo, si sigue igual que la última vez.

    La clave es (mtime, size). Los logs de sesión son append-only, así que un
    archivo con el mismo mtime y tamaño tiene exactamente el mismo contenido.
    """
    try:
        st = path.stat()
    except OSError:
        return None
    row = conn.execute(
        "SELECT mtime, size, facts FROM file_cache WHERE source = ? AND path = ?",
        (source, str(path)),
    ).fetchone()
    if not row:
        return None
    mtime, size, facts = row
    if mtime != st.st_mtime or size != st.st_size:
        return None
    return json.loads(facts)


def store_facts(conn: sqlite3.Connection, source: str, path: Path, facts: dict) -> None:
    try:
        st = path.stat()
    except OSError:
        return
    conn.execute(
        """
        INSERT INTO file_cache (source, path, mtime, size, facts)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(source, path) DO UPDATE SET
            mtime=excluded.mtime, size=excluded.size, facts=excluded.facts
        """,
        (source, str(path), st.st_mtime, st.st_size, json.dumps(facts)),
    )


def forget_missing_files(conn: sqlite3.Connection, source: str, seen: set[str]) -> int:
    """Saca del cache los archivos que ya no están en disco."""
    rows = conn.execute("SELECT path FROM file_cache WHERE source = ?", (source,)).fetchall()
    gone = [(source, p) for (p,) in rows if p not in seen]
    conn.executemany("DELETE FROM file_cache WHERE source = ? AND path = ?", gone)
    return len(gone)


# ------------------------------------------------------------ recolección

def sweep_domain(conn: sqlite3.Connection, domain: str, seen: set[str]) -> int:
    """Borra los nodos de un dominio que este ingestor ya no produce, junto con
    sus edges. Se pasa el dominio y no el `source` a propósito: un mismo nodo
    (típicamente un `project:`) lo escriben varios ingestors y el último gana
    la columna `source`, así que barrer por source borraría cosas vivas. Los
    dominios sí son exclusivos de un ingestor."""
    rows = conn.execute("SELECT id FROM nodes WHERE domain = ?", (domain,)).fetchall()
    stale = [(r[0],) for r in rows if r[0] not in seen]
    if not stale:
        return 0
    conn.executemany("DELETE FROM nodes WHERE id = ?", stale)
    conn.executemany("DELETE FROM edges WHERE from_id = ?", stale)
    conn.executemany("DELETE FROM edges WHERE to_id = ?", stale)
    return len(stale)


def gc_orphans(conn: sqlite3.Connection, domains: tuple[str, ...] = ("file", "project")) -> int:
    """Nodos derivados que quedaron sin ninguna arista. Un `file:` sólo existe
    porque alguna sesión lo tocó; si esa sesión se barrió, el archivo ya no
    tiene por qué estar en el grafo."""
    placeholders = ",".join("?" for _ in domains)
    rows = conn.execute(
        f"""
        SELECT id FROM nodes
        WHERE domain IN ({placeholders})
          AND id NOT IN (SELECT from_id FROM edges)
          AND id NOT IN (SELECT to_id FROM edges)
        """,
        domains,
    ).fetchall()
    if not rows:
        return 0
    conn.executemany("DELETE FROM nodes WHERE id = ?", [(r[0],) for r in rows])
    return len(rows)
