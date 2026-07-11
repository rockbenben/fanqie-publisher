@echo off
setlocal
cd /d "%~dp0"

REM ==========================================================================
REM  Entry modes:
REM    (none)      double-click: relaunch self as a HIDDEN process, then exit
REM    __hidden__  the relaunched hidden instance (normal path, no window)
REM    debug       run visibly in the current console (troubleshooting)
REM ==========================================================================
set "MODE="
if /i "%~1"=="debug"      set "MODE=debug"
if /i "%~1"=="__hidden__" set "MODE=hidden"
if defined MODE goto :main

REM ---- relaunch self hidden ----
REM ShowWindow(GetConsoleWindow(), SW_HIDE) does NOT work when Windows Terminal
REM is the default console host (Windows 11 default): the visible window belongs
REM to WT, not to this cmd process, so hiding our own console handle is a no-op.
REM The reliable way is to CREATE the process hidden (SW_HIDE in STARTUPINFO),
REM which WScript.Shell.Run(..., 0) does and WT honors. This visible instance
REM exits immediately, so at most a brief flash remains.
>"%TEMP%\fanqie_hide_launch.vbs" echo CreateObject("WScript.Shell").Run """%~f0"" __hidden__", 0, False
wscript "%TEMP%\fanqie_hide_launch.vbs" >nul 2>nul && exit /b 0
REM Windows Script Host unavailable (rare: disabled by policy) - run visibly.
set "MODE=debug"

:main
set "LOGFILE=%TEMP%\fanqie_launcher.log"

REM ---- detect Python (py launcher first, fallback to python) ----
set "PYEXE="
where py >nul 2>nul && set "PYEXE=py"
if not defined PYEXE where python >nul 2>nul && set "PYEXE=python"
if not defined PYEXE (
    if "%MODE%"=="hidden" (
        powershell -NoProfile -Command "Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.MessageBox]::Show('Python not found. Install Python 3.10+ and check Add to PATH. Download: https://www.python.org/downloads/', 'fanqie') | Out-Null"
    ) else (
        echo [ERROR] Python not found. Install Python 3.10+ and check "Add to PATH".
        echo         Download: https://www.python.org/downloads/
        pause
    )
    exit /b 1
)

REM ---- check/install Python deps (pip show is fast, no browser init) ----
REM In hidden mode an install pops its OWN visible console so progress is seen;
REM when nothing is missing, no window ever appears.
%PYEXE% -m pip show playwright >nul 2>nul
if errorlevel 1 (
    if "%MODE%"=="hidden" (
        start "fanqie - installing dependencies" /wait cmd /c "echo [INFO] First run: installing Python dependencies... & %PYEXE% -m pip install -r requirements.txt"
    ) else (
        echo [INFO] First run: installing Python dependencies...
        %PYEXE% -m pip install -r requirements.txt
    )
)

REM ---- ensure browser engine is present ----
REM The chromium binary is a SEPARATE artifact from the pip package; if it's
REM missing or stale (e.g. after a Playwright upgrade) the GUI's login fails
REM with "Executable doesn't exist". executable_path is version-specific, so
REM this probe also catches the upgraded-but-not-synced case. Only install
REM (in a visible window) when actually missing.
%PYEXE% -c "import os,sys; from playwright.sync_api import sync_playwright; p=sync_playwright().start(); ep=p.chromium.executable_path; p.stop(); sys.exit(0 if os.path.exists(ep) else 1)" >nul 2>nul
if errorlevel 1 (
    if "%MODE%"=="hidden" (
        start "fanqie - installing browser engine" /wait cmd /c "echo [INFO] Installing browser engine (chromium)... & %PYEXE% -m playwright install chromium || (echo [WARN] Install failed - the app offers a one-click retry at login. & pause)"
    ) else (
        echo [INFO] Installing browser engine ^(chromium^)...
        %PYEXE% -m playwright install chromium || (
            echo [WARN] Browser engine install/verify failed ^(network?^). The app can
            echo        still retry from inside: one-click install offered at login.
        )
    )
)

REM ---- launch GUI ----
if "%MODE%"=="hidden" (
    REM Console is hidden; capture output so a crash is still diagnosable.
    %PYEXE% fanqie_gui.py >"%LOGFILE%" 2>&1
) else (
    %PYEXE% fanqie_gui.py
)
set "EXITCODE=%errorlevel%"

REM ---- surface abnormal exits (no silent death behind a hidden console) ----
if %EXITCODE% neq 0 (
    if "%MODE%"=="hidden" (
        >>"%LOGFILE%" echo.
        >>"%LOGFILE%" echo [Program exited abnormally, code %EXITCODE%] Screenshot this window for the developer.
        start "" notepad "%LOGFILE%"
    ) else (
        echo.
        echo [Program exited abnormally, code %EXITCODE%] See error above; screenshot it for me.
        pause
    )
)
exit /b %EXITCODE%
