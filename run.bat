@echo off
REM CMVideo - Windows launcher
REM
REM Adds portable bin\ folders to PATH (so ffmpeg / espeak-ng are
REM visible to the Python code even when not system-installed) and
REM launches app.py in the project's .venv with pythonw.exe so no
REM console window stays open.

setlocal
set "HERE=%~dp0"

REM Strip trailing backslash for cleaner PATH entries
if "%HERE:~-1%"=="\" set "HERE=%HERE:~0,-1%"

REM Prepend portable binaries (installed by install.ps1 when winget
REM wasn't usable). Harmless if the folders don't exist.
if exist "%HERE%\bin\ffmpeg\bin\ffmpeg.exe"   set "PATH=%HERE%\bin\ffmpeg\bin;%PATH%"
if exist "%HERE%\bin\espeak-ng\espeak-ng.exe" set "PATH=%HERE%\bin\espeak-ng;%PATH%"

REM Venv check
if not exist "%HERE%\.venv\Scripts\pythonw.exe" (
    echo CMVideo isn't fully installed yet ^(no .venv found^).
    echo.
    echo Run install.bat first to set everything up, then try again.
    echo.
    pause
    exit /b 1
)

REM pythonw.exe runs without a console window. Use `start ""` so this
REM batch file returns immediately and the Tk window owns the session.
start "" "%HERE%\.venv\Scripts\pythonw.exe" "%HERE%\app.py" %*
exit /b 0
