@echo off
rem ============================================================
rem  UL10nForge 0.51.2 - one-click launcher
rem  - Bundled python: runtime\python (zero env dependency)
rem  - Double-click to start (no console window)
rem  - Debug mode: run "%~nx0" debug   to keep console open
rem  - Self-check:  run "%~nx0" --check
rem
rem  NOTE: keep this file ASCII-only with CRLF line endings.
rem  cmd.exe pre-reads the batch under the OEM code page (GBK on
rem  zh-CN Windows); non-ASCII bytes break its line parser and
rem  the launcher dies instantly. Chinese text shown to users
rem  must live in main.py (GUI), not here.
rem ============================================================
title UL10nForge 0.51.2
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "BUILTIN_PY=runtime\python\python.exe"
set "BUILTIN_PYW=runtime\python\pythonw.exe"

rem ---- prefer bundled python (ships with the package) ----
set "PY=%BUILTIN_PY%"
if exist "%BUILTIN_PY%" goto :py_ok

rem ---- bundled python missing: fall back to system python (dev only) ----
set "PY=python"
where python >nul 2>nul || set "PY=py"
where %PY% >nul 2>nul || goto :no_python

rem ---- system python deps check: auto install when missing ----
%PY% -c "import PySide6, httpx, chardet, UnityPy, dnfile" >nul 2>nul
if errorlevel 1 (
    echo [INFO] First run: installing dependencies...
    %PY% -m pip install -r requirements.txt
    if errorlevel 1 goto :deps_fail
    echo [OK] Dependencies installed.
)

:py_ok

rem ---- self check: verify only, for testing ----
if /i "%~1"=="--check" (
    echo [OK] Python: %PY%
    %PY% --version
    %PY% -c "import PySide6, httpx, chardet, UnityPy, dnfile; print('[OK] deps complete')"
    if errorlevel 1 echo [INFO] deps missing - will auto install on next launch.
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
exit /b 0

:no_python
echo [ERROR] Bundled python missing: runtime\python
echo [ERROR] No system Python found either.
echo Please re-extract the full package. For development,
echo install Python 3.10+ and enable "Add python.exe to PATH".
pause
exit /b 1

:deps_fail
echo.
echo [ERROR] Dependency install failed. Check network and retry.
pause
exit /b 1
