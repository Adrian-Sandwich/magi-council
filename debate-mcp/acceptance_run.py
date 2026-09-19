#!/usr/bin/env python3
"""Corrida de aceptación del ejecutor (plan de madurez, 2.5).

Mete N planes pequeños al consejo por la API de la UI, contra un repo de
prueba, y sigue cada uno hasta su final: aprobado → ejecutado → revisado →
mergeado (o bloqueado, fallido, rechazado). Si la revisión aprobó con 2/3,
autoriza el merge como haría el operador; si la ejecución falla, reintenta
UNA vez con «seguí». Todo queda en docs/executor-runs.md y en
logs/acceptance_run.json, con la causa de cada fallo.

    .venv/Scripts/python.exe acceptance_run.py --repo ../experiments/acceptance-repo

No reinicies MAGI mientras corre: stop-magi apaga Postgres y mata la
ejecución en curso.
"""

import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
UI = "http://127.0.0.1:8051"
TOKEN_PATH = BASE_DIR / "logs" / "ui_token"
OUT_JSON = BASE_DIR / "logs" / "acceptance_run.json"
OUT_MD = BASE_DIR.parent / "docs" / "executor-runs.md"
POLL_SECS = 15
TASK_TIMEOUT_SECS = 30 * 60

TASKS = [
    "Implementa: agregar tests de casos límite para slugify en inventario/tests/test_core.py "
    "(texto vacío, sólo símbolos, acentos, guiones repetidos) sin cambiar utils.py.",
    "Implementa: arreglar Inventario.average() en inventario/core.py para que devuelva 0.0 con "
    "el inventario vacío en vez de ZeroDivisionError, con un test que lo cubra.",
    "Implementa: agregar validación a Inventario.add_item para rechazar nombres vacíos o sólo "
    "espacios con ValueError, y cantidades negativas con ValueError, con tests.",
    "Implementa: renombrar la función helper de inventario/utils.py a format_row en todo el "
    "paquete (utils, core y tests), manteniendo el comportamiento.",
    "Implementa: agregar un flag --version a inventario/cli.py que imprima la versión de "
    "inventario.__version__ y salga con 0, con un test.",
    "Implementa: hacer que load_items en inventario/utils.py acepte tanto str como pathlib.Path "
    "y que ignore líneas que empiecen con #, con tests.",
    "Implementa: documentar parse_config y load_items en README.md con un ejemplo de uso cada "
    "una, sin tocar código.",
    "Implementa: agregar un archivo CHANGELOG.md con una sección 0.1.0 que resuma lo que hay "
    "en el paquete (core, utils, cli) y una sección Unreleased vacía.",
    "Implementa: agregar type hints a todas las funciones públicas de inventario/utils.py y "
    "inventario/core.py sin cambiar comportamiento; los tests existentes deben seguir pasando.",
    "Implementa: agregar Inventario.remove_item que devuelva también si el item quedó en cero "
    "(tupla (cantidad, agotado)) y actualizar el test existente.",
]


def _token() -> str:
    return TOKEN_PATH.read_text(encoding="ascii").strip()


