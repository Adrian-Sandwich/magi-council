#Requires -Version 5.1
# Arranca MAGI al iniciar sesion en Windows. Existe porque despues de un
# reinicio nadie levantaba Postgres, relay y UI: el 2026-09-21 la maquina se
# reinicio y el consejo quedo caido hasta que alguien miro.
#
# Va por la carpeta Inicio del usuario (shell:startup) y no por una tarea
# programada: una tarea con disparador "al iniciar sesion" terminaba con
# resultado 0 sin ejecutar nada ni dejar rastro, dos veces, y no hay historial
# del programador para saber por que. Un .cmd en Inicio es lo que Windows
# ejecuta siempre al entrar a la sesion.
#
#   powershell -ExecutionPolicy Bypass -File debate-mcp\bin\schedule-magi.ps1          # instala
#   powershell -ExecutionPolicy Bypass -File debate-mcp\bin\schedule-magi.ps1 -Now     # instala y corre ya
#   powershell -ExecutionPolicy Bypass -File debate-mcp\bin\schedule-magi.ps1 -Remove
param([switch]$Remove, [switch]$Now)
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$app = Join-Path $root 'debate-mcp'
$script = Join-Path $app 'bin\start-magi.ps1'
$log = Join-Path $app 'logs\start-magi.task.log'
$startup = [Environment]::GetFolderPath('Startup')
$entry = Join-Path $startup 'ClaMi-magi.cmd'

if ($Remove) {
    Remove-Item -LiteralPath $entry -Force -ErrorAction SilentlyContinue
    Write-Output "[schedule-magi] $entry eliminado"
    exit 0
}
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null
# `start /min` para que la consola no quede abierta al entrar; el lanzador
# cuelga los procesos de WMI, asi que cerrar esa consola no los mata.
$lines = @(
    '@echo off',
    "start `"ClaMi MAGI`" /min cmd /c `"`"powershell.exe`" -NoProfile -ExecutionPolicy Bypass -File `"$script`" -NoBrowser >> `"$log`" 2>&1`""
)
Set-Content -LiteralPath $entry -Value $lines -Encoding ASCII
Write-Output "[schedule-magi] $entry arranca MAGI al iniciar sesion; log en $log"
if ($Now) {
    & cmd.exe /c "`"$entry`""
    Start-Sleep -Seconds 15
    if (Test-Path -LiteralPath $log) { Get-Content -LiteralPath $log -Tail 3 } else { Write-Output '[schedule-magi] sin log todavia' }
}
