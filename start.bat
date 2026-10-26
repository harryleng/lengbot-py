@echo off
REM ============================================================
REM  lengbot-py launcher
REM  Docs : http://127.0.0.1:18080/docs
REM  Usage: start.bat          (default port 18080)
REM         start.bat 18081    (custom port)
REM ============================================================
cd /d "%~dp0"

if not exist ".env" (
  echo [INFO] .env not found, copying from .env.example
  copy ".env.example" ".env" >nul
)

set PORT=18080
if not "%1"=="" set PORT=%1

REM Pre-check the port. Windows sometimes reports WinError 10013 instead of
REM 10048 when the port is taken, which looks like a permission problem.
netstat -ano | findstr ":%PORT%" | findstr "LISTENING" >nul
if not errorlevel 1 (
  echo.
  echo [ERROR] Port %PORT% is already in use. Occupied by:
  netstat -ano ^| findstr ":%PORT%"
  echo.
  echo   Fix A: taskkill /F /IM python.exe    (kill leftover servers)
  echo   Fix B: start.bat 18081               (use another port)
  echo.
  pause
  exit /b 1
)

echo Starting uvicorn on port %PORT% ...
echo API docs: http://127.0.0.1:%PORT%/docs
echo Press Ctrl+C to stop.
echo.
uv run uvicorn app.main:app --reload --port %PORT%

echo.
echo Server stopped.
pause
