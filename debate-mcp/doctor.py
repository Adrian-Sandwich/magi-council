#!/usr/bin/env python3
"""¿Esta máquina puede correr MAGI? Cada problema con su línea de remedio.

`healthcheck.py` vigila el sistema EN MARCHA (Postgres caído, relay
congelado, grafo viejo). Esto es lo otro: la instalación. Qué falta para que
el consejo arranque de cero — el venv, la base, el esquema, los binarios de
las cabezas, sus credenciales, el modelo semántico, los permisos de
escritura, las tareas agendadas. Nadie sabía por qué "no arrancaba" sin leer
tres logs; acá cada línea dice qué hacer.

Uso:
    python doctor.py            # reporte legible; 0 = todo bien
    python doctor.py --json     # para la UI y otros scripts
    python doctor.py --quiet    # sólo imprime si algo está mal

Códigos de salida: 0 todo bien, 1 hay avisos, 2 hay algo crítico.
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

BASE_DIR = Path(__file__).resolve().parent
ROOT = BASE_DIR.parent
LOG_DIR = BASE_DIR / "logs"
MEMORY_DB = ROOT / "memory-graph" / "memory.db"
MODEL_CACHE = ROOT / "memory-graph" / "models"
MIN_PYTHON = (3, 12)

OK, WARN, CRIT = "ok", "warn", "crit"
_RANK = {OK: 0, WARN: 1, CRIT: 2}
ICON = {OK: "ok  ", WARN: "WARN", CRIT: "CRIT"}

# Dónde guarda sus credenciales cada CLI conocido. Un binario presente pero
# sin login produce turnos en ERROR que parecen bugs del relay.
CLI_CREDENTIALS = {
    "claude": ("~/.claude/.credentials.json", "~/.claude.json"),
    "codex": ("~/.codex/auth.json",),
    "kimi": ("~/.kimi-code/credentials", "~/.kimi-code/config.toml"),
}


def _exists(*candidates: str) -> bool:
    return any(Path(c).expanduser().exists() for c in candidates)


def check_python() -> tuple[str, str, str]:
    version = ".".join(str(n) for n in sys.version_info[:3])
    if sys.version_info[:2] < MIN_PYTHON:
        need = ".".join(str(n) for n in MIN_PYTHON)
        return CRIT, f"Python {version} es viejo (hace falta {need}+)", \
            f"instalá Python {need}+ y recreá el venv: python -m venv debate-mcp/.venv"
    running_venv = Path(sys.prefix) != Path(sys.base_prefix)
    if not running_venv:
        return WARN, f"Python {version} fuera del venv", \
            "corré con debate-mcp/.venv/Scripts/python.exe (Windows) o .venv/bin/python"
    return OK, f"Python {version} en el venv", ""


def check_dependencies() -> tuple[str, str, str]:
    missing = []
    for module in ("psycopg", "mcp"):
        try:
            __import__(module)
        except ImportError:
            missing.append(module)
    if missing:
        return CRIT, f"faltan dependencias: {', '.join(missing)}", \
            "instalalas: .venv/Scripts/pip.exe install -r debate-mcp/requirements.txt"
    return OK, "psycopg y mcp instalados", ""


def check_postgres() -> tuple[str, str, str]:
    try:
        from config import CONNINFO, connect
    except ImportError as exc:
        return CRIT, f"no puedo importar config.py: {exc}", "corré doctor.py desde el venv del repo"
    try:
        with connect() as conn:
            applied = conn.execute(
                "SELECT max(version) AS v FROM schema_version").fetchone()["v"] or 0
    except Exception as exc:
        first = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
        if "schema_version" in str(exc):
            return CRIT, f"la base responde pero no tiene esquema ({first})", \
                "aplicá las migraciones: .venv/Scripts/python.exe debate-mcp/schema/migrate.py"
        return CRIT, f"Postgres inalcanzable en «{CONNINFO}»: {first}", \
            "arrancá Postgres (en Windows lo hace bin/start-magi.ps1) o exportá DEBATE_CONNINFO"
    pending = [p.name for p in sorted((BASE_DIR / "schema").glob("*.sql"))
               if p.name[:3].isdigit() and int(p.name[:3]) > applied]
    if pending:
        return WARN, f"esquema en v{applied}; faltan {len(pending)} migraciones ({pending[0]}…)", \
            "aplicalas: .venv/Scripts/python.exe debate-mcp/schema/migrate.py"
    return OK, f"Postgres responde; esquema v{applied}", ""


def check_writable() -> tuple[str, str, str]:
    blocked = []
    for path in (LOG_DIR, MEMORY_DB.parent):
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".doctor-write-test"
            probe.write_text("ok", encoding="ascii")
            probe.unlink()
        except OSError as exc:
            blocked.append(f"{path.name} ({exc.strerror or exc})")
    if blocked:
        return CRIT, f"no puedo escribir en: {', '.join(blocked)}", \
            "dale permiso de escritura al usuario que corre MAGI, o movés el repo fuera de una carpeta protegida"
    return OK, "logs y memory-graph se pueden escribir", ""


def check_seats() -> tuple[str, str, str]:
    try:
        import apihead
        import heads
    except ImportError as exc:
        return CRIT, f"no puedo leer el registry de asientos: {exc}", "corré doctor.py desde el venv del repo"
    try:
        registry = heads.load()
    except ValueError as exc:
        return CRIT, f"heads.json inválido: {exc}", "arreglá el JSON o copiá debate-mcp/heads.example.json"
    problems, live = [], []
    for seat in registry:
        name = seat["seat"]
        if seat.get("type") == "api":
            ok, why = apihead.is_configured(seat)
            if ok:
                live.append(f"{name} (api)")
            else:
                problems.append(f"{name}: {why} — exportá la variable o ajustá heads.json")
            continue
        binary = seat.get("bin")
        if not binary or not Path(binary).exists():
            problems.append(f"{name}: sin binario en {binary or '(vacío)'} — instalá el CLI o corregí `bin` en heads.json")
            continue
        stem = Path(binary).stem.lower()
        known = next((k for k in CLI_CREDENTIALS if k in stem), None)
        if known and not _exists(*CLI_CREDENTIALS[known]):
            problems.append(f"{name}: {known} sin sesión iniciada — corré `{known}` una vez y autenticá")
            continue
        live.append(f"{name} ({Path(binary).stem})")
    if not live:
        return CRIT, "ningún asiento utilizable: " + "; ".join(problems), \
            "configurá al menos una cabeza en debate-mcp/heads.json (ver heads.example.json)"
    if problems:
        return WARN, f"{len(live)} asiento(s) listos ({', '.join(live)}); " + "; ".join(problems), \
            "el consejo abre decisiones degradadas hasta que estén los tres"
    return OK, f"{len(live)} asiento(s) listos: {', '.join(live)}", ""


def check_semantic() -> tuple[str, str, str]:
    if os.environ.get("MEMORY_SEMANTIC") == "0":
        return OK, "memoria semántica apagada por MEMORY_SEMANTIC=0 (búsqueda léxica)", ""
    try:
        import fastembed  # noqa: F401
    except ImportError:
        return WARN, "sin fastembed: la memoria del consejo usa sólo búsqueda léxica", \
            "opcional: .venv/Scripts/pip.exe install fastembed  (o exportá MEMORY_SEMANTIC=0 para silenciar)"
    if not MODEL_CACHE.exists() or not any(MODEL_CACHE.glob("models--*")):
        return WARN, "fastembed instalado pero el modelo no está descargado", \
            "la primera búsqueda lo baja sola (~120 MB); o corré memory-graph/refresh.sh"
    return OK, "memoria semántica lista (modelo en caché local)", ""


def check_graph() -> tuple[str, str, str]:
    if not MEMORY_DB.exists():
        return WARN, "memory.db no existe: el consejo no tiene memoria todavía", \
            "construila: bash memory-graph/refresh.sh (o dejá que la tarea agendada lo haga)"
    size_mb = MEMORY_DB.stat().st_size / 1e6
    return OK, f"grafo de memoria presente ({size_mb:.1f} MB)", ""


def check_schedules() -> tuple[str, str, str]:
    """Lo que mantiene a MAGI vivo sin que nadie mire: arranque al iniciar
    sesión, healthcheck, refresh del grafo, panel semanal."""
    if sys.platform != "win32":
        return OK, "agendado fuera de Windows: cron o launchd (ver docs/operacion.md)", ""
    import subprocess
    wanted = {
        "ClaMi-healthcheck": "powershell -ExecutionPolicy Bypass -File debate-mcp\\bin\\schedule-healthcheck.ps1",
        "ClaMi-memory-refresh": "ver docs/operacion.md (refresh del grafo de memoria)",
        "ClaMi-quality": "powershell -ExecutionPolicy Bypass -File debate-mcp\\bin\\schedule-quality.ps1",
    }
    missing = []
    for task, remedy in wanted.items():
        try:
            rc = subprocess.run(["schtasks", "/query", "/tn", task],
                                capture_output=True, timeout=20).returncode
        except (OSError, subprocess.SubprocessError):
            return WARN, "no pude consultar las tareas programadas", "revisalas a mano en el Programador de tareas"
        if rc != 0:
            missing.append(f"{task} ({remedy})")
    startup = Path(os.environ.get("APPDATA", "~")).expanduser() / \
        "Microsoft/Windows/Start Menu/Programs/Startup/ClaMi-magi.cmd"
    if not startup.exists():
        missing.append("arranque al iniciar sesión (powershell -ExecutionPolicy Bypass -File debate-mcp\\bin\\schedule-magi.ps1)")
    if missing:
        return WARN, "sin agendar: " + "; ".join(missing), "instalá lo que falte con los comandos de arriba"
    return OK, "arranque, healthcheck, refresh y panel semanal agendados", ""


CHECKS = [
    ("python", check_python),
    ("dependencias", check_dependencies),
    ("postgres", check_postgres),
    ("escritura", check_writable),
    ("asientos", check_seats),
    ("semantica", check_semantic),
    ("memoria", check_graph),
    ("agendado", check_schedules),
]


def run(checks=None) -> dict:
    results = []
    worst = OK
    for name, fn in (checks or CHECKS):
        try:
            status, detail, remedy = fn()
        except Exception as exc:  # un chequeo roto no puede tumbar el reporte
            status, detail, remedy = CRIT, f"el chequeo falló: {exc!r}", "reportá este error"
        results.append({"check": name, "status": status, "detail": detail, "remedy": remedy})
        if _RANK[status] > _RANK[worst]:
            worst = status
    return {"status": worst, "checks": results}


def render(report: dict) -> str:
    lines = ["[doctor] ¿puede correr MAGI en esta máquina?"]
    for r in report["checks"]:
        lines.append(f"  {ICON[r['status']]} {r['check']:<13} {r['detail']}")
        if r["remedy"] and r["status"] != OK:
            lines.append(f"       ↳ {r['remedy']}")
    verdict = {OK: "todo listo: arrancá con «Iniciar MAGI.bat» o bin/start-magi.ps1",
               WARN: "funciona, pero hay cosas a medias (ver ↳)",
               CRIT: "no va a arrancar hasta resolver lo marcado CRIT"}[report["status"]]
    lines.append(f"  → {verdict}")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--json", action="store_true", help="salida JSON")
    parser.add_argument("--quiet", action="store_true", help="no imprimir nada si todo está bien")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    report = run()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    elif not (args.quiet and report["status"] == OK):
        print(render(report))
    return _RANK[report["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
