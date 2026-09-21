#Requires -Version 5.1
# Agenda el panel semanal de calidad (metrics.py --quality) como tarea de
# Windows: cada lunes a las 9:00 imprime al log y avisa con un toast cuantas
# decisiones cerraron, cuantas tienen resultado y cuanta memoria se califico
# (plan de madurez 3.4).
#
#   powershell -ExecutionPolicy Bypass -File debate-mcp\bin\schedule-quality.ps1
#   powershell -ExecutionPolicy Bypass -File debate-mcp\bin\schedule-quality.ps1 -Remove
param([switch]$Remove, [string]$Day = 'Monday', [string]$At = '09:00')
$ErrorActionPreference = 'Stop'
$name = 'ClaMi-quality'
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$app = Join-Path $root 'debate-mcp'
$python = Join-Path $app '.venv\Scripts\python.exe'
$script = Join-Path $app 'metrics.py'
$log = Join-Path $app 'logs\quality.log'

if ($Remove) {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
    Write-Output "[schedule-quality] tarea $name eliminada"
    exit 0
}
if (-not (Test-Path -LiteralPath $python)) { throw "Falta $python (crea el venv primero)." }
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null

$command = "`"$python`" `"$script`" --quality --days 7 --notify >> `"$log`" 2>&1"
$action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument "/c `"$command`"" -WorkingDirectory $app
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $Day -At $At
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
    -StartWhenAvailable -MultipleInstances IgnoreNew -Hidden
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Force | Out-Null
Write-Output "[schedule-quality] tarea $name cada $Day a las $At; log en $log"
