@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo Miles isn't set up yet. Running setup first...
    call setup.bat
)
start "" ".venv\Scripts\pythonw.exe" main.py
