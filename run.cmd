@echo off
setlocal
cd /d "%~dp0"
py -3 tools\bootstrap.py dashboard
if errorlevel 1 pause
