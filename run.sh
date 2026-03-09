#!/usr/bin/env bash
cd "$(dirname "$0")"

if ! command -v python3 &>/dev/null; then
    echo "[!] Python3 not found. Please install Python 3.10+"
    exit 1
fi

if ! python3 -c "import sys; exit(0 if sys.version_info >= (3, 10) else 1)" 2>/dev/null; then
    echo "[!] Python 3.10+ is required. Current version:"
    python3 --version
    exit 1
fi

if ! python3 -c "import tkinter" &>/dev/null; then
    echo "[!] tkinter not found. Install with:"
    echo "    Debian/Ubuntu: sudo apt install python3-tk"
    echo "    Fedora:        sudo dnf install python3-tkinter"
    echo "    Arch:          sudo pacman -S tk"
    exit 1
fi

if ! python3 -c "import playwright" &>/dev/null; then
    echo "[*] Installing dependencies..."
    python3 -m pip install -r requirements.txt || { echo "[!] pip install failed"; exit 1; }
    echo "[*] Installing browser engine..."
    python3 -m playwright install chromium || { echo "[!] Browser engine install failed"; exit 1; }
    echo "[*] Done!"
fi

python3 fanqie_gui.py
