@echo off
setlocal EnableExtensions

REM ============================================================
REM OpenCode Remembering - PostgreSQL HTTP RPC setup
REM ============================================================

set "PSQL=E:\Program Files\PostgreSQL\16\bin\psql.exe"
set "SQL_FILE=C:\Projects\opencode-remembering\sql\remembering-http-v1.sql"
set "MEMORY_BASELINE_DSN=postgresql://postgres:werewolf@localhost:5432/memory_baseline"

echo.
echo ============================================================
echo OpenCode Remembering - PostgreSQL RPC Setup
echo ============================================================
echo.

if not exist "%PSQL%" (
    echo [ERROR] psql.exe not found:
    echo         %PSQL%
    exit /b 1
)

if not exist "%SQL_FILE%" (
    echo [ERROR] SQL file not found:
    echo         %SQL_FILE%
    exit /b 1
)

echo [1/4] Checking PostgreSQL connection...
"%PSQL%" --dbname="%MEMORY_BASELINE_DSN%" --set=ON_ERROR_STOP=1 --command="SELECT current_database() AS database, version();"

if errorlevel 1 (
    echo.
    echo [ERROR] Could not connect to PostgreSQL.
    exit /b 1
)

echo.
echo [2/4] Installing Remembering HTTP RPC functions...
"%PSQL%" --dbname="%MEMORY_BASELINE_DSN%" --set=ON_ERROR_STOP=1 --file="%SQL_FILE%"

if errorlevel 1 (
    echo.
    echo [ERROR] SQL installation failed.
    exit /b 1
)

echo.
echo [3/4] Verifying PostgreSQL extensions...
"%PSQL%" --dbname="%MEMORY_BASELINE_DSN%" --set=ON_ERROR_STOP=1 --command="SELECT extname, extversion FROM pg_extension WHERE extname IN ('vector','pg_trgm') ORDER BY extname;"

if errorlevel 1 (
    echo.
    echo [ERROR] Extension verification failed.
    exit /b 1
)

echo.
echo [4/4] Verifying Remembering RPC functions...
"%PSQL%" --dbname="%MEMORY_BASELINE_DSN%" --set=ON_ERROR_STOP=1 --command="SELECT count(*) AS remembering_rpc_count FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public' AND p.proname LIKE 'remembering_%%';"

if errorlevel 1 (
    echo.
    echo [ERROR] RPC verification failed.
    exit /b 1
)

echo.
echo Installed RPC functions:
"%PSQL%" --dbname="%MEMORY_BASELINE_DSN%" --set=ON_ERROR_STOP=1 --command="SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public' AND p.proname LIKE 'remembering_%%' ORDER BY p.proname;"

if errorlevel 1 (
    echo.
    echo [ERROR] Could not list RPC functions.
    exit /b 1
)

echo.
echo ============================================================
echo SUCCESS
echo ============================================================
echo Remembering PostgreSQL RPC functions are installed.
echo.
echo Database:
echo   memory_baseline
echo.
echo Next step:
echo   Start PostgREST and point it at this same database.
echo.
echo Expected REST health endpoint after PostgREST is running:
echo   POST http://localhost:3000/rpc/remembering_health
echo.
pause

endlocal
