@echo off
setlocal
cd /d "%~dp0"
py -3 tools\bootstrap.py qq --voice-options local\config\voice-options.json
if errorlevel 1 pause
