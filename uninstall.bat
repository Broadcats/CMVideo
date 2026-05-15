@echo off
REM CMVideo Windows uninstaller (double-click this file).
setlocal
set "HERE=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%HERE%uninstall.ps1" %*
echo.
pause
