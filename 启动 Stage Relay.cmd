@echo off
chcp 65001 >nul
cd /d "%~dp0"
python app.py --open
if errorlevel 1 pause
