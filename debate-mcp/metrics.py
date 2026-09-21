#!/usr/bin/env python3
"""Latencia, errores y timeouts del consejo, por asiento y por tipo de turno.

La materia prima ya existía: el relay escribe `logs/trigger_events.jsonl` con
`duration_s`, `rc` y `timed_out` por cada disparo. Nadie lo leía, y el paso 6
del roadmap de memoria (docs/memory-evolution.md) seguía sin "mediciones
continuas de latencia/costo". Esto no mide calidad de razonamiento: mide
cuánto tarda y cuánto falla cada proveedor en cada rol, que es lo que hace
falta para elegir modelos y detectar un asiento degradado.

Uso:
    python metrics.py                 # últimos 7 días
    python metrics.py --days 30
    python metrics.py --json          # para otros scripts

Lee también `trigger_events.jsonl.1` (la generación rotada) si existe.
"""

import argparse
import json
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
EVENTS_PATH = BASE_DIR / "logs" / "trigger_events.jsonl"
CHARS_PER_TOKEN = 4  # aproximación; los CLI no exponen el conteo real


def _percentile(values: list[float], q: float) -> float:
    """Percentil por rango más cercano; no interpola (con n chico interpolar
    inventa valores que ningún turno tuvo)."""
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, round(q * len(ordered) + 0.5) - 1))
    return ordered[idx]


def _parse_ts(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def read_events(path: Path = EVENTS_PATH, since: datetime | None = None) -> list[dict]:
    """Eventos en orden cronológico: la generación rotada primero, luego la
    actual. Las líneas rotas (una escritura cortada a mitad) se saltan."""
    events = []
    for candidate in (path.parent / f"{path.name}.1", path):
        if not candidate.exists():
            continue
        with candidate.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict):
                    continue
                ts = _parse_ts(rec.get("ts"))
                if ts is None or (since is not None and ts < since):
                    continue
                rec["_ts"] = ts
                events.append(rec)
    events.sort(key=lambda e: e["_ts"])
    return events


