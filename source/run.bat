@echo off
chcp 65001 >nul
setlocal EnableExtensions

REM 优先使用本项目构建时创建的独立 Python 环境，保持系统 Python 不受影响。  
set "PROJECT=%~dp0"
for %%I in ("%PROJECT%..") do set "ROOT=%%~fI"
set "PYTHON=%ROOT%\.build\venv\Scripts\python.exe"
if exist "%PYTHON%" (
    "%PYTHON%" "%PROJECT%src\main.py" %*
) else (
    REM 未构建时，允许开发者使用已经安装 PySide6 与 Pillow 的 Python 直接启动。  
    python "%PROJECT%src\main.py" %*
)
if errorlevel 1 (
    echo [失败] 请先运行 build.bat 安装依赖，或检查上方错误信息。  
    pause
)
