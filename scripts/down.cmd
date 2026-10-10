@echo off
rem Double-click me: stop the Risk Radar demo.
cd /d "%~dp0.."
".venv\Scripts\python.exe" scripts\down.py %*
pause
