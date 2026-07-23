@echo off
chcp 65001 >nul 2>&1
echo Stopping Analyzer Agent Telemetry Server...

:: 方式1: 通过端口 3000 找到进程并终止
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":3000.*LISTENING"') do (
    taskkill /f /pid %%a >nul 2>&1
)

:: 方式2: 按 server.js 命令行匹配 node 进程（只杀本项目的，不影响其他 node）
wmic process where "commandline like '%%server.js%%' and name='node.exe'" call terminate >nul 2>&1

:: 验证是否已停止
timeout /t 1 /nobreak >nul
netstat -aon | findstr ":3000.*LISTENING" >nul 2>&1
if %errorlevel% neq 0 (
    echo Telemetry Server stopped.
) else (
    echo Port 3000 still in use, may need manual cleanup:
    netstat -aon | findstr ":3000.*LISTENING"
)
echo.
pause
