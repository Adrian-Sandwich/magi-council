"""El respaldo del tablero. Un volcado que no se verifica es falsa calma:
lo que se prueba acá es que un respaldo vacío o cortado falle ruidoso, que la
rotación deje lo que promete y que el diagnóstico sepa cuándo está viejo.
Nada de esto toca Postgres: pg_dump se reemplaza por un script."""

import gzip
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import backup

VOLCADO = ("--\n-- PostgreSQL database dump\n--\n"
           "CREATE TABLE public.decisions (id bigint);\n"
           "CREATE TABLE public.messages (id bigint);\n"
           "COPY public.decisions (id) FROM stdin;\n1\n\\.\n")


def _fake_pg_dump(tmp_path: Path, salida: str = VOLCADO, rc: int = 0, stderr: str = "") -> str:
    """Un pg_dump de mentira: imprime lo que le digan y sale con el código
    pedido. El binario real no se toca en los tests."""
    script = tmp_path / "fake_pg_dump.py"
    script.write_text(
        "import sys\n"
        f"sys.stdout.buffer.write({salida!r}.encode('utf-8'))\n"
        f"sys.stderr.write({stderr!r})\n"
        f"raise SystemExit({rc})\n", encoding="utf-8")
    return f"{sys.executable}|{script}"


class _Runner:
    """pg_dump es una lista [python, script]; subprocess.run recibe el binario
    como un solo string, así que acá se parte."""

    def __init__(self, monkeypatch):
        real = subprocess.run
        monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: real(
            [*cmd[0].split("|"), *cmd[1:]] if "|" in cmd[0] else cmd, **kw))


def test_respalda_verifica_y_rota(tmp_path, monkeypatch, allow_real_processes):
    _Runner(monkeypatch)
    directory = tmp_path / "backups"
    result = backup.run("dbname=debate", directory, keep=2, pg_dump=_fake_pg_dump(tmp_path))
    assert result["path"].exists() and result["path"].suffix == ".gz"
    assert result["path"].name.startswith("debate-") and result["kept"] == 1
    with gzip.open(result["path"], "rt", encoding="utf-8") as f:
        assert "CREATE TABLE public.decisions" in f.read()
    assert not list(directory.glob("*.part")), "sin archivos a medias"

    # rotación: se conservan los dos más nuevos
    for stamp in ("debate-20260101-000000.sql.gz", "debate-20260102-000000.sql.gz"):
        (directory / stamp).write_bytes(b"viejo")
    removed = backup.rotate(keep=2, directory=directory)
    assert [p.name for p in removed] == ["debate-20260101-000000.sql.gz"]
    assert len(backup.backups(directory)) == 2
    assert backup.latest(directory).name == result["path"].name, "el más nuevo primero"


def test_un_volcado_vacio_o_cortado_no_pasa_por_respaldo(tmp_path, monkeypatch, allow_real_processes):
    """El caso que importa: pg_dump 'funciona' pero devuelve una cabecera sin
    tablas. Guardarlo sería peor que no tener respaldo."""
    _Runner(monkeypatch)
    directory = tmp_path / "backups"
    cortado = "--\n-- PostgreSQL database dump\n--\n"
    with pytest.raises(RuntimeError, match="respaldo inválido"):
        backup.run("dbname=debate", directory, pg_dump=_fake_pg_dump(tmp_path, cortado))
    assert backup.backups(directory) == [], "el archivo inválido se descarta"

    with pytest.raises(RuntimeError, match="pg_dump salió 1"):
        backup.run("dbname=debate", directory,
                   pg_dump=_fake_pg_dump(tmp_path, "", rc=1, stderr="FATAL: no existe la base"))
    assert backup.backups(directory) == []


def test_verify_detecta_archivo_ilegible(tmp_path):
    roto = tmp_path / "debate-20260101-000000.sql.gz"
    roto.write_bytes(b"esto no es gzip")
    ok, detail = backup.verify(roto)
    assert not ok and "no se puede leer" in detail


def test_check_avisa_cuando_no_hay_respaldo_o_esta_viejo(tmp_path):
    status, detail, remedy = backup.check(tmp_path)
    assert status == "warn" and "sin respaldos" in detail and "schedule-backup" in remedy

    viejo = tmp_path / "debate-20260101-000000.sql.gz"
    with gzip.open(viejo, "wt", encoding="utf-8") as f:
        f.write(VOLCADO)
    hace_tres_dias = (datetime.now(timezone.utc) - timedelta(days=3)).timestamp()
    import os
    os.utime(viejo, (hace_tres_dias, hace_tres_dias))
    status, detail, _ = backup.check(tmp_path)
    assert status == "warn" and "3.0 días" in detail

    nuevo = tmp_path / "debate-20260102-000000.sql.gz"
    with gzip.open(nuevo, "wt", encoding="utf-8") as f:
        f.write(VOLCADO)
    status, detail, remedy = backup.check(tmp_path)
    assert status == "ok" and "2 guardados" in detail and remedy == ""


def test_find_pg_dump_prefiere_la_variable_y_explica_si_no_hay(monkeypatch, tmp_path):
    monkeypatch.setenv("PG_DUMP", "/ruta/pg_dump")
    assert backup.find_pg_dump() == "/ruta/pg_dump"
    assert backup.find_pg_dump("/otro") == "/otro", "el argumento manda sobre la variable"
    monkeypatch.delenv("PG_DUMP")
    monkeypatch.setattr(backup, "PORTABLE_BIN", tmp_path / "no-existe")
    monkeypatch.setattr(backup.shutil, "which", lambda name: None)
    with pytest.raises(FileNotFoundError, match="PG_DUMP"):
        backup.find_pg_dump()


def test_main_list_y_check_no_necesitan_postgres(tmp_path, capsys):
    assert backup.main(["--list", "--dir", str(tmp_path)]) == 0
    assert "0 respaldos" in capsys.readouterr().out
    assert backup.main(["--check", "--dir", str(tmp_path)]) == 1, "sin respaldo, salida 1"
    salida = capsys.readouterr().out
    assert "sin respaldos" in salida and "↳" in salida
