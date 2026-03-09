@echo off
cd /d "%~dp0"

where python >nul 2>&1
if %errorlevel% neq 0 (
    echo [!] Python not found. Please install Python 3.10+
    echo     https://www.python.org/downloads/
    pause
    exit /b 1
)

python -c "import sys; exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if %errorlevel% neq 0 (
    echo [!] Python 3.10+ is required. Current version:
    python --version
    echo     https://www.python.org/downloads/
    pause
    exit /b 1
)

python -c "import playwright" >nul 2>&1
if %errorlevel% neq 0 (
    echo [*] Installing dependencies...
    python -m pip install -r requirements.txt
    if %errorlevel% neq 0 (
        echo [!] pip install failed
        pause
        exit /b 1
    )
    echo [*] Installing browser engine...
    python -m playwright install chromium
    if %errorlevel% neq 0 (
        echo [!] Browser engine install failed
        pause
        exit /b 1
    )
    echo [*] Done!
)

start "" pythonw fanqie_gui.py
