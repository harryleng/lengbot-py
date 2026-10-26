@echo off
REM ============================================================
REM  Create a standalone copy of the remote database (lengbot_py)
REM  The production DB "lengbot" is only READ, never written.
REM
REM  Usage: set PGPASSWORD=yourpassword  ^&^&  create_remote_copy.bat
REM ============================================================

set PGBIN=C:\PostgreSQL\15\pgsql\bin
set PGHOST=localhost
set PGPORT=15432
set PGUSER=postgres
set SRC=lengbot
set DST=lengbot_py
set DUMP=%~dp0lengbot_seed.dump

if "%PGPASSWORD%"=="" (
  echo [ERROR] PGPASSWORD is not set.
  echo         Run:  set PGPASSWORD=yourpassword
  pause
  exit /b 1
)

if not exist "%DUMP%" (
  echo [ERROR] dump file not found: %DUMP%
  pause
  exit /b 1
)

echo [1/4] Checking whether %DST% exists ...
%PGBIN%\psql.exe -h %PGHOST% -p %PGPORT% -U %PGUSER% -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='%DST%'" > "%TEMP%\lengbot_py_exists.txt"
set /p EXISTS=<"%TEMP%\lengbot_py_exists.txt"
if "%EXISTS%"=="1" (
  echo       Already exists, skip creating. To rebuild, run manually:
  echo       DROP DATABASE %DST%;
  goto restore
)

echo       Creating empty database %DST% ...
%PGBIN%\createdb.exe -h %PGHOST% -p %PGPORT% -U %PGUSER% -E UTF8 -T template0 %DST%
if errorlevel 1 (
  echo [ERROR] createdb failed
  pause
  exit /b 1
)

:restore
echo [2/4] Restoring data (about 8 MB, 10-30 seconds) ...
%PGBIN%\pg_restore.exe -h %PGHOST% -p %PGPORT% -U %PGUSER% -d %DST% --no-owner --no-privileges "%DUMP%"
echo       NOTE: warnings about existing roles/schemas can be ignored.

echo [3/4] Verifying row counts ...
%PGBIN%\psql.exe -h %PGHOST% -p %PGPORT% -U %PGUSER% -d %DST% -c "SELECT (SELECT count(*) FROM chat_session) AS sessions, (SELECT count(*) FROM message) AS messages, (SELECT extversion FROM pg_extension WHERE extname='vector') AS pgvector;"

echo [4/4] Done. Set lengbot-py/.env DATABASE_URL to:
echo       postgresql+asyncpg://postgres:YOUR_PASSWORD@localhost:15432/lengbot_py
echo.
echo Production DB "lengbot" was read-only, no writes were made.
pause