def summarize(events: list[dict]) -> dict:
    """{'seats': [...], 'rounds': {...}, 'spawn_failures': {...}}.

    - seats: por (asiento, turno): n, p50/p95/max de duración, errores
      (rc≠0), timeouts. Un timeout cuenta también como error.
    - rounds: tiempo de pared de cada ronda de decisión (del primer disparo
      al último cierre de esa ronda), que es lo que espera el operador.
    - spawn_failures: disparos que ni arrancaron, por causa.
    """
    by_seat: dict[tuple, dict] = defaultdict(lambda: {"durations": [], "errors": 0, "timeouts": 0,
                                                       "prompt_chars": [], "output_chars": [], "memory_chars": [],
                                                       "input_tokens": [], "output_tokens": [], "cost_usd": [],
                                                       "retries": 0})
    round_bounds: dict[tuple, dict] = {}
    round_secs: list[float] = []
    spawn_failures: dict[str, int] = defaultdict(int)

    def close_burst(bounds: dict) -> None:
        if bounds["end"] is not None:
            round_secs.append((bounds["end"] - bounds["start"]).total_seconds())

    for e in events:
        kind = e.get("event")
        key = (e.get("decision_id"), e.get("round"))
        if kind == "trigger_spawned" and key[0] is not None:
            bounds = round_bounds.get(key)
            if bounds is None or (bounds["end"] is not None and e["_ts"] > bounds["end"]):
                # Una ronda que ya había cerrado y vuelve a disparar (reintento
                # de una cabeza fallida, un día después) es otra tanda: la pausa
                # humana entre medio no es latencia del consejo.
                if bounds is not None:
                    close_burst(bounds)
                bounds = round_bounds[key] = {"start": e["_ts"], "end": None}
            bounds["start"] = min(bounds["start"], e["_ts"])
        elif kind == "trigger_done":
            seat = by_seat[(e.get("author") or "?", e.get("turn") or "?")]
            seat["durations"].append(float(e.get("duration_s") or 0))
            for field in ("prompt_chars", "output_chars", "memory_chars",
                          "input_tokens", "output_tokens", "cost_usd"):  # no pisar `key` (decisión, ronda)
                if isinstance(e.get(field), (int, float)):
                    seat[field].append(float(e[field]))
            if isinstance(e.get("attempts"), int) and e["attempts"] > 1:
                seat["retries"] += e["attempts"] - 1  # asientos API: 429/5xx reintentados
            if e.get("timed_out"):
                seat["timeouts"] += 1
            if e.get("rc") not in (0, None) or e.get("timed_out"):
                seat["errors"] += 1
            if key[0] is not None and key in round_bounds:
                bounds = round_bounds[key]
                bounds["end"] = max(bounds["end"] or e["_ts"], e["_ts"])
        elif kind == "trigger_spawn_failed":
            spawn_failures[str(e.get("error") or "desconocido")[:80]] += 1

    seats = []
    for (author, turn), data in sorted(by_seat.items(), key=lambda kv: -len(kv[1]["durations"])):
        d = data["durations"]
        # Tokens aproximados (4 caracteres por token en español/código). Lo
        # que la cabeza lee por su cuenta con herramientas no está: sólo el
        # prompt que le mandamos y lo que escribió.
        tokens = {}
        for key, name in (("prompt_chars", "in"), ("output_chars", "out"), ("memory_chars", "memory")):
            values = data[key]
            tokens[f"{name}_tokens_p50"] = round(statistics.median(values) / CHARS_PER_TOKEN) if values else None
            tokens[f"{name}_tokens_total"] = round(sum(values) / CHARS_PER_TOKEN) if values else None
            tokens[f"{name}_measured"] = len(values)
        # Uso real (asientos API): tokens que cobró el proveedor y costo con
        # los precios de heads.json. Los CLI no lo exponen: queda None.
        usage = {
            "usage_in_total": round(sum(data["input_tokens"])) if data["input_tokens"] else None,
            "usage_out_total": round(sum(data["output_tokens"])) if data["output_tokens"] else None,
            "usage_measured": len(data["input_tokens"]),
            "cost_usd_total": round(sum(data["cost_usd"]), 4) if data["cost_usd"] else None,
            "retries": data["retries"],
        }
        seats.append({
            "seat": author, "turn": turn, "n": len(d),
            "p50_s": round(statistics.median(d), 1),
            "p95_s": round(_percentile(d, 0.95), 1),
            "max_s": round(max(d), 1),
            "errors": data["errors"], "timeouts": data["timeouts"],
            "error_rate": round(data["errors"] / len(d), 3),
            **tokens, **usage,
        })
    for bounds in round_bounds.values():
        close_burst(bounds)
    rounds = {
        "n": len(round_secs),
        "p50_s": round(statistics.median(round_secs), 1) if round_secs else None,
        "p95_s": round(_percentile(round_secs, 0.95), 1) if round_secs else None,
        "max_s": round(max(round_secs), 1) if round_secs else None,
    }
    return {"seats": seats, "rounds": rounds, "spawn_failures": dict(spawn_failures)}


def _fmt_num(v) -> str:
    return "-" if v is None else f"{v:,}"


def _fmt_usd(v) -> str:
    return "-" if v is None else f"${v:,.2f}"


QUARANTINE_STREAK = 3
VOTING_TURNS = {"answer", "recast", "cli-inline", "api"}


def quarantined_seats(events: list[dict] | None = None, streak: int = QUARANTINE_STREAK,
                      now: datetime | None = None) -> dict[str, str]:
    """Asientos cuyos últimos `streak` turnos de voto de hoy fallaron todos
    (rc≠0 o timeout): una clave vencida, un proveedor caído, un límite de
    sesión. `board.start_decision` no los sienta en decisiones nuevas hasta
    que un turno salga bien o cambie el día; la decisión se abre degradada
    en vez de colgarse. {asiento: detalle}."""
    now = now or datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if events is None:
        events = read_events(EVENTS_PATH, since=start)
    flags: dict[str, list[bool]] = defaultdict(list)
    for e in events:
        if e.get("event") != "trigger_done" or not e.get("author") or e.get("turn") not in VOTING_TURNS:
            continue
        flags[e["author"]].append(e.get("rc") not in (0, None) or bool(e.get("timed_out")))
    return {seat: f"{streak} turnos de voto fallidos seguidos hoy"
            for seat, failed in flags.items() if len(failed) >= streak and all(failed[-streak:])}


def _fmt_secs(value) -> str:
    if value is None:
        return "-"
    return f"{value:.0f}s" if value < 600 else f"{value / 60:.1f}min"


