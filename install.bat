@echo off
cd /d "%~dp0"

REM 找 Python（先试 python，再试 py launcher）
REM 很多 Windows 用户有 python 但没有 py launcher
python --version >nul 2>&1
if not errorlevel 1 goto run_python

py -3 --version >nul 2>&1
if not errorlevel 1 goto run_py3

echo.
echo ==========================================
echo   未找到 Python 3.10+
echo ==========================================
echo.
echo   请安装: winget install Python.Python.3.12
echo   或 https://www.python.org/downloads/
echo   安装时勾选 "Add Python to PATH"
echo.
pause
exit /b 1

:run_python
python install.py %*
goto finish

:run_py3
py -3 install.py %*
goto finish

:finish
if errorlevel 1 pause
