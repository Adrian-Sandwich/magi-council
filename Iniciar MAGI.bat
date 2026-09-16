@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0debate-mcp\bin\start-magi.ps1"
if errorlevel 1 pause