def _request(method: str, path: str, payload=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(UI + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", "X-Magi-Token": _token()})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def state_of(decision_id: int) -> dict | None:
    snapshot = _request("GET", "/state")
    return next((d for d in snapshot["decisions"] if d["id"] == decision_id), None)


def votes_of(d: dict) -> str:
    return ", ".join(f"{s['seat']}={s.get('position') or '-'}" for s in d.get("seats", []))


def run_task(index: int, task: str, repo: str, log) -> dict:
    started = time.time()
    record = {"task": index, "request": task, "started": datetime.now().isoformat(timespec="seconds"),
              "events": []}
    opened = _request("POST", "/message", {"mode": "council", "body": task, "artifact": repo,
                                            "force_new": True, "production": True})
    did = opened["decision_id"]
    record["decision_id"] = did
    log(f"[{index}] #{did} abierta")
    retried = False
    authorized = False
    last_state = None
    last_review = None  # /state deja de traer la revisión cuando la decisión cierra
    while time.time() - started < TASK_TIMEOUT_SECS:
        d = state_of(did)
        if d and d.get("review"):
            last_review = d["review"]
        if d is None:
            record["events"].append("la decisión desapareció del estado")
            record["final"] = "desaparecida"
            break
        key = (d["status"], d.get("execution_state"), d.get("execution_cause"), (d.get("review") or {}).get("status"))
        if key != last_state:
            last_state = key
            record["events"].append(f"{round(time.time() - started)}s: {d['status']}/{d.get('execution_state')} "
                                    f"votos[{votes_of(d)}] revisión={d.get('review')}")
            log(f"[{index}] #{did} {d['status']}/{d.get('execution_state')} {votes_of(d)}")
        status, ex = d["status"], d.get("execution_state")
        if status == "split":
            record["final"] = "split"
            record["ruling"] = None
            _request("POST", "/abort", {"decision_id": did})
            record["events"].append("split: abortada para seguir con la corrida")
            break
        if status == "closed" and ex != "merged":
            record["final"] = "rechazada" if d.get("ruling") in ("no", "info") else f"cerrada:{d.get('ruling')}"
            record["ruling"] = d.get("ruling")
            break
        if ex == "merged":
            record["final"] = "merged" + (" (2/3 autorizado)" if authorized else "")
            break
        if ex == "failed":
            record["cause"] = d.get("execution_cause")
            if not retried:
                retried = True
                record["events"].append(f"fallo [{record['cause']}]: reintento único con «seguí»")
                _request("POST", "/message", {"mode": "council", "decision_id": did, "body": "seguí"})
                time.sleep(POLL_SECS)
                continue
            record["final"] = f"failed:{record['cause']}"
            break
        if ex == "merge_blocked":
            review = d.get("review") or {}
            if (not authorized and review.get("ruling") == "yes"
                    and float(review.get("confidence") or 0) >= 0.66):
                authorized = True
                _request("POST", "/merge-majority", {"decision_id": did})
                record["events"].append(f"revisión #{review.get('id')} aprobó 2/3: merge autorizado")
                time.sleep(POLL_SECS)
                continue
            record["final"] = "merge_blocked"
            record["review"] = review
            break
        time.sleep(POLL_SECS)
    else:
        record["final"] = "timeout"
        _request("POST", "/abort", {"decision_id": did})
    d = state_of(did) or {}
    if last_review and last_review.get("id"):
        # la revisión es otra decisión del tablero: su veredicto final vive ahí
        rv = state_of(last_review["id"]) or {}
        last_review = {**last_review, "status": rv.get("status", last_review.get("status")),
                       "ruling": rv.get("ruling"), "confidence": rv.get("confidence")}
    record.update(elapsed_s=round(time.time() - started), votes=votes_of(d),
                  review=d.get("review") or last_review,
                  ruling=d.get("ruling"), retried=retried, authorized=authorized,
                  cause=record.get("cause") or d.get("execution_cause"))
    log(f"[{index}] #{did} FINAL {record['final']} ({record['elapsed_s']}s)")
    return record


def render(records: list[dict], repo: str) -> str:
    reached_review = sum(1 for r in records if r.get("review"))
    merged = sum(1 for r in records if str(r.get("final", "")).startswith("merged"))
    lines = [f"# Corrida de aceptación del ejecutor — {datetime.now():%Y-%m-%d}",
             "",
             f"Repositorio de prueba: `{repo}`. {len(records)} planes; {reached_review} llegaron a revisión; "
             f"{merged} se integraron. Criterio del plan de madurez (2.5): ≥ 8/10 a revisión, ≥ 5/10 mergeados.",
             "",
             "| # | decisión | resultado | tiempo | votos del plan | revisión | causa | reintento |",
             "|---|---|---|---|---|---|---|---|"]
    for r in records:
        rev = r.get("review") or {}
        rev_txt = f"#{rev.get('id')} {rev.get('ruling')} {rev.get('confidence')}" if rev else "—"
        final = f"{r.get('final')} (repetido)" if r.get("rerun") else r.get("final")
        lines.append(f"| {r['task']} | #{r.get('decision_id')} | {final} | {r.get('elapsed_s')}s | "
                     f"{r.get('votes')} | {rev_txt} | {r.get('cause') or '—'} | {'sí' if r.get('retried') else 'no'} |")
    lines += ["", "## Peticiones", ""]
    for r in records:
        lines.append(f"{r['task']}. {r['request']}")
    lines += ["", "## Eventos", ""]
    for r in records:
        lines.append(f"### Plan {r['task']} (#{r.get('decision_id')})")
        lines.extend(f"- {e}" for e in r["events"])
        lines.append("")
    return "\n".join(lines)


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", required=True)
    parser.add_argument("--limit", type=int, default=len(TASKS))
    parser.add_argument("--start", type=int, default=1, help="primer plan (1-based), para reanudar")
    parser.add_argument("--only", type=int, action="append", default=[],
                        help="repetir sólo estos planes (1-based) y reemplazar su registro; el resto se conserva")
    parser.add_argument("--note", default="", help="con --only: por qué se repite, queda en los eventos del plan")
    args = parser.parse_args(argv)
    repo = str(Path(args.repo).resolve())
    records = []
    if OUT_JSON.exists() and (args.start > 1 or args.only):
        records = json.loads(OUT_JSON.read_text(encoding="utf-8"))["records"]
        if not args.only:
            records = records[: args.start - 1]

    def log(msg):
        print(f"{datetime.now():%H:%M:%S} {msg}", flush=True)

    def attempt(index, task):
        try:
            return run_task(index, task, repo, log)
        except Exception as exc:  # una tarea rota no tira la corrida
            log(f"[{index}] ERROR {type(exc).__name__}: {exc}")
            return {"task": index, "request": task, "final": f"error:{type(exc).__name__}",
                    "events": [str(exc)], "elapsed_s": 0}

    def save():
        OUT_JSON.write_text(json.dumps({"repo": repo, "records": records}, ensure_ascii=False, indent=1),
                            encoding="utf-8")
        OUT_MD.write_text(render(records, repo), encoding="utf-8")

    if args.only:
        # repetir planes sueltos (una suspensión de la máquina, un bug del relay ya
        # corregido) sin perder los registros de los demás
        for index in args.only:
            record = attempt(index, TASKS[index - 1])
            previous = next((r for r in records if r.get("task") == index), None)
            if previous:
                note = f" — {args.note}" if args.note else ""
                record["events"].insert(0, f"corrida anterior: #{previous.get('decision_id')} "
                                           f"{previous.get('final')} ({previous.get('elapsed_s')}s){note}")
                records[records.index(previous)] = record
            else:
                records.append(record)
            record["rerun"] = True
            records.sort(key=lambda r: r.get("task", 0))
            save()
        log("corrida terminada")
        return 0

    for index, task in enumerate(TASKS[: args.limit], start=1):
        if index < args.start:
            continue
        records.append(attempt(index, task))
        save()
    log("corrida terminada")
    return 0


if __name__ == "__main__":
    sys.exit(main())
