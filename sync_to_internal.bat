@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul 2>&1

REM ============================================================
REM sync_to_internal.bat — 一键同步外网代码到内网 git 仓库
REM
REM 用法（在内网 Windows 电脑上运行）：
REM   sync_to_internal.bat                          REM 用默认配置
REM   sync_to_internal.bat D:\path\to\repo          REM 指定内网仓库路径
REM   sync_to_internal.bat --config D:\path\to\repo REM 设置默认路径（记住）
REM
REM 自动完成：
REM   1. git clone --depth 1 外网GitHub最新代码
REM   2. robocopy 同步成品文件到内网仓库（排除开发文件）
REM   3. 在内网仓 git add + commit + push 到内网远端
REM ============================================================

REM ── 配置 ──
set "EXTERNAL_REPO=https://github.com/yb333/analyzer-agent.git"
set "CONFIG_FILE=%USERPROFILE%\.analyzer-agent-sync.conf"

REM 读取配置文件（记住上次设置的内网仓库路径）
if exist "%CONFIG_FILE%" (
    set /p INTERNAL_REPO=<"%CONFIG_FILE%"
)

REM 命令行参数处理
if "%~1"=="--config" (
    if not "%~2"=="" (
        echo %~2>"%CONFIG_FILE%"
        echo [OK] 已保存内网仓库路径: %~2
        echo 以后直接运行 sync_to_internal.bat 即可，不用再指定路径。
        goto :eof
    )
)

REM 覆盖默认路径
if not "%~1"=="" (
    set "INTERNAL_REPO=%~1"
)

REM 检查内网仓库路径
if "!INTERNAL_REPO!"=="" (
    echo [ERROR] 未指定内网 git 仓库路径
    echo.
    echo 用法：
    echo   首次使用先配置：sync_to_internal.bat --config D:\path\to\internal\repo
    echo   后续直接运行：sync_to_internal.bat
    goto :eof
)

if not exist "!INTERNAL_REPO!\.git" (
    echo [ERROR] 不是 git 仓库: !INTERNAL_REPO!
    echo 请确认路径正确，且该目录已 git init 或 clone 自内网远端
    goto :eof
)

echo ============================================================
echo   同步外网代码 - 内网 git 仓库
echo ============================================================
echo 外网仓库: %EXTERNAL_REPO%
echo 内网仓库: !INTERNAL_REPO!
echo.

REM ── Step 1: 从外网拉最新代码 ──
set "TEMP_DIR=%TEMP%\analyzer-agent-sync-%RANDOM%"
echo [Step 1] 拉取外网最新代码...
git clone --depth 1 %EXTERNAL_REPO% "%TEMP_DIR%" 2>&1
if not exist "%TEMP_DIR%\.git" (
    echo [ERROR] clone 失败，请检查网络或仓库地址
    goto :cleanup
)
echo.

REM ── Step 2: robocopy 同步到内网仓（排除开发文件，保留 .git）──
echo [Step 2] 同步到内网仓库...

robocopy "%TEMP_DIR%" "!INTERNAL_REPO!" /MIR /NFL /NDL /NP /NJH /NJS ^
    /XD tests docs release dev __pycache__ .pytest_cache .git telemetry-server ^
    /XF architecture.md sync_to_internal.sh sync_to_internal.bat start_telemetry.sh start_telemetry.bat stop_telemetry.bat sample_rule.yml .gitignore .DS_Store 2>nul

if !errorlevel! GTR 7 (
    echo [ERROR] robocopy 失败
    goto :cleanup
)
echo   + 同步完成

REM 清理垃圾文件
for /d /r "!INTERNAL_REPO!" %%D in (__pycache__) do (
    if exist "%%D" rmdir /s /q "%%D" 2>nul
)
del /s /q "!INTERNAL_REPO!\*.pyc" 2>nul
del /s /q "!INTERNAL_REPO!\.DS_Store" 2>nul

echo.

REM ── Step 3: 在内网仓 git commit + push ──
echo [Step 3] 提交到内网 git...
pushd "!INTERNAL_REPO!"

REM 取外网最新 commit 信息（用于提交 message）
pushd "%TEMP_DIR%"
for /f "delims=" %%H in ('git log -1 --format^="%%s"') do set "COMMIT_MSG=%%H"
for /f "delims=" %%H in ('git rev-parse --short HEAD') do set "COMMIT_HASH=%%H"
popd

git add -A

REM 检查有没有变更
git diff --cached --quiet
if !errorlevel! equ 0 (
    echo   无变更，内容已是最新。
) else (
    echo   提交信息: !COMMIT_MSG!
    git commit -m "!COMMIT_MSG! (!COMMIT_HASH!)" 2>&1
    echo.
    echo [Step 4] 推送到内网远端...
    git push 2>&1
    echo.
    echo ============================================================
    echo   同步完成: !COMMIT_HASH! !COMMIT_MSG!
    echo ============================================================
)
popd

:cleanup
if exist "%TEMP_DIR%" rmdir /s /q "%TEMP_DIR%" 2>nul
echo.
echo 按任意键关闭...
pause >nul
