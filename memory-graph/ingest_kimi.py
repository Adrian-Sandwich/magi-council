"""Camina ~/.kimi-code/sessions/wd_<slug>/session_<uuid>/agents/*/wire.jsonl.
No confía en session_index.jsonl como enumeración (se vio desactualizado —
11 líneas mientras había sesiones nuevas en disco no listadas); enumera
caminando los directorios reales y resuelve el proyecto vía workspaces.json.

Nota de compatibilidad: distintas versiones de kimi-code escriben distinto
wire.jsonl, y el ingestor tolera ambos formatos. Hubo una nota acá diciendo
que la build 0.34.0 había dejado de emitir eventos `tool.call` — al 2026-08-26
eso ya NO es cierto: sobre los 75 wire.jsonl del disco hay 1900 `tool.call`
(1032 con path, 144 con thread) y 193 `turn.prompt`, con sólo 2 de 75 archivos
sin prompts. La nota vieja envejeció en silencio y se dio por buena durante
semanas. Por eso el chequeo ahora vive en `tests/test_parsers.py`, que corre
contra los logs reales y falla si un evento desaparece — en vez de en un
comentario que nadie revalida."""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import db
import ingest_debate
import settings
from code_lookup import CodeIndex
from ingest_claude import project_id, resolve_file_node
from jsonl_facts import Facts, merge_facts, read_jsonl

SOURCE = "ingest_kimi"
KIMI_HOME = settings.KIMI_HOME
SESSIONS_DIR = KIMI_HOME / "sessions"


def load_workspaces() -> dict:
    """El mapa slug→root del workspace. Sin el archivo (kimi-code nunca corrió
    acá, p.ej.) degrade a un mapa vacío con aviso: refresh.sh corre con
    `set -euo pipefail` y un FileNotFoundError acá tumba la re-ingesta entera
    antes de que corran los demás ingestores."""
    try:
        data = json.loads((KIMI_HOME / "workspaces.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[ingest_kimi] workspaces.json ilegible ({exc}); asumo sin workspaces", file=sys.stderr)
        return {}
    return {slug: w["root"] for slug, w in data.get("workspaces", {}).items()}


def kimi_session_id(sid: str) -> str:
    return f"kimi_session:{sid}"


def extract_agent_facts(path: Path) -> dict:
    """Dialecto de kimi-code: los turnos son eventos `turn.prompt` con el texto
    en `input`, y las llamadas a herramientas van envueltas en
    `context.append_loop_event` -> `event.type == "tool.call"`. El timestamp es
    `time`, en milisegundos epoch (Claude usa ISO en `timestamp`)."""
    facts = Facts()
    first_prompt = None

    for d in read_jsonl(path, facts):
        facts.observe_ts(d.get("time"))

        t = d.get("type")
        if t == "turn.prompt":
            facts.n_turns += 1
            if first_prompt is None:
                for block in d.get("input", []) or []:
                    if block.get("type") == "text":
                        first_prompt = block["text"]
                        break
            continue

        if t != "context.append_loop_event":
            continue
        ev = d.get("event", {})
        if ev.get("type") != "tool.call":
            continue

        args = ev.get("args") or {}
        facts.touch_from(args)
        # Sólo entran tools cuyo arg es `thread`. Los de decisiones
        # (start_decision/cast_position/get_decision) nombran decision_id, no
        # thread — igual no se pierde el vínculo: las cabezas leen el journal
        # con read_thread antes de votar, y ese sí queda anotado.
        if ev.get("name") in ("post_message", "read_thread", "wait_messages") and "thread" in args:
            facts.note_thread(args["thread"])

    return facts.as_dict(first_prompt=first_prompt)


def merge(facts_list: list[dict]) -> dict:
    """Kimi guarda un wire.jsonl por agente dentro de la misma sesión."""
    return merge_facts(facts_list, first_wins=("first_prompt",))


def epoch_ms_to_iso(ms) -> str | None:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def main() -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn = db.connect()
    code_index = CodeIndex()
    workspaces = load_workspaces()

    seen_sessions: set[str] = set()
    seen_paths: set[str] = set()
    n_sessions = n_cached = 0
    for workspace_dir in SESSIONS_DIR.glob("wd_*"):
        cwd = workspaces.get(workspace_dir.name)
        if not cwd:
            continue  # workspace borrado/desconocido, sin root_path confiable
        for session_dir in workspace_dir.glob("session_*"):
            sid = session_dir.name.removeprefix("session_")
            wire_files = list(session_dir.glob("agents/*/wire.jsonl"))
            if not wire_files:
                continue

            facts_list = []
            for wire in wire_files:
                seen_paths.add(str(wire))
                facts = db.cached_facts(conn, SOURCE, wire)
                if facts is not None:
                    n_cached += 1
                else:
                    facts = extract_agent_facts(wire)
                    db.store_facts(conn, SOURCE, wire, facts)
                facts_list.append(facts)
            m = merge(facts_list)

            pid = project_id(cwd)
            db.upsert_node(conn, id=pid, domain="project", source=SOURCE, updated_at=now, label=Path(cwd).name, tag="Project")

            sid_node = kimi_session_id(sid)
            seen_sessions.add(sid_node)
            label = m["first_prompt"][:80] if m["first_prompt"] else sid
            db.upsert_node(
                conn, id=sid_node, domain="kimi_session", source=SOURCE, updated_at=now,
                label=label, tag="KimiSession",
                size=min(30, max(6, 6 + m["n_turns"] // 2)),
                tooltip=f"{m['n_turns']} turnos, {epoch_ms_to_iso(m['first_ts'])} - {epoch_ms_to_iso(m['last_ts'])}",
                props={
                    "cwd": cwd, "n_turns": m["n_turns"], "n_agents": len(wire_files),
                    "first_ts": epoch_ms_to_iso(m["first_ts"]), "last_ts": epoch_ms_to_iso(m["last_ts"]),
                    "bad_lines": m["bad_lines"],
                },
            )

            db.reset_source_edges(conn, SOURCE, sid_node)
            db.upsert_edge(conn, sid_node, pid, "belongs_to", source=SOURCE, updated_at=now)
            for fpath, count in m["touched"].items():
                abs_path = fpath if Path(fpath).is_absolute() else str(Path(cwd) / fpath)
                target = resolve_file_node(code_index, abs_path)
                if not target.startswith("code:"):
                    db.upsert_node(conn, id=target, domain="file", source=SOURCE, updated_at=now, label=Path(fpath).name, tag="File")
                db.upsert_edge(conn, sid_node, target, "touched", source=SOURCE, updated_at=now, weight=float(count))
            for thread in m["threads"]:
                db.upsert_edge(conn, sid_node, ingest_debate.node_id(thread), "posted_to", source=SOURCE, updated_at=now)

            n_sessions += 1

    n_swept = db.sweep_domain(conn, "kimi_session", seen_sessions)
    db.forget_missing_files(conn, SOURCE, seen_paths)

    code_index.close()
    db.record_run(conn, "ingest_kimi")
    conn.commit()
    conn.close()
    print(f"[ingest_kimi] {n_sessions} sesiones ({n_cached} archivos sin cambios), {n_swept} borradas")


if __name__ == "__main__":
    main()
