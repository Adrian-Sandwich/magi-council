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
                                                       "prompt_chars": [], "output_chars": [], "memory_chars": []})
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
            for field in ("prompt_chars", "output_chars", "memory_chars"):  # no pisar `key` (decisión, ronda)
                if isinstance(e.get(field), (int, float)):
                    seat[field].append(float(e[field]))
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
        seats.append({
            "seat": author, "turn": turn, "n": len(d),
            "p50_s": round(statistics.median(d), 1),
            "p95_s": round(_percentile(d, 0.95), 1),
            "max_s": round(max(d), 1),
            "errors": data["errors"], "timeouts": data["timeouts"],
            "error_rate": round(data["errors"] / len(d), 3),
            **tokens,
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
                 f"{'in≈tok':>7} {'out≈tok':>8} {'memoria':>8}")
    total_in = total_out = 0
    for s in summary["seats"]:
        total_in += s.get("in_tokens_total") or 0
        total_out += s.get("out_tokens_total") or 0
        lines.append(
            f"  {s['seat']:<10} {s['turn']:<11} {s['n']:>4} {_fmt_secs(s['p50_s']):>7} "
            f"{_fmt_secs(s['p95_s']):>7} {_fmt_secs(s['max_s']):>7} "
            f"{s['errors']:>3} {s['error_rate'] * 100:>3.0f}% {s['timeouts']:>4} "
            f"{_fmt_num(s.get('in_tokens_p50')):>7} {_fmt_num(s.get('out_tokens_p50')):>8} {_fmt_num(s.get('memory_tokens_p50')):>8}"
        )
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--days", type=int, default=7, help="ventana hacia atrás (default 7)")
    parser.add_argument("--json", action="store_true", help="salida JSON")
    parser.add_argument("--events", type=Path, default=EVENTS_PATH, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    summary = summarize(read_events(args.events, since))
    if args.json:
        print(json.dumps({"days": args.days, **summary}, ensure_ascii=False, indent=1))
    else:
        print(render(summary, args.days))
    return 0


if __name__ == "__main__":
    sys.exit(main())
