#Requires -Version 5.1
# Agenda el respaldo diario del tablero (backup.py) como tarea de Windows.
# Existe porque Postgres es lo unico irreemplazable del sistema y no habia
# respaldo: el 2026-09-24 un apagon sucio dejo cuatro archivos en ceros y la
# base se salvo por suerte.
#
#   powershell -ExecutionPolicy Bypass -File debate-mcp\bin\schedule-backup.ps1
#   powershell -ExecutionPolicy Bypass -File debate-mcp\bin\schedule-backup.ps1 -At 03:00 -Keep 30
#   powershell -ExecutionPolicy Bypass -File debate-mcp\bin\schedule-backup.ps1 -Remove
param([switch]$Remove, [string]$At = '09:30', [int]$Keep = 14)
$ErrorActionPreference = 'Stop'
$name = 'ClaMi-backup'
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$app = Join-Path $root 'debate-mcp'
$python = Join-Path $app '.venv\Scripts\python.exe'
$script = Join-Path $app 'backup.py'
$log = Join-Path $app 'logs\backup.log'

if ($Remove) {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
    Write-Output "[schedule-backup] tarea $name eliminada"
    exit 0
}
if (-not (Test-Path -LiteralPath $python)) { throw "Falta $python (crea el venv primero)." }
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null

# Mismo patron que schedule-healthcheck.ps1: cmd /c con el comando entero
# entre comillas extra, para poder redirigir al log.
$command = "`"$python`" `"$script`" --keep $Keep >> `"$log`" 2>&1"
$action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument "/c `"$command`"" -WorkingDirectory $app
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -StartWhenAvailable -MultipleInstances IgnoreNew -Hidden
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Force | Out-Null
Write-Output "[schedule-backup] tarea $name todos los dias a las $At (conserva $Keep); log en $log"
