@echo off
chcp 65001 >nul
REM ============================================================
REM 番茄作家上传工具 - Windows 启动脚本
REM ============================================================

cd /d "%~dp0"

REM 检查 Python 是否安装
setlocal enabledelayedexpansion

REM 优先使用 py launcher (官方推荐), 回退到 python
REM ---------- 检测 Python ----------
set "PYEXE="
where py >/dev/null 2>/dev/null && set "PYEXE=py"
if not defined PYEXE (
    where python >/dev/null 2>/dev/null && set "PYEXE=python"
)
if not defined PYEXE (
    echo [ERROR] 未检测到 Python, 请先安装 Python 3.10+ 并勾选 Add to PATH
    echo         下载地址: https://www.python.org/downloads/
    pause
    exit /b 1
)

REM ---------- 检查依赖 ----------
REM 用 pip show 检测 playwright 是否已安装（比 import 更快，不触发浏览器初始化）
set "NEED_INSTALL="
%PYEXE% -m pip show playwright >/dev/null 2>/dev/null || set "NEED_INSTALL=1"

REM ---------- 安装依赖 ----------
if defined NEED_INSTALL (
    echo [INFO] 首次运行，正在安装依赖...
    %PYEXE% -m pip install -r requirements.txt
    %PYEXE% -m playwright install chromium
)

REM ---------- 启动 GUI ----------
REM 用 pythonw 静默启动（无终端窗口），找不到则回退 python
set "PYWEXE=%PYEXE:python=pythonw%"
if "%PYWEXE%"=="%PYEXE%" set "PYWEXE=pythonw"
if /i "%PYEXE%"=="py" set "PYWEXE=pyw"
start "" %PYWEXE% fanqie_gui.py
