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

REM minimize THIS console (not hide) so the GUI takes focus; window stays alive
REM so the abnormal-exit pause below is still reachable by restoring it.
powershell -NoProfile -Command "Add-Type -Name Win -Namespace Con -MemberDefinition '[DllImport(\"kernel32.dll\")] public static extern System.IntPtr GetConsoleWindow(); [DllImport(\"user32.dll\")] public static extern bool ShowWindow(System.IntPtr h, int n);'; [Con.Win]::ShowWindow([Con.Win]::GetConsoleWindow(), 6) | Out-Null" >nul 2>nul

%PYEXE% fanqie_gui.py
set "EXITCODE=!errorlevel!"

REM pause only on abnormal exit so errors stay visible (no more flash-and-close)
if !EXITCODE! neq 0 (
    REM restore the minimized console so the error is visible again
    powershell -NoProfile -Command "Add-Type -Name Win2 -Namespace Con -MemberDefinition '[DllImport(\"kernel32.dll\")] public static extern System.IntPtr GetConsoleWindow(); [DllImport(\"user32.dll\")] public static extern bool ShowWindow(System.IntPtr h, int n);'; [Con.Win2]::ShowWindow([Con.Win2]::GetConsoleWindow(), 9) | Out-Null" >nul 2>nul
    echo.
    echo [Program exited abnormally, code !EXITCODE!] See error above; screenshot it for me.
    pause
)
endlocal
