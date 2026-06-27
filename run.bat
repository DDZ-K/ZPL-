@echo off
cd /d "%~dp0"
python "%~dp0main.py"
if errorlevel 1 pause
