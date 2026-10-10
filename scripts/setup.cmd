@echo off
rem Double-click me once on a fresh machine, before demo.cmd.
rem Installs what the demo needs and checks the rest. Safe to run again.
rem   setup.cmd --check          report what is missing, change nothing
rem   setup.cmd --no-data        empty desk instead of restoring fixtures\handover.dump
cd /d "%~dp0.."
set "PY="
rem A Python that is too old, or the Microsoft Store stub, must not be picked: test it.
py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>&1 && set "PY=py -3"
if not defined PY python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>&1 && set "PY=python"
if defined PY goto found
echo.
echo   Python 3.12 or newer was not found. Install it, then double-click this again:
echo.
echo       winget install Python.Python.3.12
echo.
echo   If you have only just installed it, open a new window first so it is on the PATH.
pause
exit /b 1
:found
%PY% scripts\setup.py %*
pause
