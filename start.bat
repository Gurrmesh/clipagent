@echo off
REM One command to run ClipAgent Studio on Windows. Double-click this file.
cd /d "%~dp0"
if "%PORT%"=="" set PORT=8000

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo   Python isn't installed. Get it from python.org and tick
  echo   "Add python.exe to PATH" on the first installer screen.
  pause & exit /b 1
)

where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo.
  echo   ffmpeg isn't installed - the renderer needs it. Run:
  echo       winget install Gyan.FFmpeg
  echo   then close and reopen this window.
  pause & exit /b 1
)

if not exist .venv (
  echo.
  echo   First run - setting up. This takes a minute.
  python -m venv .venv
)
call .venv\Scripts\activate.bat

if not exist .venv\.installed (
  echo   Installing dependencies...
  python -m pip install --quiet --upgrade pip
  python -m pip install --quiet -r requirements.txt
  echo done> .venv\.installed
)

if not exist .env (
  copy .env.example .env >nul
  echo.
  echo   Created .env for you. Open it, paste your two API keys,
  echo   then run this file again.
  pause & exit /b 1
)

echo.
echo   ClipAgent Studio is starting on http://localhost:%PORT%  (Ctrl+C to stop)
start "" http://localhost:%PORT%
python -m uvicorn app.main:app --host 127.0.0.1 --port %PORT%
