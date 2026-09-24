#!/usr/bin/env python3
"""¿Los sesgos de las cabezas cambian algo, o sólo se recitan?

En el tablero real no se puede saber: cada asiento es un proveedor distinto
(kimi/codex/claude), así que la discrepancia de Melchior puede ser de Kimi y
no de "la verdad técnica". Este experimento pone el MISMO modelo en los tres
asientos y re-vota decisiones pasadas del tablero en tres modos de persona
(personas.py): off, rhetorical (el de siempre) y behavioral (con obligación).
Cada cabeza recibe el mismo journal humano previo al primer voto, sin
herramientas ni repositorio, para que la única variable sea la persona.

Mide, por modo: acuerdo de voto por pareja, decisiones unánimes, cuántos
votos nombran su eje (recitado) y diversidad de argumentos (1 − Jaccard
medio de vocabulario entre cabezas). Guarda las respuestas crudas en JSON.

Con `--mixed` corre la otra mitad del experimento: cada asiento con su
proveedor real (el consejo de todos los días). Comparando las dos corridas
sobre las MISMAS decisiones se separa lo que aporta la persona de lo que
aporta el proveedor: en la corrida de un solo modelo la única variable es
la persona; en la mixta se suma el proveedor.

    .venv/Scripts/python.exe persona_ab.py --limit 20 --seat casper --jobs 3
    .venv/Scripts/python.exe persona_ab.py --limit 20 --mixed --jobs 3
    .venv/Scripts/python.exe persona_ab.py --report logs/persona_ab.json

No escribe nada en Postgres: lee decisiones y mensajes, vota en el vacío.
"""

import argparse
import json
import re
import sys
import tempfile
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import apihead  # noqa: E402
import heads  # noqa: E402
import personas  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_OUT = BASE_DIR / "logs" / "persona_ab.json"
SEATS = ("melchior", "balthasar", "casper")
CONTROL = ("seguí", "segui", "retry", "reintent")
# Un plan de producción y su revisión no son deliberaciones: son tareas
# mecánicas del ejecutor («Implementa: agregar type hints…»), donde las tres
# cabezas dicen que sí por lo mismo. Meterlas al experimento infla el acuerdo
# y hunde la diversidad sin que la persona tenga nada que ver.
PLAN_PREFIXES = ("implementa:", "revisar implementación de", "revisar implementacion de")
# Palabras con las que cada cabeza suele recitar su eje/sesgo.
AXIS_WORDS = {
    "melchior": ("hechos", "verdad técnica", "frialdad", "mi eje", "verifiqué", "verificado"),
    "balthasar": ("daño", "cuidado", "sobreprotec", "mi eje", "irreversible", "mitigaci"),
    "casper": ("deseo", "queremos", "conveniencia", "bancar", "mi eje", "más barato", "operador quiere"),
}
STOP = set("para sobre como esta este esto estas estos desde hasta entre porque pero también cuando donde".split())


def _normal(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", (text or "").casefold())
                   if not unicodedata.combining(c))


def vocabulary(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-záéíóúñ]{5,}", _normal(text)) if t not in STOP}


def is_execution_plan(row: dict) -> bool:
    """Plan de producción o revisión de uno."""
    title = (row.get("title") or "").strip().lower()
    return bool(row.get("production")) or title.startswith(PLAN_PREFIXES)


