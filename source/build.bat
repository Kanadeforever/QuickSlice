@echo off
chcp 65001 >nul
setlocal EnableExtensions

REM 本脚本从当前源码项目定位发行包根目录，不依赖固定磁盘或编译器安装位置。  
set "PROJECT=%~dp0"
for %%I in ("%PROJECT%..") do set "ROOT=%%~fI"
set "VENV=%ROOT%\.build\venv"
set "PYTHON=%VENV%\Scripts\python.exe"

REM 所有虚拟环境、构建中间文件都放入发行包的 .build 目录。  
if not exist "%ROOT%\.build" mkdir "%ROOT%\.build"
if not exist "%ROOT%\release" mkdir "%ROOT%\release"
if not exist "%PYTHON%" (
    echo [准备] 创建独立 Python 虚拟环境。  
    py -3.11-64 -m venv "%VENV%" >nul 2>&1
    if errorlevel 1 (
        python -m venv "%VENV%"
        if errorlevel 1 goto :failed
    )
)

REM 若事先放入合法 wheel 文件，则纯离线安装；否则尝试使用系统配置的 Python 软件包源。  
if exist "%PROJECT%tools\wheels\*.whl" (
    echo [依赖] 使用 tools\wheels 内的本地离线依赖。  
    "%PYTHON%" -m pip install --disable-pip-version-check --no-index --find-links "%PROJECT%tools\wheels" -r "%PROJECT%requirements.txt"
) else (
    echo [依赖] 本地没有 wheel 缓存，尝试安装固定版本依赖。  
    "%PYTHON%" -m pip install --disable-pip-version-check -r "%PROJECT%requirements.txt"
)
if errorlevel 1 goto :failed

REM 先执行无需启动 Qt 窗口的无损像素测试，失败则不允许生成可执行程序。  
echo [测试] 校验切割模型与原图像素完整性。  
"%PYTHON%" -m unittest discover -s "%PROJECT%tools" -p "test_*.py" -v
if errorlevel 1 goto :failed

REM 使用 onedir 避免 onefile 每次启动解包；图标同时用于 EXE 资源和程序窗口。  
echo [构建] 正在生成 Windows x64 便携目录版。  
"%PYTHON%" -m PyInstaller --noconfirm --clean --onedir --windowed --name QuickSlice --icon "%PROJECT%assets\quickslice.ico" --add-data "%PROJECT%assets;assets" --distpath "%ROOT%\release" --workpath "%ROOT%\.build\pyinstaller" --specpath "%ROOT%\.build" "%PROJECT%src\main.py"
if errorlevel 1 goto :failed
if not exist "%ROOT%\release\QuickSlice\QuickSlice.exe" goto :failed

REM 只删除一次性生成的工作缓存，保留独立虚拟环境以便下次本地离线构建。  
if exist "%ROOT%\.build\pyinstaller" rmdir /s /q "%ROOT%\.build\pyinstaller"
echo.
echo [完成] 可执行程序：%ROOT%\release\QuickSlice\QuickSlice.exe  
pause
exit /b 0

:failed
echo.
echo [失败] 构建或测试失败，未声称已经生成可用的 EXE。  
pause
exit /b 1
