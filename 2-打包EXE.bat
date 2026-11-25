@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem 重新打包成 exe。产物在 dist\Lookout\ 下。
rem 注意：先关掉正在运行的 RadarScope，否则文件被占用会打包失败。

if not exist ".venv\Scripts\python.exe" (
    echo [x] 没找到虚拟环境，请先运行「1-安装依赖.bat」
    pause
    exit /b 1
)

echo [1/4] 检查 PyInstaller 与运行状态 ...
tasklist /fi "imagename eq Lookout.exe" 2>nul | find /i "Lookout.exe" >nul
if not errorlevel 1 (
    echo [x] Lookout 正在运行，文件被占用，无法替换。
    echo     请先关掉它（包括托盘里的进程）再重新打包。
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -c "import PyInstaller" 2>nul
if errorlevel 1 (
    echo       未安装，正在安装 ...
    ".venv\Scripts\python.exe" -m pip install pyinstaller
    if errorlevel 1 (
        echo [x] PyInstaller 安装失败
        pause
        exit /b 1
    )
)

echo [2/4] 打包中，约需 1 分钟 ...
rem 先把旧产物改名而不是让打包器自己删：有些安全软件会拦批量删除，导致中途失败
if exist "dist\Lookout" (
    if exist "dist_prev" rmdir /s /q "dist_prev"
    move /y "dist\Lookout" "dist_prev" >nul
    if errorlevel 1 (
        echo [x] 旧产物改名失败，多半还是被占用。关掉 RadarScope 再试。
        pause
        exit /b 1
    )
)
".venv\Scripts\pyinstaller.exe" --noconfirm --clean --windowed --name Lookout --collect-submodules radarscope main.py
if errorlevel 1 (
    echo [x] 打包失败，看上面的报错
    pause
    exit /b 1
)

echo [3/4] 复制插件目录 ...
if exist "dist\Lookout\plugins" rmdir /s /q "dist\Lookout\plugins"
xcopy "plugins" "dist\Lookout\plugins" /e /i /q /y >nul
if exist "dist_prev" rmdir /s /q "dist_prev"

echo [4/4] 验证打包结果（无窗口自检）...
"dist\Lookout\Lookout.exe" --selftest
if errorlevel 1 (
    echo.
    echo [x] 自检失败！这个 exe 可能在别的机器上跑不起来。
    echo     详情看 dist\Lookout\data\radarscope.log
    pause
    exit /b 1
)
if exist "dist\Lookout\data" rmdir /s /q "dist\Lookout\data"

echo.
echo === 完成，自检通过 ===
echo 程序目录： %cd%\dist\Lookout
echo 整个文件夹拷给别人就能跑，不用装 Python。
pause
endlocal
