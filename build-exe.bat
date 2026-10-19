@echo off
chcp 65001 >nul
cd /d "%~dp0"
title CClear - 打包 EXE

set PY=python

where %PY% >nul 2>nul
if errorlevel 1 (
  echo [错误] 未检测到 Python，请先安装 Python 3.9+ 并加入 PATH。
  pause
  exit /b 1
)

%PY% -m pip show pyinstaller >nul 2>nul
if errorlevel 1 (
  echo 正在安装 PyInstaller ...
  %PY% -m pip install pyinstaller
  if errorlevel 1 (
    echo PyInstaller 安装失败。
    pause
    exit /b 1
  )
)

%PY% -m pip show pywebview >nul 2>nul
if errorlevel 1 (
  echo 正在安装运行依赖 ...
  %PY% -m pip install -r requirements.txt
  if errorlevel 1 (
    echo 依赖安装失败。
    pause
    exit /b 1
  )
)

echo 正在打包，约 1-2 分钟 ...
%PY% -m PyInstaller --noconfirm --clean CClear.spec
if errorlevel 1 (
  echo.
  echo 打包失败，请查看上面的错误信息。
  pause
  exit /b 1
)

echo.
echo 打包完成: dist\CClear.exe
echo 双击即可运行，界面在独立窗口中打开，不需要浏览器。
echo 需要管理员权限时，请右键 dist\CClear.exe 选择“以管理员身份运行”。
pause