def render(summary: dict, days: int) -> str:
    lines = [f"[metrics] últimos {days} días"]
    if not summary["seats"]:
        lines.append("  sin turnos registrados en el período")
        return "\n".join(lines)
    lines.append(f"  {'asiento':<10} {'turno':<11} {'n':>4} {'p50':>7} {'p95':>7} {'max':>7} {'err':>5} {'t/o':>4} "
                 f"{'in≈tok':>7} {'out≈tok':>8} {'memoria':>8} {'costo':>8}")
    total_in = total_out = 0
    total_cost = 0.0
    priced = 0
    for s in summary["seats"]:
        total_in += s.get("in_tokens_total") or 0
        total_out += s.get("out_tokens_total") or 0
        if s.get("cost_usd_total") is not None:
            total_cost += s["cost_usd_total"]
            priced += s.get("usage_measured") or 0
        lines.append(
            f"  {s['seat']:<10} {s['turn']:<11} {s['n']:>4} {_fmt_secs(s['p50_s']):>7} "
            f"{_fmt_secs(s['p95_s']):>7} {_fmt_secs(s['max_s']):>7} "
            f"{s['errors']:>3} {s['error_rate'] * 100:>3.0f}% {s['timeouts']:>4} "
            f"{_fmt_num(s.get('in_tokens_p50')):>7} {_fmt_num(s.get('out_tokens_p50')):>8} {_fmt_num(s.get('memory_tokens_p50')):>8} "
            f"{_fmt_usd(s.get('cost_usd_total')):>8}"
        )
    if priced:
        lines.append(f"  costo real en el período: {_fmt_usd(total_cost)} en {priced} turnos API con precio configurado "
                     f"(tokens cobrados por el proveedor; los CLI no exponen uso)")
    if total_in or total_out:
        lines.append(f"  tokens aproximados en el período: {total_in:,} de entrada / {total_out:,} de salida "
                     f"(4 chars/token; no incluye lo que las cabezas leen con sus herramientas)")
    r = summary["rounds"]
    if r["n"]:
        lines.append(
            f"  rondas de decisión: n={r['n']} p50={_fmt_secs(r['p50_s'])} "
            f"p95={_fmt_secs(r['p95_s'])} max={_fmt_secs(r['max_s'])}"
        )
    if summary["spawn_failures"]:
        lines.append("  disparos que no arrancaron:")
        for error, n in sorted(summary["spawn_failures"].items(), key=lambda kv: -kv[1]):
            lines.append(f"    {n:>4}  {error}")
    lines.append("  (mide tiempo y fallos por proveedor, no calidad de las respuestas)")
    return "\n".join(lines)


# ------------------------------------------------------------- calidad

def _count(values) -> dict:
    out: dict = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return out


def quality_summary(decisions: list[dict], outcomes: list[dict], feedback: list[dict],
                    events: list[dict]) -> dict:
    """Lo que el lazo de calidad necesita ver junto: cuántas decisiones
    cerraron y cómo, qué fracción tiene resultado reportado y cuál fue,
    cuánta memoria se calificó y si sirvió, tiempo de pared y tokens por
    decisión, y fallos de cabeza. Todo por decisión, no por turno."""
    closed = [d for d in decisions if d.get("status") == "closed"]
    by_ruling = _count(str(d.get("ruling")) for d in closed)
    production = [d for d in decisions if d.get("production")]
    by_execution = _count(str(d.get("execution_state")) for d in production)
    outcome_by_decision: dict = {}
    for o in sorted(outcomes, key=lambda o: o.get("id") or 0):
        outcome_by_decision[o["decision_id"]] = o.get("status")
    covered = [d for d in closed if d["id"] in outcome_by_decision]
    labels = [f for f in feedback if f.get("decision_id") in {d["id"] for d in decisions}]
    with_memory = [d for d in decisions if d.get("memory_sources")]
    labeled = {f["decision_id"] for f in labels}
    wall = []
    for d in closed:
        if d.get("created_at") and d.get("closed_at"):
            wall.append((d["closed_at"] - d["created_at"]).total_seconds())
    tokens_in: dict = {}
    tokens_out: dict = {}
    failures = 0
    for e in events:
        did = e.get("decision_id")
        if e.get("event") != "trigger_done" or did is None:
            continue
        if isinstance(e.get("prompt_chars"), (int, float)):
            tokens_in[did] = tokens_in.get(did, 0) + e["prompt_chars"] / CHARS_PER_TOKEN
        if isinstance(e.get("output_chars"), (int, float)):
            tokens_out[did] = tokens_out.get(did, 0) + e["output_chars"] / CHARS_PER_TOKEN
        if e.get("rc") not in (0, None) or e.get("timed_out"):
            failures += 1
    med = lambda values: round(statistics.median(values)) if values else None  # noqa: E731
    return {
        "decisions": len(decisions), "closed": len(closed), "by_ruling": by_ruling,
        "production": len(production), "by_execution_state": by_execution,
        "outcomes": {"reported": len(covered), "coverage": round(len(covered) / len(closed), 3) if closed else None,
                     "by_status": _count(outcome_by_decision[d["id"]] for d in covered)},
        "memory": {"with_sources": len(with_memory), "labeled": len(labeled),
                   "coverage": round(len(labeled) / len(with_memory), 3) if with_memory else None,
                   "useful_rate": round(sum(1 for f in labels if f.get("useful")) / len(labels), 3) if labels else None},
        "wall_p50_s": med(wall),
        "tokens_in_p50": med(list(tokens_in.values())), "tokens_out_p50": med(list(tokens_out.values())),
        "head_failures": failures,
    }


