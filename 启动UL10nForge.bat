@echo off
chcp 65001 >nul 2>nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
rem ============================================================
rem  UL10nForge v2 - One-Click Portable Launcher (ASCII + CRLF)
rem ============================================================
title UL10nForge v2
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "PY=python"
if exist "runtime\python\python.exe" (
    set "PY=runtime\python\python.exe"
    set "PYTHONNOUSERSITE=1"
    set "PYTHONHOME="
    set "PYTHONPATH="
    if exist "%~dp0runtime\python\tcl\tcl8.6" set "TCL_LIBRARY=%~dp0runtime\python\tcl\tcl8.6"
    if exist "%~dp0runtime\python\tcl\tk8.6" set "TK_LIBRARY=%~dp0runtime\python\tcl\tk8.6"
)

if "%PY%"=="python" (
    where python >nul 2>nul
    if errorlevel 1 (
        echo [ERROR] Python 3.12+ not found and bundled runtime\python\python.exe is missing.
        pause
        exit /b 1
    )
)

%PY% -c "import fastapi, uvicorn, httpx, UnityPy" >nul 2>nul
if errorlevel 1 (
    echo [INFO] Installing dependencies...
    %PY% -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] Dependency installation failed.
        pause
        exit /b 1
    )
)

echo [INFO] Starting UL10nForge v2...
%PY% main.py %*
if errorlevel 1 pause
exit /b %errorlevel%
