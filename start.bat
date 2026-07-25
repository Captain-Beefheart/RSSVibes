@echo off
setlocal
rem --- Local RSS Dashboard launcher ---------------------------------
set "PY=C:\msys64\mingw64\bin\python.exe"
if not exist "%PY%" set "PY=python"

echo.
echo   Starting Local RSS Dashboard...
echo   Your browser will open at http://127.0.0.1:8787/
echo   Close this window (or press Ctrl+C) to stop the dashboard.
echo.

rem open the browser a moment after the server starts
start "" cmd /c "timeout /t 1 >nul & start "" http://127.0.0.1:8787/"

"%PY%" "%~dp0server.py"
pause
