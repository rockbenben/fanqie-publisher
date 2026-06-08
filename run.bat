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

REM ---- check/install Python deps (pip show is fast, no browser init) ----
%PYEXE% -m pip show playwright >nul 2>nul
if errorlevel 1 (
    echo [INFO] First run: installing Python dependencies...
    %PYEXE% -m pip install -r requirements.txt
)

REM ---- ensure browser engine is present (idempotent: fast if already installed) ----
REM    Must run even when the pip package is already there: the chromium binary is
REM    a SEPARATE artifact. If it's missing or stale (e.g. after a Playwright
REM    upgrade), the GUI's login fails with "Executable doesn't exist" and looks
REM    like it does nothing. Mirrors run.sh, which already installs unconditionally.
echo [INFO] Checking browser engine (chromium)...
%PYEXE% -m playwright install chromium
if errorlevel 1 (
    echo [WARN] Browser engine install/verify failed ^(network?^). The app can still
    echo        retry from inside: it will offer a one-click install when you log in.
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
