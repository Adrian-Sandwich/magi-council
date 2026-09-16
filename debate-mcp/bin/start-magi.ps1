param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$app = Join-Path $root 'debate-mcp'
$python = Join-Path $app '.venv/Scripts/python.exe'
$pg = Join-Path $root 'experiments/pg/pgsql/bin'
$logs = Join-Path $app 'logs'
$port = if ($env:MAGI_UI_PORT) { [int]$env:MAGI_UI_PORT } else { 8051 }
$url = "http://127.0.0.1:$port"
$mutex = New-Object System.Threading.Mutex($false, 'Local\ClaMiMagiLauncher')
$locked = $false
try {
    $locked = $mutex.WaitOne(0)
    if (-not $locked) { throw 'MAGI ya se esta iniciando. Espera unos segundos.' }
    New-Item -ItemType Directory -Force -Path $logs | Out-Null
    if (-not (Test-Path -LiteralPath $python)) { throw 'Falta el entorno Python debate-mcp/.venv.' }
    if (-not $env:DEBATE_CONNINFO) {
        & "$pg/pg_isready.exe" -h 127.0.0.1 -p 5432 | Out-Null
        if ($LASTEXITCODE -ne 0) {
            $data = Join-Path $root 'experiments/pg/data'
            $pgLog = Join-Path $root 'experiments/pg/pg.log'
            $process = Start-Process -FilePath "$pg/pg_ctl.exe" -ArgumentList "-D `"$data`" -l `"$pgLog`" -w start" -WindowStyle Hidden -PassThru
            $process.WaitForExit()
            if ($process.ExitCode -ne 0) { throw "Postgres no arranco. Revisa $pgLog" }
        }
    }
    & $python "$app/schema/migrate.py"
    if ($LASTEXITCODE -ne 0) { throw 'No se pudo actualizar la base de datos.' }
    foreach ($script in @('relay.py', 'magi_ui.py')) {
        $path = Join-Path $app $script
        $existing = Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
            $_.CommandLine -and $_.CommandLine.Replace('/', '\').Contains($path.Replace('/', '\'))
        }
        if (-not $existing) {
            $name = [IO.Path]::GetFileNameWithoutExtension($script)
            Start-Process -FilePath $python -ArgumentList "`"$path`"" -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput "$logs/$name.stdout.log" -RedirectStandardError "$logs/$name.stderr.log" | Out-Null
        }
    }
    $ready = $false
    for ($i = 0; $i -lt 30; $i++) {
        try {
            $response = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2
            if ($response.StatusCode -eq 200) { $ready = $true; break }
        } catch { Start-Sleep -Milliseconds 500 }
    }
    if (-not $ready) { throw "La interfaz no responde. Revisa $logs/magi_ui.stderr.log" }
    Write-Host "MAGI listo: $url"
    if (-not $NoBrowser) { Start-Process $url }
} catch {
    Write-Host "No se pudo iniciar MAGI: $_" -ForegroundColor Red
    exit 1
} finally {
    if ($locked) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}