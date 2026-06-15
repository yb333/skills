@echo off
chcp 65001 >nul 2>&1
setlocal EnableDelayedExpansion

REM ==========================================
REM  DWS Skills 安装器 (Windows .bat)
REM  双击即可执行，无需 PowerShell
REM ==========================================

set "SCRIPT_DIR=%~dp0"
set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"

echo ==========================================
echo   DWS Skills 安装器 (Windows)
echo ==========================================
echo.

REM ── 1. 扫描可用 skill ──
echo [1/4] 扫描可用 skill...
set "SKILL_COUNT=0"
set "SKILL_LIST="
for /d %%D in ("%SCRIPT_DIR%\*") do (
    if exist "%%D\SKILL.md" (
        set /a SKILL_COUNT+=1
        for %%F in ("%%D") do set "SKILL_NAME=%%~nxF"
        set "SKILL_LIST=!SKILL_LIST! !SKILL_NAME!"
    )
)

if %SKILL_COUNT%==0 (
    echo   未找到任何 skill（需要 SKILL.md 文件）
    exit /b 1
)
echo   找到 %SKILL_COUNT% 个 skill:!SKILL_LIST!
echo.

REM ── 2. 检测 Python（Windows 没有 python3）──
echo [2/4] 检测 Python...
set "PYTHON="

REM 尝试 py launcher（官方安装器自带）
py -3 -c "import sys; exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
if !errorlevel!==0 (
    set "PYTHON=py -3"
    goto :found_python
)

REM 尝试 python（勾选了 Add to PATH 时可用）
python -c "import sys; exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
if !errorlevel!==0 (
    set "PYTHON=python"
    goto :found_python
)

echo   未找到 Python 3.10+
echo.
echo   请安装 Python 3.10+:
echo     方式 1: https://www.python.org/downloads/  下载安装
echo     方式 2: winget install Python.Python.3.12
echo.
echo   安装时务必勾选 "Add Python to PATH"
pause
exit /b 1

:found_python
for /f "delims=" %%v in ('!PYTHON! --version 2^>^&1') do set "PY_VERSION=%%v"
echo   !PYTHON! (!PY_VERSION!)
echo.

REM ── 3. 创建 venv + 安装依赖 ──
echo [3/4] 创建虚拟环境 + 安装依赖...
set "CONFIG_DIR=%USERPROFILE%\.config\opencode"
set "VENV_DIR=%CONFIG_DIR%\venv"
set "SKILLS_DIR=%CONFIG_DIR%\skills"
set "COMMANDS_DIR=%CONFIG_DIR%\commands"

if not exist "%VENV_DIR%" (
    !PYTHON! -m venv "%VENV_DIR%"
)
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"

REM 汇总依赖
set "ALL_REQS="
for /d %%D in ("%SCRIPT_DIR%\*") do (
    if exist "%%D\requirements.txt" (
        for /f "eol=# tokens=*" %%L in (%%D\requirements.txt) do (
            set "LINE=%%L"
            if not "!LINE!"=="" set "ALL_REQS=!ALL_REQS! !LINE!"
        )
    )
)

if not "!ALL_REQS!"=="" (
    "!VENV_PY!" -m pip install --upgrade pip --quiet >nul 2>&1
    "!VENV_PY!" -m pip install !ALL_REQS! --quiet
    if !errorlevel!==0 (
        echo   已安装:!ALL_REQS!
    ) else (
        echo   依赖安装失败，请手动执行: "!VENV_PY!" -m pip install!ALL_REQS!
        pause
        exit /b 1
    )
)
echo.

REM ── 4. 复制 skill + 命令 ──
echo [4/4] 安装 skill 文件...
if not exist "%SKILLS_DIR%" mkdir "%SKILLS_DIR%"
if not exist "%COMMANDS_DIR%" mkdir "%COMMANDS_DIR%"

for /d %%D in ("%SCRIPT_DIR%\*") do (
    if exist "%%D\SKILL.md" (
        for %%F in ("%%D") do set "SNAME=%%~nxF"
        set "DEST=%SKILLS_DIR%\!SNAME!"
        if exist "!DEST!" rmdir /s /q "!DEST!"
        mkdir "!DEST!"
        xcopy "%%D\*" "!DEST!\" /e /i /q /y >nul 2>&1
        REM 清理 __pycache__
        if exist "!DEST!\references\__pycache__" rmdir /s /q "!DEST!\references\__pycache__"
        echo   !SNAME!
    )
)

REM 复制命令
if exist "%SCRIPT_DIR%\commands" (
    for %%F in ("%SCRIPT_DIR%\commands\*.md") do (
        copy "%%F" "%COMMANDS_DIR%\%%~nxF" >nul 2>&1
        echo   命令: %%~nxF
    )
)

REM ── 完成 ──
echo.
echo ==========================================
echo   安装完成！
echo ==========================================
echo.
echo Python 依赖:!ALL_REQS!
echo 安装位置: %SKILLS_DIR%
echo.
echo 在 opencode 中使用:
echo   /analyze @execution_tasks.xlsx
echo.
pause
