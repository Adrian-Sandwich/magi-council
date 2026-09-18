"""Robustez de los ingestores del grafo (memory-graph): un ingestor que no
encuentra su fuente de datos tiene que degradar con aviso, no tumbar
`refresh.sh` entero (`set -euo pipefail`) ni reventar por una cita que no
resuelve. Nada acá toca Postgres."""

import json
import os

import ingest_docs
import ingest_kimi


class _SinIndice:
    """CodeIndex de mentira: ningún proyecto indexado, todo cae al nodo
    genérico file:."""

    def resolve_file(self, abs_path):
        return None


# ------------------------------------------------------------ ingest_kimi

def test_load_workspaces_sin_archivo_degrada_a_vacio(monkeypatch, tmp_path, capsys):
    """Sin workspaces.json (kimi-code nunca corrió acá), refresh.sh seguía
    corriendo los demás ingestores: FileNotFoundError acá lo mataba entero."""
    monkeypatch.setattr(ingest_kimi, "KIMI_HOME", tmp_path)
    assert ingest_kimi.load_workspaces() == {}
    assert "workspaces.json" in capsys.readouterr().err


def test_load_workspaces_ilegible_degrada_a_vacio(monkeypatch, tmp_path, capsys):
    (tmp_path / "workspaces.json").write_text("{ no json", encoding="utf-8")
    monkeypatch.setattr(ingest_kimi, "KIMI_HOME", tmp_path)
    assert ingest_kimi.load_workspaces() == {}


def test_load_workspaces_leyendo_el_mapa(monkeypatch, tmp_path):
    (tmp_path / "workspaces.json").write_text(json.dumps({
        "workspaces": {
            "wd_clami": {"root": "/home/adrian/src/ClaMi"},
            "wd_otro": {"root": "/home/adrian/src/otro"},
        }
    }), encoding="utf-8")
    monkeypatch.setattr(ingest_kimi, "KIMI_HOME", tmp_path)
    assert ingest_kimi.load_workspaces() == {
        "wd_clami": "/home/adrian/src/ClaMi",
        "wd_otro": "/home/adrian/src/otro",
    }


# ------------------------------------------------------------ ingest_docs

def test_file_id_resuelve_el_path_directo(tmp_path):
    (tmp_path / "src").mkdir()
    f = tmp_path / "src" / "x.py"
    f.write_text("", encoding="utf-8")
    out = ingest_docs.file_id(_SinIndice(), tmp_path, "src/x.py")
    assert out == f"file:{f.resolve()}"


def test_file_id_busca_por_nombre_en_raices_comunes(tmp_path):
    """El doc cita `deep.py` sin path: aparece en src/. Antes era un rglob
    libre del repo entero por cada cita."""
    (tmp_path / "src").mkdir()
    f = tmp_path / "src" / "deep.py"
    f.write_text("", encoding="utf-8")
    out = ingest_docs.file_id(_SinIndice(), tmp_path, "deep.py")
    assert out == f"file:{f.resolve()}"


def test_file_id_con_caminado_capeado_no_explota(tmp_path, monkeypatch):
    """Una cita que no existe en ningún lado recorre el repo con tope de
    directorios en vez de un rglob sin fin; devuelve el candidato sin
    resolver, como antes."""
    for i in range(6):
        (tmp_path / f"dir{i}").mkdir()
    monkeypatch.setattr(ingest_docs, "_RGLOB_MAX_DIRS", 3)
    out = ingest_docs.file_id(_SinIndice(), tmp_path, "no_existe.py")
    assert out == f"file:{tmp_path / 'no_existe.py'}"


# ------------------------------------------------------------ export_kgraph

def test_export_sin_visor_no_tumba_el_refresh(tmp_path, allow_real_processes):
    """Sin Node_visualizer en la máquina, export_kgraph salía con código 1 y
    refresh.sh (set -e) terminaba en error en cada corrida horaria: la tarea
    programada quedaba siempre 'fallida' y tapaba fallos reales. El visor es
    opcional: se avisa y se sale limpio."""
    import subprocess
    import sys
    from pathlib import Path

    script = Path(__file__).resolve().parent.parent / "memory-graph" / "export_kgraph.py"
    env = dict(os.environ, NODE_VISUALIZER_DIR=str(tmp_path / "no-existe"),
               MEMORY_GRAPH_DB=str(tmp_path / "memory.db"))
    proc = subprocess.run([sys.executable, str(script)], env=env, cwd=str(script.parent),
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert "omitido" in proc.stderr
