@echo off
rem Para relay y UI (las ventanas cmd que abrio start-magi) y baja Postgres.
setlocal
set "ROOT=%~dp0..\.."
set "PG=%ROOT%\experiments\pg\pgsql\bin"

taskkill /F /FI "WINDOWTITLE eq MAGI relay*" >nul 2>nul
taskkill /F /FI "WINDOWTITLE eq MAGI ui*" >nul 2>nul

rem Respaldo: cualquier python corriendo relay.py / magi_ui.py aunque no
rem tenga ventana con titulo (instancias levantadas a mano o con
rem Start-Process). El filtro por linea de comando las alcanza igual.
rem Filtro con comillas simples: el \"\" doble llegaba a PowerShell como una
rem cadena vacia seguida de Name=..., el comando fallaba y el relay viejo
rem seguia vivo (visto el 2026-09-17: reinicio con el pid de siempre).
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter 'Name=''python.exe''' | Where-Object { $_.CommandLine -match 'relay\.py|magi_ui\.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

"%PG%\pg_isready.exe" -h 127.0.0.1 -p 5432 >nul 2>nul
if not errorlevel 1 (
    "%PG%\pg_ctl.exe" -D "%ROOT%\experiments\pg\data" stop
) else (
    echo [stop-magi] postgres ya estaba abajo
)
echo [stop-magi] listo
