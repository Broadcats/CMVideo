@echo off
REM CMVideo Windows installer (double-click this file).
REM Just bootstraps PowerShell with ExecutionPolicy Bypass.
setlocal
set "HERE=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%HERE%install.ps1" %*
set "EC=%ERRORLEVEL%"
echo.
if "%EC%"=="0" (
    echo Done. You can close this window.
) else (
    echo Install ended with exit code %EC%.
)
pause
exit /b %EC%
