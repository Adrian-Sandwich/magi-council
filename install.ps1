#Requires -Version 5.1
<#
.SYNOPSIS
    Instala MAGI en esta maquina: venv, dependencias, base, esquema, tareas
    agendadas y un heads.json para editar. Termina corriendo el diagnostico.

.DESCRIPTION
    Idempotente: correrlo dos veces no rompe nada y sirve para reparar una
    instalacion a medias. Existe porque "clona y segui el README" eran nueve
    pasos manuales y cualquiera de ellos, salteado, daba un error distinto
    tres capas mas abajo.

    Postgres, en orden: si DEBATE_CONNINFO esta seteada, esa base; si no, el
    Postgres portatil de experiments/pg; si no esta, cualquier postgres del
    PATH. Con -WithPostgres baja los binarios oficiales (~350 MB) y crea el
    cluster portatil.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install.ps1
    powershell -ExecutionPolicy Bypass -File install.ps1 -WithPostgres -Schedule
#>
param(
    [switch]$WithPostgres,   # baja e inicializa el Postgres portatil
    [switch]$Schedule,       # agenda arranque, healthcheck y panel semanal
    [switch]$NoStart,        # no levanta MAGI al terminar
    [string]$PostgresUrl = 'https://get.enterprisedb.com/postgresql/postgresql-16.4-1-windows-x64-binaries.zip'
)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$app = Join-Path $root 'debate-mcp'
$venv = Join-Path $app '.venv'
$python = Join-Path $venv 'Scripts\python.exe'
$pgRoot = Join-Path $root 'experiments\pg'
$pgBin = Join-Path $pgRoot 'pgsql\bin'
$pgData = Join-Path $pgRoot 'data'

function Step($text) { Write-Host "[install] $text" -ForegroundColor Cyan }
function Done($text) { Write-Host "[install] $text" -ForegroundColor Green }
function Warn($text) { Write-Host "[install] $text" -ForegroundColor Yellow }

# ---------------------------------------------------------------- 1. venv
if (-not (Test-Path -LiteralPath $python)) {
    $interpreter = $null
    foreach ($candidate in @('py -3.14', 'py -3.13', 'py -3.12', 'python3', 'python')) {
        $parts = $candidate.Split(' ')
        $exe = Get-Command $parts[0] -ErrorAction SilentlyContinue
        if (-not $exe) { continue }
        $version = & $exe.Source @($parts[1..($parts.Length - 1)]) -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>$null
        if ($LASTEXITCODE -eq 0 -and [version]$version -ge [version]'3.12') { $interpreter = $candidate; break }
    }
    if (-not $interpreter) { throw 'No encuentro Python 3.12+. Instalalo desde https://www.python.org/downloads/ y volve a correr esto.' }
    Step "creando el venv con $interpreter"
    $parts = $interpreter.Split(' ')
    & $parts[0] @($parts[1..($parts.Length - 1)]) -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw 'No pude crear el venv.' }
} else { Done 'venv ya existe' }

Step 'instalando dependencias'
& $python -m pip install --quiet --disable-pip-version-check --upgrade pip
& $python -m pip install --quiet --disable-pip-version-check -r (Join-Path $app 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'pip install fallo.' }
Done 'dependencias instaladas'

# ---------------------------------------------------------------- 2. Postgres
if ($env:DEBATE_CONNINFO) {
    Done "usando la base de DEBATE_CONNINFO ($env:DEBATE_CONNINFO)"
} elseif ($WithPostgres -and -not (Test-Path -LiteralPath $pgBin)) {
    Step "bajando Postgres portatil (~350 MB) de $PostgresUrl"
    New-Item -ItemType Directory -Force -Path $pgRoot | Out-Null
    $zip = Join-Path $pgRoot 'pg.zip'
    try {
        Invoke-WebRequest -Uri $PostgresUrl -OutFile $zip -UseBasicParsing
        Expand-Archive -LiteralPath $zip -DestinationPath $pgRoot -Force
    } catch {
        throw "No pude bajar Postgres: $_`nAlternativa: instala Postgres a mano y exporta DEBATE_CONNINFO, o descomprimi los binarios en $pgRoot."
    }
    Step 'inicializando el cluster'
    & "$pgBin\initdb.exe" -D $pgData -U $env:USERNAME --auth=trust --encoding=UTF8 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'initdb fallo.' }
    Done 'Postgres portatil listo'
} elseif (Test-Path -LiteralPath $pgBin) {
    Done 'Postgres portatil ya presente'
} elseif (Get-Command pg_isready -ErrorAction SilentlyContinue) {
    Done 'usando el Postgres del PATH'
} else {
    Warn 'sin Postgres: volve a correr con -WithPostgres, o instala uno y exporta DEBATE_CONNINFO'
}

# arrancarlo y crear la base si hace falta
if (-not $env:DEBATE_CONNINFO -and (Test-Path -LiteralPath $pgBin)) {
    & "$pgBin\pg_isready.exe" -h 127.0.0.1 -p 5432 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Step 'arrancando Postgres'
        Start-Process -FilePath "$pgBin\pg_ctl.exe" -ArgumentList "-D `"$pgData`" -l `"$pgRoot\pg.log`" -w start" -WindowStyle Hidden -Wait
    }
    & "$pgBin\psql.exe" -h 127.0.0.1 -lqt 2>$null | Select-String -Quiet 'debate' | Out-Null
    $exists = $?
    if (-not $exists) {
        Step 'creando la base "debate"'
        & "$pgBin\createdb.exe" -h 127.0.0.1 debate 2>$null
    }
}

# ---------------------------------------------------------------- 3. esquema
Step 'aplicando migraciones'
& $python (Join-Path $app 'schema\migrate.py')
if ($LASTEXITCODE -ne 0) { throw 'No pude aplicar el esquema. Esta Postgres arriba?' }

# ---------------------------------------------------------------- 4. cabezas
$headsPath = Join-Path $app 'heads.json'
if (-not (Test-Path -LiteralPath $headsPath)) {
    Copy-Item (Join-Path $app 'heads.example.json') $headsPath
    Warn "escribi un heads.json de ejemplo: edita $headsPath con tus cabezas (CLI o API)"
} else { Done 'heads.json ya existe' }

# ---------------------------------------------------------------- 5. tareas
if ($Schedule) {
    Step 'agendando arranque, healthcheck y panel semanal'
    & (Join-Path $app 'bin\schedule-magi.ps1')
    & (Join-Path $app 'bin\schedule-healthcheck.ps1')
    & (Join-Path $app 'bin\schedule-quality.ps1')
}

# ---------------------------------------------------------------- 6. diagnostico
Step 'diagnostico'
& $python (Join-Path $app 'doctor.py')
$diagnosis = $LASTEXITCODE
if ($diagnosis -ge 2) { throw 'El diagnostico encontro algo critico (ver CRIT arriba).' }

if (-not $NoStart) {
    Step 'levantando MAGI'
    & (Join-Path $app 'bin\start-magi.ps1')
} else {
    Done 'listo. Arranca con "Iniciar MAGI.bat"'
}
