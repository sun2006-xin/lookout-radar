@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem 双击运行，不需要敲任何命令。
rem 用 python.exe 而不是 pythonw.exe：出问题时控制台能看到报错，好排查。

if not exist ".venv\Scripts\python.exe" (
    echo [x] 没找到虚拟环境 .venv
    echo.
    echo     请先双击运行「1-安装依赖.bat」
    echo.
    pause
    exit /b 1
)

echo 正在启动 Lookout ...
".venv\Scripts\python.exe" main.py
if errorlevel 1 (
    echo.
    echo [x] 程序异常退出，错误码 %errorlevel%
    echo     日志在 data\radarscope.log
    echo.
    pause
)
endlocal
