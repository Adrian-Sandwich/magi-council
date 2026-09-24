#!/usr/bin/env python3
"""Respaldo del tablero: lo único irreemplazable del sistema.

El grafo de memoria se reconstruye con `memory-graph/refresh.sh` y los
worktrees del ejecutor son temporales. Las decisiones, el journal, los votos
y los resultados viven sólo en Postgres. El 2026-09-24 un apagón sucio dejó
cuatro archivos en ceros — entre ellos la configuración de codex, que quedó
inservible; la base se salvó por suerte y no había respaldo.

Un respaldo que no se verifica es una falsa calma: acá el volcado se
comprime, se vuelve a leer y se exige que contenga las tablas del tablero.
Si no, se borra y el comando falla ruidoso.

    python backup.py                 # respalda y rota
    python backup.py --check         # sólo informa la antigüedad del último
    python backup.py --list
    python backup.py --keep 30

Códigos de salida: 0 bien, 1 aviso (no hay respaldo reciente), 2 falló.
"""

import argparse
import gzip
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

BASE_DIR = Path(__file__).resolve().parent
ROOT = BASE_DIR.parent
BACKUP_DIR = Path(os.environ.get("DEBATE_BACKUP_DIR") or (BASE_DIR / "backups"))
PORTABLE_BIN = ROOT / "experiments" / "pg" / "pgsql" / "bin"
KEEP = 14
STALE_HOURS = 48
# Lo que tiene que aparecer en el volcado para considerarlo un respaldo del
# tablero y no un archivo vacío con cabecera.
REQUIRED = ("PostgreSQL database dump", "CREATE TABLE public.decisions", "CREATE TABLE public.messages")
PREFIX = "debate-"
SUFFIX = ".sql.gz"


def find_pg_dump(explicit: str | None = None) -> str:
    """El binario del volcado: variable de entorno, el Postgres portátil del
    repo (Windows) o el del PATH."""
    if explicit:
        return explicit
    env = os.environ.get("PG_DUMP")
    if env:
        return env
    portable = PORTABLE_BIN / ("pg_dump.exe" if os.name == "nt" else "pg_dump")
    if portable.exists():
        return str(portable)
    found = shutil.which("pg_dump")
    if not found:
        raise FileNotFoundError(
            "no encuentro pg_dump: instalá las herramientas de Postgres o exportá PG_DUMP con su ruta")
    return found


def backup_name(now: datetime | None = None) -> str:
    return f"{PREFIX}{(now or datetime.now()).strftime('%Y%m%d-%H%M%S')}{SUFFIX}"


def backups(directory: Path | None = None) -> list[Path]:
    """Del más nuevo al más viejo; el nombre ordena por fecha."""
    directory = directory or BACKUP_DIR
    if not directory.exists():
        return []
    return sorted((p for p in directory.glob(f"{PREFIX}*{SUFFIX}") if p.is_file()), reverse=True)


def latest(directory: Path | None = None) -> Path | None:
    found = backups(directory)
    return found[0] if found else None


def age_hours(path: Path, now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    return (now - modified).total_seconds() / 3600


def verify(path: Path) -> tuple[bool, str]:
    """Un .sql.gz sirve si se descomprime y trae las tablas del tablero."""
    try:
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
            head = f.read(2_000_000)
    except OSError as exc:
        return False, f"no se puede leer: {exc}"
    missing = [needle for needle in REQUIRED if needle not in head]
    if missing:
        return False, f"el volcado no contiene {missing[0]!r} (¿base vacía o volcado cortado?)"
    return True, f"{path.stat().st_size / 1e6:.1f} MB comprimido"


def rotate(keep: int = KEEP, directory: Path | None = None) -> list[Path]:
    """Deja los `keep` más nuevos y borra el resto. Devuelve lo borrado."""
    removed = []
    for old in backups(directory)[keep:]:
        old.unlink()
        removed.append(old)
    return removed


def dump(conninfo: str, destination: Path, pg_dump: str | None = None, timeout: int = 900) -> Path:
    """Volcado comprimido. pg_dump escribe SQL por stdout y acá se gzipea al
    vuelo: sin archivo intermedio que se quede a medias."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    command = [pg_dump or find_pg_dump(), "--dbname", conninfo, "--no-owner", "--no-privileges"]
    try:
        with partial.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb") as gz:
            proc = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
            gz.write(proc.stdout)
        if proc.returncode != 0:
            detail = (proc.stderr or b"").decode("utf-8", errors="replace").strip().splitlines()
            raise RuntimeError(f"pg_dump salió {proc.returncode}: {detail[-1] if detail else 'sin detalle'}")
        partial.replace(destination)
    finally:
        partial.unlink(missing_ok=True)
    return destination


def run(conninfo: str, directory: Path | None = None, keep: int = KEEP,
        pg_dump: str | None = None) -> dict:
    directory = directory or BACKUP_DIR
    destination = directory / backup_name()
    dump(conninfo, destination, pg_dump)
    ok, detail = verify(destination)
    if not ok:
        destination.unlink(missing_ok=True)
        raise RuntimeError(f"respaldo inválido, se descarta: {detail}")
    removed = rotate(keep, directory)
    return {"path": destination, "detail": detail, "removed": [p.name for p in removed],
            "kept": len(backups(directory))}


def check(directory: Path | None = None, stale_hours: float = STALE_HOURS) -> tuple[str, str, str]:
    """(estado, detalle, remedio) para doctor.py."""
    newest = latest(directory)
    remedy = ("respaldá ahora: .venv/Scripts/python.exe debate-mcp/backup.py; "
              "agendalo con bin/schedule-backup.ps1 (ver docs/operacion.md#respaldo)")
    if newest is None:
        return "warn", f"sin respaldos en {(directory or BACKUP_DIR)}", remedy
    hours = age_hours(newest)
    size = newest.stat().st_size / 1e6
    if hours > stale_hours:
        return "warn", f"el último respaldo ({newest.name}) tiene {hours / 24:.1f} días", remedy
    return "ok", f"último respaldo hace {hours:.1f} h ({newest.name}, {size:.1f} MB); {len(backups(directory))} guardados", ""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dir", type=Path, default=BACKUP_DIR)
    parser.add_argument("--keep", type=int, default=KEEP, help=f"respaldos a conservar (default {KEEP})")
    parser.add_argument("--check", action="store_true", help="sólo informar la antigüedad del último")
    parser.add_argument("--list", action="store_true", help="listar los respaldos")
    parser.add_argument("--pg-dump", help="ruta del binario pg_dump")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    if args.list:
        found = backups(args.dir)
        print(f"[backup] {len(found)} respaldos en {args.dir}")
        for path in found:
            print(f"  {path.name:<30} {path.stat().st_size / 1e6:>7.1f} MB  hace {age_hours(path):.1f} h")
        return 0
    if args.check:
        status, detail, remedy = check(args.dir)
        print(f"[backup] {status}: {detail}")
        if remedy:
            print(f"  ↳ {remedy}")
        return 0 if status == "ok" else 1

    from config import CONNINFO
    try:
        result = run(CONNINFO, args.dir, args.keep, args.pg_dump)
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print(f"[backup] FALLÓ: {exc}", file=sys.stderr)
        return 2
    print(f"[backup] {result['path'].name} ({result['detail']}); {result['kept']} guardados"
          + (f"; borrados {len(result['removed'])}" if result["removed"] else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
