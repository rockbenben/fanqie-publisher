@echo off
cd /d "%~dp0"
setlocal enabledelayedexpansion

REM ---- detect Python (py launcher first, fallback to python) ----
set "PYEXE="
where py >nul 2>nul && set "PYEXE=py"
if not defined PYEXE (
    where python >nul 2>nul && set "PYEXE=python"
)
if not defined PYEXE (
    echo [ERROR] Python not found. Install Python 3.10+ and check "Add to PATH".
    echo         Download: https://www.python.org/downloads/
    pause
    exit /b 1
)

REM ---- check/install dependency (pip show is fast, no browser init) ----
%PYEXE% -m pip show playwright >nul 2>nul
if errorlevel 1 (
    echo [INFO] First run: installing dependencies...
    %PYEXE% -m pip install -r requirements.txt
    %PYEXE% -m playwright install chromium
)

REM ---- launch GUI in foreground (keep console; do NOT use pythonw/start /min) ----
echo Using Python: %PYEXE%
%PYEXE% fanqie_gui.py

REM pause only on abnormal exit so errors stay visible (no more flash-and-close)
if errorlevel 1 (
    echo.
    echo [Program exited abnormally, code !errorlevel!] See error above; screenshot it for me.
    pause
)
endlocal
