"""Camina ~/.claude/projects/**/*.jsonl (incluye subagents/, que reusan el
sessionId de la sesión padre). Un nodo por sesión, un nodo por proyecto (cwd),
edges touched (Read/Edit/Write/NotebookEdit -> archivo) y posted_to (llamadas
mcp__debate__* -> thread).

Varios archivos .jsonl pueden compartir el mismo sessionId (transcripts de
subagentes) — por eso se extraen los hechos de CADA archivo primero y se
agregan por sessionId antes de escribir a la DB, en vez de escribir por
archivo (que pisaría los edges del anterior por el mismo id)."""

from datetime import datetime, timezone
from pathlib import Path

import db
import ingest_debate
import settings
from code_lookup import CodeIndex
from jsonl_facts import FILE_PATH_KEYS, Facts, merge_facts, read_jsonl  # noqa: F401

SOURCE = "ingest_claude"
PROJECTS_DIR = settings.CLAUDE_PROJECTS_DIR


def project_id(cwd: str) -> str:
    return f"project:{cwd}"


def session_id(sid: str) -> str:
    return f"claude_session:{sid}"


def resolve_file_node(code_index: CodeIndex, abs_path: str) -> str:
    resolved = code_index.resolve_file(abs_path)
    return resolved if resolved else f"file:{abs_path}"


def extract_facts(path: Path) -> dict | None:
    """Dialecto de Claude Code: un objeto JSON por línea, con `sessionId` y
    `cwd` repetidos en cada uno, los turnos marcados por `type` user/assistant,
    y las llamadas a herramientas como bloques `tool_use` dentro de
    `message.content`."""
    facts = Facts()
    sid = cwd = ai_title = first_user_text = None

    for d in read_jsonl(path, facts):
        sid = sid or d.get("sessionId")
        cwd = cwd or d.get("cwd")
        facts.observe_ts(d.get("timestamp"))

        t = d.get("type")
        if t == "ai-title":
            ai_title = d.get("aiTitle")
            continue
        if t not in ("user", "assistant"):
            continue

        facts.n_turns += 1
        content = d.get("message", {}).get("content")
        if t == "user" and first_user_text is None and isinstance(content, str):
            first_user_text = content
        if t != "assistant" or not isinstance(content, list):
            continue

        for block in content:
            if block.get("type") != "tool_use":
                continue
            inp = block.get("input") or {}
            facts.touch_from(inp)
            if block.get("name", "").startswith("mcp__debate__") and "thread" in inp:
                facts.note_thread(inp["thread"])

    if sid is None:
        return None  # archivo sin sessionId: no es un transcript utilizable

    return facts.as_dict(
        sid=sid, cwd=cwd, ai_title=ai_title, first_user_text=first_user_text,
    )


def merge(facts_list: list[dict]) -> dict:
    """Varios .jsonl pueden compartir sessionId (los transcripts de
    subagentes): hay que sumarlos, no dejar que el último pise al anterior."""
    return merge_facts(facts_list, first_wins=("cwd", "ai_title", "first_user_text"))


def write_session(conn, code_index: CodeIndex, sid: str, m: dict, now: str) -> None:
    cwd = m["cwd"] or "unknown"
    label = m["ai_title"] or (m["first_user_text"][:80] if m["first_user_text"] else sid)
    pid = project_id(cwd)
    db.upsert_node(conn, id=pid, domain="project", source=SOURCE, updated_at=now, label=Path(cwd).name, tag="Project")

    sid_node = session_id(sid)
    db.upsert_node(
        conn, id=sid_node, domain="claude_session", source=SOURCE, updated_at=now,
        label=label, tag="ClaudeSession",
        size=min(30, max(6, 6 + m["n_turns"] // 5)),
        tooltip=f"{m['n_turns']} turnos, {m['first_ts']} - {m['last_ts']}",
        props={"cwd": cwd, "n_turns": m["n_turns"], "first_ts": m["first_ts"], "last_ts": m["last_ts"], "bad_lines": m["bad_lines"]},
    )

    db.reset_source_edges(conn, SOURCE, sid_node)
    db.upsert_edge(conn, sid_node, pid, "belongs_to", source=SOURCE, updated_at=now)
    for fpath, count in m["touched"].items():
        target = resolve_file_node(code_index, fpath)
        if not target.startswith("code:"):
            db.upsert_node(conn, id=target, domain="file", source=SOURCE, updated_at=now, label=Path(fpath).name, tag="File")
        db.upsert_edge(conn, sid_node, target, "touched", source=SOURCE, updated_at=now, weight=float(count))
    for thread in m["threads"]:
        db.upsert_edge(conn, sid_node, ingest_debate.node_id(thread), "posted_to", source=SOURCE, updated_at=now)


def main() -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn = db.connect()
    code_index = CodeIndex()

    by_sid: dict[str, list[dict]] = {}
    seen_paths: set[str] = set()
    n_files = n_empty = n_cached = 0
    for jsonl_path in PROJECTS_DIR.rglob("*.jsonl"):
        n_files += 1
        seen_paths.add(str(jsonl_path))
        # Los transcripts son append-only: mismo mtime y tamaño => mismo
        # contenido, y parsear es lo caro de esta corrida.
        facts = db.cached_facts(conn, SOURCE, jsonl_path)
        if facts is not None:
            n_cached += 1
        else:
            facts = extract_facts(jsonl_path)
            if facts is not None:
                db.store_facts(conn, SOURCE, jsonl_path, facts)
        if facts is None:
            n_empty += 1
            continue
        by_sid.setdefault(facts["sid"], []).append(facts)

    for sid, facts_list in by_sid.items():
        write_session(conn, code_index, sid, merge(facts_list), now)

    n_swept = db.sweep_domain(conn, "claude_session", {session_id(s) for s in by_sid})
    db.forget_missing_files(conn, SOURCE, seen_paths)

    code_index.close()
    db.record_run(conn, "ingest_claude")
    conn.commit()
    conn.close()
    print(
        f"[ingest_claude] {n_files} archivos ({n_cached} sin cambios, "
        f"{n_empty} vacíos/sin sessionId) -> {len(by_sid)} sesiones, "
        f"{n_swept} borradas"
    )


if __name__ == "__main__":
    main()
