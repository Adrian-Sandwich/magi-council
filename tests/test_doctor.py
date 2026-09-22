"""doctor.py responde «¿puede correr MAGI acá?» y, sobre todo, qué hacer
cuando no. Cada chequeo se prueba con el sistema roto a propósito: lo que
importa es que el remedio salga escrito, no que el mensaje sea bonito."""

import json
from pathlib import Path

import pytest

import doctor


def test_asientos_sin_binario_sin_sesion_y_api_sin_clave(monkeypatch, tmp_path):
    import apihead
    import heads
    binario = tmp_path / "claude.exe"
    binario.write_text("#", encoding="ascii")
    monkeypatch.setattr(heads, "load", lambda: [
        {"seat": "melchior", "type": "cli", "bin": str(tmp_path / "no-existe.exe")},
        {"seat": "balthasar", "type": "cli", "bin": str(binario)},
        {"seat": "casper", "type": "api", "provider": "anthropic", "model": "claude-opus-4-1"},
    ])
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(doctor, "CLI_CREDENTIALS", {"claude": (str(tmp_path / "sin-login.json"),)})
    status, detail, remedy = doctor.check_seats()
    assert status == doctor.CRIT, "sin un solo asiento vivo, MAGI no delibera"
    assert "melchior: sin binario" in detail
    assert "balthasar: claude sin sesión iniciada" in detail
    assert "casper: falta la variable ANTHROPIC_API_KEY" in detail
    assert "heads.json" in remedy

    # con una cabeza viva el resto es aviso, no bloqueo
    monkeypatch.setattr(doctor, "CLI_CREDENTIALS", {"claude": (str(binario),)})
    status, detail, _ = doctor.check_seats()
    assert status == doctor.WARN and "1 asiento(s) listos (balthasar (claude))" in detail
    assert apihead.is_configured({"seat": "casper", "type": "api", "provider": "anthropic",
                                  "model": "x"})[0] is False


def test_postgres_caido_esquema_ausente_y_migraciones_pendientes(monkeypatch):
    import config

    def caido():
        raise OSError("connection refused\nsegunda línea")

    monkeypatch.setattr(config, "connect", caido)
    status, detail, remedy = doctor.check_postgres()
    assert status == doctor.CRIT and "connection refused" in detail and "segunda línea" not in detail
    assert "start-magi" in remedy or "DEBATE_CONNINFO" in remedy

    def sin_esquema():
        raise RuntimeError('relation "schema_version" does not exist')

    monkeypatch.setattr(config, "connect", sin_esquema)
    status, _, remedy = doctor.check_postgres()
    assert status == doctor.CRIT and "migrate.py" in remedy

    class Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a): return self
        def fetchone(self): return {"v": 1}

    monkeypatch.setattr(config, "connect", lambda: Conn())
    status, detail, remedy = doctor.check_postgres()
    assert status == doctor.WARN and "faltan" in detail and "migrate.py" in remedy


def test_escritura_bloqueada_es_critica(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(doctor, "MEMORY_DB", tmp_path / "graph" / "memory.db")
    assert doctor.check_writable()[0] == doctor.OK

    def negado(self, *a, **kw):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "write_text", negado)
    status, detail, remedy = doctor.check_writable()
    assert status == doctor.CRIT and "logs" in detail and "permiso" in remedy


def test_semantica_y_grafo_son_avisos_no_bloqueos(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMORY_SEMANTIC", "0")
    assert doctor.check_semantic()[0] == doctor.OK
    monkeypatch.delenv("MEMORY_SEMANTIC")
    monkeypatch.setattr(doctor, "MODEL_CACHE", tmp_path / "vacio")
    status, _, remedy = doctor.check_semantic()
    assert status == doctor.WARN and ("fastembed" in remedy or "refresh" in remedy)

    monkeypatch.setattr(doctor, "MEMORY_DB", tmp_path / "no-existe.db")
    status, detail, remedy = doctor.check_graph()
    assert status == doctor.WARN and "refresh.sh" in remedy
    db = tmp_path / "memory.db"
    db.write_bytes(b"x" * 2_000_000)
    monkeypatch.setattr(doctor, "MEMORY_DB", db)
    assert doctor.check_graph()[0] == doctor.OK


def test_reporte_render_json_y_codigo_de_salida(capsys, monkeypatch):
    checks = [("uno", lambda: (doctor.OK, "todo bien", "")),
              ("dos", lambda: (doctor.WARN, "a medias", "hacé esto")),
              ("roto", lambda: (_ for _ in ()).throw(ValueError("boom")))]
    report = doctor.run(checks)
    assert report["status"] == doctor.CRIT, "un chequeo que revienta es crítico, no silencio"
    assert report["checks"][2]["detail"].startswith("el chequeo falló")
    text = doctor.render(report)
    assert "hacé esto" in text and "todo bien" in text and "CRIT" in text
    assert "↳ " in text and text.count("↳") == 2, "sólo los no-ok llevan remedio"

    monkeypatch.setattr(doctor, "CHECKS", checks[:1])
    assert doctor.main([]) == 0
    assert "todo listo" in capsys.readouterr().out
    assert doctor.main(["--quiet"]) == 0 and capsys.readouterr().out == ""
    assert doctor.main(["--json"]) == 0
    assert json.loads(capsys.readouterr().out)["checks"][0]["check"] == "uno"
    monkeypatch.setattr(doctor, "CHECKS", checks[1:2])
    assert doctor.main(["--quiet"]) == 1, "con avisos imprime aunque sea quiet"
    assert "a medias" in capsys.readouterr().out


@pytest.mark.parametrize("faltante", ["ClaMi-healthcheck", None])
def test_agendado_windows_avisa_lo_que_falta(monkeypatch, faltante, tmp_path):
    import subprocess
    monkeypatch.setattr(doctor.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    startup = tmp_path / "Microsoft/Windows/Start Menu/Programs/Startup"
    startup.mkdir(parents=True)
    (startup / "ClaMi-magi.cmd").write_text("@echo off", encoding="ascii")

    class Result:
        def __init__(self, rc): self.returncode = rc

    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **kw: Result(1 if faltante and cmd[-1] == faltante else 0))
    status, detail, _ = doctor.check_schedules()
    if faltante:
        assert status == doctor.WARN and faltante in detail and "schedule-healthcheck" in detail
    else:
        assert status == doctor.OK and "agendados" in detail
