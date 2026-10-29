@echo off
setlocal
cd /d "%~dp0"
py -3 tools\bootstrap.py qq
if errorlevel 1 pause
