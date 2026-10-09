@echo off
rem Double-click me: start the whole demo, publish it, and keep MFA codes on screen.
rem Everything this starts is closed again by scripts\down.cmd.
cd /d "%~dp0.."
".venv\Scripts\python.exe" scripts\up.py --share %*
rem The codes window reads the database, so it opens only once the stack is up.
start "Risk Radar - MFA codes" cmd /k ".venv\Scripts\python.exe scripts\codes.py"
pause
