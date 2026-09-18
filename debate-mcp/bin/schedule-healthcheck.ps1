#Requires -Version 5.1
# Agenda healthcheck.py cada 15 minutos como tarea de Windows (equivalente
# del launchd de macOS). Existe porque en Windows nadie corria el chequeo:
# Postgres se cayo el 2026-09-17 y no hubo aviso hasta que alguien miro.
# --notify muestra un toast; --quiet solo escribe al log cuando algo falla.
#
#   powershell -ExecutionPolicy Bypass -File debate-mcp\bin\schedule-healthcheck.ps1
#   powershell -ExecutionPolicy Bypass -File debate-mcp\bin\schedule-healthcheck.ps1 -Remove
param([switch]$Remove, [int]$Minutes = 15)
$ErrorActionPreference = 'Stop'
$name = 'ClaMi-healthcheck'
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$app = Join-Path $root 'debate-mcp'
$python = Join-Path $app '.venv\Scripts\python.exe'
$script = Join-Path $app 'healthcheck.py'
$log = Join-Path $app 'logs\healthcheck.log'

if ($Remove) {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
    Write-Output "[schedule-healthcheck] tarea $name eliminada"
    exit 0
}
if (-not (Test-Path -LiteralPath $python)) { throw "Falta $python (crea el venv primero)." }
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null

# cmd /c para poder redirigir al log; la tarea corre en la sesion interactiva
# del usuario (LogonType Interactive) para que el toast se vea. El comando
# entero va entre comillas extra: cmd /c quita la primera y la ultima comilla
# de la linea, y con dos rutas entrecomilladas eso rompia la invocacion
# (LastTaskResult 0x8007042B).
$command = "`"$python`" `"$script`" --notify --quiet >> `"$log`" 2>&1"
$action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument "/c `"$command`"" -WorkingDirectory $app
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $Minutes)
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
    -StartWhenAvailable -MultipleInstances IgnoreNew -Hidden
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Force | Out-Null
Write-Output "[schedule-healthcheck] tarea $name cada $Minutes min; log en $log"
