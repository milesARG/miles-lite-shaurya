@echo off
setlocal
cd /d "%~dp0"
title Miles Lite setup
echo.
echo   MILES LITE // SETUP  (CPU-only build for laptops)
echo   Made by Arnav, personally for Shaurya
echo   ================================================
echo.

rem Long folder paths break some Python packages on Windows - keep Miles Lite somewhere short.
set "HERE=%~dp0"
if not "%HERE:~70,1%"=="" (
    echo   NOTE: this folder's path is long. If installing fails, move "Miles Lite" to e.g. C:\Miles Lite
    echo.
)

rem Python 3.12 or 3.13 has ready-made packages for everything Miles needs.
set "PY="
py -3.12 -c "" >nul 2>nul && set "PY=py -3.12"
if not defined PY py -3.13 -c "" >nul 2>nul && set "PY=py -3.13"
rem (runs python rather than "where python": a fresh Windows has a fake python.exe that only opens the Store)
if not defined PY python -c "import sys; assert sys.version_info >= (3, 10)" >nul 2>nul && set "PY=python"
if not defined PY py -3 -c "import sys; assert sys.version_info >= (3, 10)" >nul 2>nul && set "PY=py -3"
if not defined PY (
    echo [0/5] Installing Python 3.12...
    winget install --id Python.Python.3.12 -e --accept-source-agreements --accept-package-agreements --silent
    set "PY=py -3.12"
    if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY="%LOCALAPPDATA%\Programs\Python\Python312\python.exe""
)

if not exist ".venv\Scripts\python.exe" (
    echo [1/5] Creating virtual environment...
    %PY% -m venv .venv || (echo Failed to create venv & pause & exit /b 1)
)
echo [2/5] Installing Python packages (about 400 MB, no GPU libraries)...
".venv\Scripts\python.exe" -m pip install --upgrade pip -q
".venv\Scripts\python.exe" -m pip install -r requirements.txt || (echo Package install failed & pause & exit /b 1)

set "OLLAMA=%LOCALAPPDATA%\Programs\Ollama\ollama.exe"
if not exist "%OLLAMA%" (
    echo [3/5] Installing Ollama...
    winget install --id Ollama.Ollama -e --accept-source-agreements --accept-package-agreements --silent
)
echo [3/5] Downloading the AI model (about 1.4 GB, one time)...
for /f "delims=" %%m in ('".venv\Scripts\python.exe" -c "import config; print(config.OLLAMA_MODEL)"') do set MODEL=%%m
"%OLLAMA%" pull %MODEL%

echo [4/5] Downloading the voices (about 60 MB each)...
for /f "delims=" %%v in ('".venv\Scripts\python.exe" -c "import config; print(config.PIPER_VOICE, config.PIPER_VOICE_FRIDAY)"') do set VOICES=%%v
if not exist "data\piper" mkdir "data\piper"
".venv\Scripts\python.exe" -m piper.download_voices --download-dir data\piper %VOICES%

echo [5/5] Downloading speech recognition models...
".venv\Scripts\python.exe" -c "import os,sys; os.environ['HF_HUB_DISABLE_SYMLINKS_WARNING']='1'; sys.path.insert(0,'.'); from miles.audio import Transcriber; Transcriber()"

echo.
echo   Setup complete. Enjoy your assistant, Shaurya - from Arnav.
echo   Double-click "Start Miles.bat" to launch.
echo   The first start takes about a minute while the AI reads its instructions.
echo.
pause
