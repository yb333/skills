@echo off
chcp 65001 >nul 2>&1
setlocal enabledelayedexpansion

REM ============================================================
REM start_telemetry.bat - Analyzer Agent Telemetry Server
REM
REM Double-click to run. Zero dependencies (uses Node built-in sqlite).
REM Requires Node.js v22.5+ (v24 recommended).
REM ============================================================

cd /d "%~dp0telemetry-server"

echo ==========================================
echo   Analyzer Agent Telemetry Server
echo ==========================================
echo.

REM --- Check Node.js ---
where node >nul 2>&1
if !errorlevel! neq 0 (
    echo [ERROR] Node.js not found. Install v22.5+ from https://nodejs.org/
    echo.
    pause
    exit /b 1
)
for /f "delims=" %%v in ('node -v') do set "NODE_VER=%%v"
echo [OK] Node.js: !NODE_VER!

REM --- Check Node version >= 22 ---
for /f "tokens=1 delims=." %%a in ("!NODE_VER:v=! ") do set "NODE_MAJOR=%%a"
set "NODE_MAJOR=!NODE_VER:v=!"
set "NODE_MAJOR=!NODE_MAJOR:~0,2!"
if !NODE_MAJOR! LSS 22 (
    echo [ERROR] Node.js !NODE_VER! is too old. Need v22.5+ for built-in sqlite.
    echo Download from https://nodejs.org/
    echo.
    pause
    exit /b 1
)
echo.

REM --- Start server ---
echo Starting server on port 3000...
echo.
echo   Dashboard: http://localhost:3000/
echo   Endpoint:  http://YOUR_IP:3000/api/usage
echo   Stats:     http://localhost:3000/api/stats
echo.
echo   Press Ctrl+C to stop.
echo ==========================================
echo.

REM Open browser after 3s (background, non-blocking)
start "" /b cmd /c "timeout /t 3 /nobreak >nul && start http://localhost:3000/"

REM Start node (foreground)
node server.js

echo.
echo Server stopped.
pause