def render_quality(q: dict, days: int) -> str:
    lines = [f"[metrics --quality] últimos {days} días: {q['decisions']} decisiones, {q['closed']} cerradas "
             f"{q['by_ruling']}; producción {q['production']} {q['by_execution_state']}"]
    o, m = q["outcomes"], q["memory"]
    lines.append(f"  resultados reportados: {o['reported']}/{q['closed']} ({_pct(o['coverage'])}) {o['by_status']}")
    lines.append(f"  memoria calificada: {m['labeled']}/{m['with_sources']} decisiones con fuentes ({_pct(m['coverage'])}); "
                 f"útil en {_pct(m['useful_rate'])}")
    lines.append(f"  por decisión: pared p50 {_fmt_secs(q['wall_p50_s'])}, tokens≈ {_fmt_num(q['tokens_in_p50'])} in / "
                 f"{_fmt_num(q['tokens_out_p50'])} out; turnos de cabeza fallidos: {q['head_failures']}")
    lines.append("  (objetivos del plan de madurez: ≥50 % con resultado, ≥30 % de memoria calificada)")
    return "\n".join(lines)


def _pct(v) -> str:
    return "-" if v is None else f"{v * 100:.0f}%"


def _load_quality(days: int) -> tuple[list, list, list]:
    """Filas de Postgres para el informe de calidad (import perezoso: el
    resto de metrics.py no necesita la base)."""
    from datetime import datetime as _dt
    from config import connect
    since = _dt.now(timezone.utc) - timedelta(days=days)
    with connect() as conn:
        decisions = [dict(r) for r in conn.execute(
            """SELECT id, status, ruling, production, artifact, created_at, closed_at,
                      minority_report->>'execution_state' AS execution_state,
                      minority_report->'memory_sources' AS memory_sources
               FROM decisions WHERE created_at >= %s ORDER BY id""", (since,)).fetchall()]
        outcomes = [dict(r) for r in conn.execute(
            "SELECT id, decision_id, status FROM decision_outcomes ORDER BY id").fetchall()]
        try:
            feedback = [dict(r) for r in conn.execute(
                "SELECT id, decision_id, useful FROM memory_feedback ORDER BY id").fetchall()]
        except Exception:  # base sin la migración 006
            feedback = []
    return decisions, outcomes, feedback


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--days", type=int, default=7, help="ventana hacia atrás (default 7)")
    parser.add_argument("--json", action="store_true", help="salida JSON")
    parser.add_argument("--quality", action="store_true", help="informe del lazo de calidad (necesita Postgres)")
    parser.add_argument("--notify", action="store_true",
                        help="con --quality: además avisa con una notificación del sistema (panel semanal)")
    parser.add_argument("--events", type=Path, default=EVENTS_PATH, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    if args.quality:
        decisions, outcomes, feedback = _load_quality(args.days)
        q = quality_summary(decisions, outcomes, feedback, read_events(args.events, since))
        text = render_quality(q, args.days)
        print(json.dumps({"days": args.days, **q}, ensure_ascii=False, indent=1, default=str) if args.json
              else text)
        if args.notify:
            # panel semanal agendado (bin/schedule-quality.ps1): el toast lleva
            # las dos líneas que importan; el resto queda en el log
            from healthcheck import notify
            lines = [line.strip() for line in text.split("\n")]
            notify("MAGI · calidad de la semana", " · ".join(lines[1:3]))
        return 0
    summary = summarize(read_events(args.events, since))
    if args.json:
        print(json.dumps({"days": args.days, **summary}, ensure_ascii=False, indent=1))
    else:
        print(render(summary, args.days))
    return 0


if __name__ == "__main__":
    sys.exit(main())
