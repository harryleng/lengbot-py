@echo off
REM ============================================================
REM  lengbot-py frontend (vite dev server, port 5175)
REM  Backend must be running first: start.bat (port 18080)
REM  Page: http://localhost:5175
REM ============================================================
cd /d "%~dp0ui"

if not exist "package.json" (
  echo [ERROR] ui\package.json not found. Run this from lengbot-py root.
  pause
  exit /b 1
)

netstat -ano | findstr ":5175" | findstr "LISTENING" >nul
if not errorlevel 1 (
  echo.
  echo [ERROR] Port 5175 is already in use. Occupied by:
  netstat -ano ^| findstr ":5175"
  echo.
  echo   Fix: taskkill /F /IM node.exe
  echo.
  pause
  exit /b 1
)

if not exist "node_modules" (
  echo [INFO] node_modules missing, running pnpm install ...
  call pnpm install
)

echo Starting vite dev server on 5175 ...
echo Page: http://localhost:5175
echo Press Ctrl+C to stop.
echo.
call pnpm dev

echo.
echo Frontend stopped.
pause