def pick_decisions(rows: list[dict], limit: int) -> list[dict]:
    """Decisiones con tres votos en la ronda 1 y un título con contenido, la
    mitad con artefacto (código) y la mitad sin él (documento/filosofía),
    para que el resultado no dependa del tipo de pregunta."""
    usable = [r for r in rows if r.get("votes") == 3 and len(r.get("title") or "") >= 20
              and not (r["title"] or "").strip().lower().startswith(CONTROL)
              and not is_execution_plan(r)]
    with_repo = [r for r in usable if r.get("artifact")]
    without = [r for r in usable if not r.get("artifact")]
    half = max(1, limit // 2)
    chosen = with_repo[-half:] + without[-(limit - min(half, len(with_repo))):]
    return sorted(chosen, key=lambda r: r["id"])[:limit]


def metrics(results: list[dict]) -> dict:
    """results: [{decision_id, mode, seat, position, body}] → por modo."""
    by_mode: dict[str, dict] = {}
    for mode in sorted({r["mode"] for r in results}):
        rows = [r for r in results if r["mode"] == mode and r.get("position")]
        by_decision: dict[int, dict[str, dict]] = {}
        for r in rows:
            by_decision.setdefault(r["decision_id"], {})[r["seat"]] = r
        pair_agree = pair_n = 0
        unanimous = decided = 0
        jaccards = []
        for votes in by_decision.values():
            if len(votes) < 2:
                continue
            decided += 1
            unanimous += len({v["position"] for v in votes.values()}) == 1
            for a, b in combinations(sorted(votes), 2):
                pair_n += 1
                pair_agree += votes[a]["position"] == votes[b]["position"]
                va, vb = vocabulary(votes[a]["body"]), vocabulary(votes[b]["body"])
                if va or vb:
                    jaccards.append(len(va & vb) / len(va | vb))
        recited = sum(1 for r in rows if any(w in _normal(r["body"]) for w in map(_normal, AXIS_WORDS[r["seat"]])))
        by_mode[mode] = {
            "votes": len(rows), "decisions": decided,
            "pair_agreement": round(pair_agree / pair_n, 3) if pair_n else None,
            "unanimous_rate": round(unanimous / decided, 3) if decided else None,
            "axis_recited_rate": round(recited / len(rows), 3) if rows else None,
            "argument_diversity": round(1 - sum(jaccards) / len(jaccards), 3) if jaccards else None,
            "positions": {seat: dict(sorted(_count(r["position"] for r in rows if r["seat"] == seat).items()))
                          for seat in SEATS},
        }
    return by_mode


def _count(values):
    out: dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return out


def render(report: dict) -> str:
    lines = [f"[persona_ab] {report['decisions']} decisiones × {len(report['modes'])} modos, "
             f"{report['seat_config']}"]
    lines.append(f"  {'modo':<11} {'votos':>5} {'acuerdo':>8} {'unánime':>8} {'recita':>7} {'diversidad':>10}")
    for mode, m in report["modes"].items():
        lines.append(f"  {mode:<11} {m['votes']:>5} {_pct(m['pair_agreement']):>8} {_pct(m['unanimous_rate']):>8} "
                     f"{_pct(m['axis_recited_rate']):>7} {_pct(m['argument_diversity']):>10}")
    for mode, m in report["modes"].items():
        lines.append(f"  {mode}: " + "; ".join(f"{s} {m['positions'][s]}" for s in SEATS))
    lines.append("  (acuerdo = votos iguales por pareja; diversidad = 1 − Jaccard de vocabulario; "
                 + ("cada asiento con su proveedor" if report.get("mixed") else "mismo modelo en los tres asientos")
                 + ", sin herramientas)")
    return "\n".join(lines)


def _pct(v) -> str:
    return "-" if v is None else f"{v * 100:.0f}%"


# ------------------------------------------------------------------ corrida

def _load_decisions(limit: int) -> list[dict]:
    from config import connect
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT d.id, d.title, d.artifact, d.protocol, d.thread, d.production,
                   (SELECT count(*) FROM positions p WHERE p.decision_id = d.id AND p.round = 1) AS votes,
                   (SELECT min(p.message_id) FROM positions p WHERE p.decision_id = d.id) AS first_vote
            FROM decisions d ORDER BY d.id
            """
        ).fetchall()
        chosen = pick_decisions([dict(r) for r in rows], limit)
        for d in chosen:
            d["journal"] = [dict(m) for m in conn.execute(
                """
                SELECT author, kind, body FROM messages
                WHERE thread = %s AND author = 'adrian' AND (%s IS NULL OR id < %s)
                ORDER BY id
                """,
                (d["thread"], d["first_vote"], d["first_vote"]),
            ).fetchall()]
    return chosen


def seat_configs(seat_name: str | None, mixed: bool) -> tuple[dict, str]:
    """({asiento: config}, etiqueta). Con `mixed`, cada asiento con su
    proveedor real; si no, el proveedor de `seat_name` en los tres puestos
    (la persona queda como única variable)."""
    def usable(cfg: dict | None, name: str) -> dict:
        if not cfg or cfg.get("type") == "api" or cfg.get("journal") != "inline":
            raise SystemExit(f"{name} debe ser un asiento CLI inline de heads.json")
        return cfg

    if mixed:
        configs = {seat: usable(heads.seat_by_name(seat), seat) for seat in SEATS}
        label = "mixto: " + ", ".join(f"{s}={configs[s].get('name', '?')}" for s in SEATS)
        return configs, label
    cfg = usable(heads.seat_by_name(seat_name), f"--seat {seat_name!r}")
    return {seat: cfg for seat in SEATS}, f"{seat_name} ({cfg.get('name', '?')}) en los tres asientos"


def scratch_config(seat_cfg: dict, seat: str) -> dict:
    """El experimento vota en un directorio temporal, no en un repo: codex
    exec se niega a arrancar fuera de un directorio confiable sin
    `--skip-git-repo-check` (así murió el primer intento de la corrida
    mixta, con «Not inside a trusted directory»)."""
    config = dict(seat_cfg, seat=seat, args=list(seat_cfg.get("args", [])))
    if "exec" in config["args"] and "--skip-git-repo-check" not in config["args"]:
        config["args"].append("--skip-git-repo-check")
    return config


def _vote(seat_cfg: dict, seat: str, decision: dict, mode: str, cwd: str) -> dict:
    import relay
    persona = personas.system_prompt(seat, mode)
    system, user = apihead.build_api_prompt(seat, decision, decision["journal"])
    # build_api_prompt usa la persona del modo vigente; se sustituye por la del
    # modo pedido para que las tres condiciones corran en la misma corrida.
    system = system.replace(apihead._persona(seat), persona, 1)
    prompt = f"{system}\n\n{user}"
    config = scratch_config(seat_cfg, seat)
    text = relay._run_cli_inline(config, prompt, cwd, seat_cfg.get("timeout_secs", 600))
    try:
        vote = apihead.parse_vote(apihead.strip_echo(text, prompt))
        return {"position": vote["position"], "body": vote["body"], "provider": seat_cfg.get("name")}
    except ValueError as exc:
        return {"position": None, "body": text, "error": str(exc), "provider": seat_cfg.get("name")}


def run(limit: int, seat_name: str, modes: list[str], jobs: int, out: Path,
        resume: bool = False, mixed: bool = False) -> dict:
    configs, label = seat_configs(seat_name, mixed)
    decisions = _load_decisions(limit)
    for d in decisions:
        d["round"] = 1
    tasks = [(d, mode, seat) for d in decisions for mode in modes for seat in SEATS]
    results = []
    out.parent.mkdir(parents=True, exist_ok=True)
    if resume and out.exists():
        # Sólo se re-votan los que quedaron sin voto (p.ej. por límite de
        # sesión del proveedor); los demás se conservan tal cual.
        previous = json.loads(out.read_text(encoding="utf-8")).get("results", [])
        results = [r for r in previous if r.get("position")]
        done = {(r["decision_id"], r["mode"], r["seat"]) for r in results}
        tasks = [(d, mode, seat) for d, mode, seat in tasks if (d["id"], mode, seat) not in done]
        print(f"  reanudando: {len(results)} votos conservados, {len(tasks)} por re-votar", flush=True)

    def save():
        report = {"seat_config": label, "mixed": mixed, "decisions": len(decisions),
                  "modes": metrics(results), "results": results}
        out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        return report

    with tempfile.TemporaryDirectory(prefix="magi-persona-ab-") as cwd, ThreadPoolExecutor(jobs) as pool:
        futures = {pool.submit(_vote, configs[seat], seat, d, mode, cwd): (d, mode, seat)
                   for d, mode, seat in tasks}
        for future, (d, mode, seat) in futures.items():
            try:
                outcome = future.result()
            except Exception as exc:  # un voto roto no tira 71 votos buenos
                outcome = {"position": None, "body": "", "provider": configs[seat].get("name"),
                           "error": f"{type(exc).__name__}: {exc}"[:300]}
            results.append({"decision_id": d["id"], "title": d["title"], "artifact": bool(d.get("artifact")),
                            "mode": mode, "seat": seat, **outcome})
            # ASCII a propósito: la consola de Windows (cp1252) no imprime flechas
            print(f"  [{len(results)}/{len(results) + len(futures) - sum(f.done() for f in futures)}] "
                  f"#{d['id']} {mode:<10} {seat:<9} -> {outcome.get('position') or 'ERROR'}", flush=True)
            save()  # guardado incremental: un crash tarde no pierde lo votado
    return save()


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):  # cp1252 en Windows: nunca morir por un carácter
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--limit", type=int, default=8, help="decisiones a re-votar (mitad con repo, mitad sin)")
    parser.add_argument("--seat", default="casper", help="asiento de heads.json cuyo proveedor ocupa los tres puestos")
    parser.add_argument("--mixed", action="store_true",
                        help="cada asiento con su proveedor real (mide persona + proveedor)")
    parser.add_argument("--modes", default=",".join(personas.MODES))
    parser.add_argument("--jobs", type=int, default=3)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--report", type=Path, help="sólo imprimir el informe de un JSON ya generado")
    parser.add_argument("--resume", action="store_true", help="re-votar sólo lo que quedó sin voto en --out")
    args = parser.parse_args(argv)
    if args.report:
        data = json.loads(args.report.read_text(encoding="utf-8"))
        data["modes"] = metrics(data["results"])
        print(render(data))
        return 0
    modes = [m for m in args.modes.split(",") if m in personas.MODES]
    report = run(args.limit, args.seat, modes, args.jobs, args.out, resume=args.resume, mixed=args.mixed)
    print(render(report))
    print(f"  respuestas crudas en {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
