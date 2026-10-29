@echo off
setlocal
cd /d "%~dp0"
py -3 tools\bootstrap.py install
if errorlevel 1 pause
