@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem 第一次使用跑一次这个。之后双击「启动.bat」即可。
rem 会自动找 Python：优先 py 启动器，其次 python。

echo === Lookout 依赖安装 ===
echo.

set PY=
where py >nul 2>nul && set PY=py -3
if "%PY%"=="" (
    where python >nul 2>nul && set PY=python
)
if "%PY%"=="" (
    echo [x] 没找到 Python。请先安装 Python 3.9 以上版本，安装时勾选 "Add to PATH"。
    echo     下载： https://www.python.org/downloads/
    pause
    exit /b 1
)

echo 使用的 Python：%PY%
%PY% --version
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [1/3] 创建虚拟环境 .venv ...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo [x] 创建虚拟环境失败。
        pause
        exit /b 1
    )
) else (
    echo [1/3] 虚拟环境已存在，跳过
)

echo [2/3] 升级 pip ...
".venv\Scripts\python.exe" -m pip install --upgrade pip -q

echo [3/3] 安装依赖（PyQt5 / numpy / pyserial），首次约 60MB ...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [x] 依赖安装失败。如果是网络问题，可加国内镜像：
    echo     .venv\Scripts\python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
    pause
    exit /b 1
)

echo.
echo === 安装完成，双击「启动.bat」即可运行 ===
pause
endlocal
