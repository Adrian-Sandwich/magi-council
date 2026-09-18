"""Los dos paquetes son scripts sueltos, no paquetes instalables: se importan
por ruta. `settings` y `db` leen configuración de variables de entorno, así que
todos los tests que tocan disco apuntan a un tmp_path — nunca a la memory.db
real ni al Postgres real.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

REAL_POPEN = subprocess.Popen

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"

sys.path.insert(0, str(REPO_ROOT / "memory-graph"))
sys.path.insert(0, str(REPO_ROOT / "debate-mcp"))


@pytest.fixture
def graph_db(tmp_path, monkeypatch):
    """Una memory.db vacía y descartable."""
    import db as db_module
    import settings

    path = tmp_path / "memory.db"
    monkeypatch.setattr(settings, "DB_PATH", path)
    monkeypatch.setattr(db_module, "DB_PATH", path)
    conn = db_module.connect(path)
    yield conn
    conn.close()


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def real_claude_logs() -> Path:
    """Los transcripts de verdad. Los tests que los usan son el canario de
    formato: si Claude Code cambia el shape del .jsonl, fallan acá."""
    import settings

    path = settings.CLAUDE_PROJECTS_DIR
    if not path.exists() or not any(path.rglob("*.jsonl")):
        pytest.skip(f"sin transcripts de Claude en {path}")
    return path


@pytest.fixture
def real_kimi_logs() -> Path:
    import settings

    path = settings.KIMI_HOME / "sessions"
    if not path.exists() or not any(path.glob("wd_*/session_*/agents/*/wire.jsonl")):
        pytest.skip(f"sin wire.jsonl de Kimi en {path}")
    return path


@pytest.fixture(autouse=True)
def _relay_log_isolated(monkeypatch, tmp_path):
    """Ningún test escribe en debate-mcp/logs de verdad. Un test de regresión
    de producción dejó 16 eventos con pid 1234 en trigger_events.jsonl, que
    después contaban en metrics.py y healthcheck como turnos reales."""
    try:
        import relay
    except Exception:  # tests que no tocan el relay
        return
    monkeypatch.setattr(relay, "LOG_DIR", tmp_path)
    monkeypatch.setattr(relay, "EVENTS_PATH", tmp_path / "trigger_events.jsonl")
    monkeypatch.setattr(relay, "HEARTBEAT_PATH", tmp_path / "relay_heartbeat.json")
    monkeypatch.setattr(relay, "STATE_PATH", tmp_path / "relay_state.json")


@pytest.fixture(autouse=True)
def _no_accidental_agent_spawn(monkeypatch):
    """Red de seguridad: ningún test tiene por qué lanzar un `claude -p` real."""
    def _boom(*a, **kw):
        raise AssertionError(f"un test intentó lanzar un proceso: {a[:1]}")

    monkeypatch.setattr(subprocess, "Popen", _boom, raising=True)
    if os.environ.get('CLAMI_SEMANTIC_EVAL') != '1':
        monkeypatch.setenv('MEMORY_SEMANTIC', '0')
    os.environ.setdefault("DEBATE_CONNINFO", "dbname=debate user=adrianmedina host=localhost")


@pytest.fixture
def allow_real_processes(monkeypatch):
    """Opt-in para los tests que SÍ lanzan un proceso a propósito: stubs
    locales triviales (un print) que validan el pipeline de disparo. La red
    _no_accidental_agent_spawn sigue aplicando a todo lo demás."""
    monkeypatch.setattr(subprocess, "Popen", REAL_POPEN)
