@echo off
chcp 65001 >nul
title UL10nForge 0.37.0
setlocal enabledelayedexpansion
cd /d "%~dp0"

rem ============================================================
rem  UL10nForge 0.37.0 - one-click launcher
rem  - 内置 Python(runtime\python):解压即用,零环境依赖
rem  - Double-click to start (no console window)
rem  - Debug mode: run "%~nx0 debug" to keep console open
rem ============================================================

set "BUILTIN_PY=runtime\python\python.exe"
set "BUILTIN_PYW=runtime\python\pythonw.exe"

rem ---- 优先使用内置 Python(随包分发,依赖已全部装好)----
set "PY=%BUILTIN_PY%"
if exist "%BUILTIN_PY%" goto :py_ok

rem ---- 内置缺失:回退系统 Python(仅开发环境)----
set "PY=python"
where python >nul 2>nul || set "PY=py"
where %PY% >nul 2>nul || (
    echo [ERROR] 内置 Python 缺失(runtime\python),且系统未安装 Python。
    echo 请重新解压完整包;开发环境请安装 Python 3.10+ 并勾选
    echo "Add python.exe to PATH"。
    pause
    exit /b 1
)

rem ---- 系统 Python 依赖检查:缺失则自动安装 ----
%PY% -c "import PySide6, httpx, chardet, UnityPy, dnfile" >nul 2>nul
if errorlevel 1 (
    echo [INFO] First run: installing dependencies...
    %PY% -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo [ERROR] Dependency install failed. Check network and retry.
        pause
        exit /b 1
    )
    echo [OK] Dependencies installed.
)

:py_ok

rem ---- self check: verify only (for testing) ----
if /i "%~1"=="--check" (
    echo [OK] Python: %PY%
    %PY% --version
    %PY% -c "import PySide6, httpx, chardet, UnityPy, dnfile; print('[OK] deps complete')"
    if errorlevel 1 (
        echo [INFO] deps missing - will auto install on next launch.
    )
    exit /b 0
)

rem ---- debug mode: run with console ----
if /i "%~1"=="debug" (
    echo [INFO] Debug mode: closing this window exits the app.
    %PY% main.py
    echo.
    echo [INFO] App exited with code %errorlevel%
    pause
    exit /b %errorlevel%
)

rem ---- normal launch: pythonw without console window ----
if exist "%BUILTIN_PYW%" (
    start "" "%BUILTIN_PYW%" main.py
    exit /b 0
)
where pythonw >nul 2>nul
if not errorlevel 1 (
    start "" pythonw main.py
    exit /b 0
)

%PY% main.py
pause
