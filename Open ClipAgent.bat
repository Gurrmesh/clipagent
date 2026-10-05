@echo off
REM Opens ClipAgent. Starts it first when it is not running yet.
title Opening ClipAgent
cd /d "C:\Users\gurem\Downloads\clipagent-studio\clipagent-studio"

REM Already running? Just open it in the browser.
curl.exe -s -f -o nul -m 2 http://127.0.0.1:8000/api/config
if %errorlevel%==0 goto open

REM Not running: start it in its own window (keep that window open while you use ClipAgent).
echo.
echo   Starting ClipAgent - it opens in your browser in a few seconds...
start "ClipAgent Studio - keep this window open" "C:\Users\gurem\Downloads\START_CLIPAGENT.bat"
for /l %%i in (1,1,60) do (
  timeout /t 1 /nobreak >nul
  curl.exe -s -f -o nul -m 2 http://127.0.0.1:8000/api/config && goto open
)
echo.
echo   ClipAgent is taking longer than usual to start. Check the "ClipAgent Studio" window for errors.
pause
exit /b

:open
start "" http://localhost:8000
exit /b
