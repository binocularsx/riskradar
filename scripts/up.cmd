@echo off
rem Double-click me: start the whole Risk Radar demo.
cd /d "%~dp0.."
".venv\Scripts\python.exe" scripts\up.py %*
pause
