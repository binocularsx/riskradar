@echo off
rem Double-click me: live MFA codes for the seeded Risk Radar accounts.
cd /d "%~dp0.."
".venv\Scripts\python.exe" scripts\codes.py %*
pause
