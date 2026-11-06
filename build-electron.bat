@echo off
cd /d "%~dp0"
chcp 65001 >nul

REM ============================================================
REM CClear 构建脚本
REM 1. 后端 PyInstaller
REM 2. Electron NSIS 安装包
REM 3. Electron 便携版 zip（手动打包 win-unpacked 目录）
REM ============================================================

where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 未检测到 Python，请先安装 Python 3.11+ 并加入 PATH。
  pause
  exit /b 1
)

where node >nul 2>nul
if errorlevel 1 (
  echo [错误] 未检测到 Node.js，请先安装 Node.js 18+ 并加入 PATH。
  pause
  exit /b 1
)

echo ============================================================
echo [1/3] 正在构建后端 (PyInstaller) ...
echo ============================================================
python -m PyInstaller --noconfirm --clean CClear.spec
if errorlevel 1 (
    echo 后端构建失败
    pause
    exit /b 1
)
echo 后端构建完成: dist\CClear-backend.exe
echo.

echo ============================================================
echo [2/3] 正在构建安装版 (NSIS) ...
echo ============================================================
cd electron
if not exist node_modules (
    echo 首次运行，正在安装 Electron 依赖...
    call npm install
    if errorlevel 1 (
        echo 依赖安装失败
        cd ..
        pause
        exit /b 1
    )
)
call npx electron-builder --win nsis --publish never
if errorlevel 1 (
    echo 安装版构建失败
    cd ..
    pause
    exit /b 1
)
echo 安装版构建完成: electron\dist\CClear-Setup-%version%.exe
echo.

echo ============================================================
echo [3/3] 正在构建便携版 (zip) ...
echo ============================================================
powershell -NoProfile -Command "Compress-Archive -Path 'electron\dist\win-unpacked\*' -DestinationPath 'electron\dist\CClear-Portable-%version%.zip' -Force"
if errorlevel 1 (
    echo 便携版 zip 打包失败
    pause
    exit /b 1
)
echo 便携版构建完成: electron\dist\CClear-Portable-%version%.zip
echo.

echo ============================================================
echo 构建完成！
echo   安装版: electron\dist\CClear-Setup-%version%.exe
echo   便携版: electron\dist\CClear-Portable-%version%.zip
echo ============================================================
pause
