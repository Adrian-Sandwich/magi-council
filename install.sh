#!/usr/bin/env bash
# Instala MAGI en macOS o Linux: venv, dependencias, base, esquema y un
# heads.json para editar. Termina corriendo el diagnóstico.
#
# Idempotente: correrlo dos veces no rompe nada y sirve para reparar una
# instalación a medias.
#
#   ./install.sh              # instala y deja todo listo
#   ./install.sh --start      # además levanta relay y UI en segundo plano
#
# Postgres: si DEBATE_CONNINFO está seteada, usa esa base; si no, el postgres
# local (createdb debate). El Postgres portátil de experiments/pg es sólo de
# Windows.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$ROOT/debate-mcp"
VENV="$APP/.venv"
PY="$VENV/bin/python"
START=0
[[ "${1:-}" == "--start" ]] && START=1

step() { printf '\033[36m[install] %s\033[0m\n' "$1"; }
done_() { printf '\033[32m[install] %s\033[0m\n' "$1"; }
warn() { printf '\033[33m[install] %s\033[0m\n' "$1"; }

# ---------------------------------------------------------------- 1. venv
if [[ ! -x "$PY" ]]; then
    INTERPRETER=""
    for candidate in python3.14 python3.13 python3.12 python3; do
        command -v "$candidate" >/dev/null 2>&1 || continue
        if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 12) else 1)'; then
            INTERPRETER="$candidate"; break
        fi
    done
    [[ -n "$INTERPRETER" ]] || { echo "No encuentro Python 3.12+. Instalalo y volvé a correr esto." >&2; exit 1; }
    step "creando el venv con $INTERPRETER"
    "$INTERPRETER" -m venv "$VENV"
else
    done_ "venv ya existe"
fi

step "instalando dependencias"
"$PY" -m pip install --quiet --disable-pip-version-check --upgrade pip
"$PY" -m pip install --quiet --disable-pip-version-check -r "$APP/requirements.txt"
done_ "dependencias instaladas"

# ---------------------------------------------------------------- 2. base
if [[ -n "${DEBATE_CONNINFO:-}" ]]; then
    done_ "usando la base de DEBATE_CONNINFO ($DEBATE_CONNINFO)"
elif command -v psql >/dev/null 2>&1; then
    if ! psql -lqt 2>/dev/null | cut -d'|' -f1 | grep -qw debate; then
        step "creando la base «debate»"
        createdb debate || warn "no pude crear la base; creala a mano o exportá DEBATE_CONNINFO"
    else
        done_ "la base «debate» ya existe"
    fi
else
    warn "sin cliente de Postgres en el PATH: instalá Postgres (brew install postgresql@16) o exportá DEBATE_CONNINFO"
fi

# ---------------------------------------------------------------- 3. esquema
step "aplicando migraciones"
"$PY" "$APP/schema/migrate.py"

# ---------------------------------------------------------------- 4. cabezas
if [[ ! -f "$APP/heads.json" ]]; then
    cp "$APP/heads.example.json" "$APP/heads.json"
    warn "escribí un heads.json de ejemplo: editá $APP/heads.json con tus cabezas (CLI o API)"
else
    done_ "heads.json ya existe"
fi

# ---------------------------------------------------------------- 5. diagnóstico
step "diagnóstico"
set +e
"$PY" "$APP/doctor.py"
DIAGNOSIS=$?
set -e
[[ $DIAGNOSIS -lt 2 ]] || { echo "El diagnóstico encontró algo crítico (ver CRIT arriba)." >&2; exit 1; }

if [[ $START -eq 1 ]]; then
    step "levantando relay y UI en segundo plano"
    mkdir -p "$APP/logs"
    (cd "$ROOT" && nohup "$PY" "$APP/relay.py" >>"$APP/logs/relay.stdout.log" 2>&1 &)
    (cd "$ROOT" && nohup "$PY" "$APP/magi_ui.py" >>"$APP/logs/magi_ui.stdout.log" 2>&1 &)
    sleep 3
    done_ "MAGI en http://127.0.0.1:8051 (logs en debate-mcp/logs/)"
else
    done_ "listo. Levantalo con: $PY $APP/relay.py  y  $PY $APP/magi_ui.py"
fi
