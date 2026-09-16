@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-magi.ps1" %*
exit /b %errorlevel%
