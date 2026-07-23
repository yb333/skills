@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul 2>&1

REM ============================================================
REM sync_to_internal.bat — 一键同步外网代码到内网 git 仓库
REM
REM 原理：clone 外网仓 → 删掉开发文件 → git push --force 到内网仓
REM 用 git 自己处理 diff，不用 robocopy，不会"看不出变化"
REM
REM 用法（在内网 Windows 电脑上运行）：
REM   sync_to_internal.bat                          REM 用默认配置
REM   sync_to_internal.bat D:\path\to\repo          REM 指定内网仓库路径
REM   sync_to_internal.bat --config D:\path\to\repo REM 设置默认路径（记住）
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

echo ═══════════════════════════════════════════════
echo   同步外网代码 → 内网 git 仓库
echo ═══════════════════════════════════════════════
echo 外网仓库: %EXTERNAL_REPO%
echo 内网仓库: !INTERNAL_REPO!
echo.

REM ── Step 1: 从外网 clone 最新代码（完整克隆，不用 --depth 1）──
set "TEMP_DIR=%TEMP%\analyzer-agent-sync-%RANDOM%"
echo [Step 1] 拉取外网最新代码...
git clone %EXTERNAL_REPO% "%TEMP_DIR%" 2>&1
if not exist "%TEMP_DIR%\.git" (
    echo [ERROR] clone 失败，请检查网络或仓库地址
    goto :cleanup
)
echo.

REM ── Step 2: 删掉不该给用户的文件 ──
echo [Step 2] 清理开发文件...
pushd "%TEMP_DIR%"
for /d %%D in (tests docs release telemetry-server) do (
    if exist "%%D" rmdir /s /q "%%D" 2>nul
)
if exist architecture.md del /q architecture.md 2>nul
if exist sync_to_internal.sh del /q sync_to_internal.sh 2>nul
if exist sync_to_internal.bat del /q sync_to_internal.bat 2>nul
if exist start_telemetry.sh del /q start_telemetry.sh 2>nul
if exist start_telemetry.bat del /q start_telemetry.bat 2>nul
if exist stop_telemetry.bat del /q stop_telemetry.bat 2>nul
if exist sample_rule.yml del /q sample_rule.yml 2>nul
if exist .gitignore del /q .gitignore 2>nul
for /d /r "%TEMP_DIR%" %%D in (__pycache__) do (
    if exist "%%D" rmdir /s /q "%%D" 2>nul
)
del /s /q "%TEMP_DIR%\*.pyc" 2>nul
del /s /q "%TEMP_DIR%\\.DS_Store" 2>nul

REM 提交这些删除（让 git 记录变化）
git add -A
git commit -m "清理开发文件（同步前预处理）" --allow-empty 2>nul

REM 取外网最新 commit 信息（用于显示）
for /f "delims=" %%H in ('git log -1 --format^="%%s"') do set "COMMIT_MSG=%%H"
for /f "delims=" %%H in ('git rev-parse --short HEAD') do set "COMMIT_HASH=%%H"
echo   外网最新: !COMMIT_HASH! !COMMIT_MSG!
popd
echo.

REM ── Step 3: 直接 git push 到内网仓 ──
echo [Step 3] 推送到内网仓库...
pushd "%TEMP_DIR%"

REM 添加内网仓为 remote
git remote add internal "!INTERNAL_REPO!" 2>nul
if !errorlevel! neq 0 (
    git remote set-url internal "!INTERNAL_REPO!"
)

REM 检测当前分支名（外网是 main，内网可能是 master）
for /f "delims=" %%B in ('git rev-parse --abbrev-ref HEAD') do set "BRANCH=%%B"
echo   外网分支: !BRANCH!

REM 检测内网仓的分支名
set "INTERNAL_BRANCH=master"
pushd "!INTERNAL_REPO!"
for /f "delims=" %%B in ('git rev-parse --abbrev-ref HEAD 2^>nul') do set "INTERNAL_BRANCH=%%B"
popd
echo   内网分支: !INTERNAL_BRANCH!
echo.

REM 允许 push 到非 bare 仓的当前分支（否则报 branch is currently checked out）
pushd "!INTERNAL_REPO!"
git config receive.denyCurrentBranch updateInstead 2>nul
popd

REM 直接 force push（以外网为准，不 pull 不 merge，避免冲突）
git push --force internal !BRANCH!:!INTERNAL_BRANCH! 2>&1
if !errorlevel! neq 0 (
    echo   [ERROR] push 失败
    popd
    goto :cleanup
)

popd
echo.
echo ═══════════════════════════════════════════════
echo   同步完成
echo   !COMMIT_HASH! !COMMIT_MSG!
echo ═══════════════════════════════════════════════

:cleanup
if exist "%TEMP_DIR%" rmdir /s /q "%TEMP_DIR%" 2>nul
echo.
echo 按任意键关闭...
pause >nul
