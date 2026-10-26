@echo off
chcp 65001 >nul
cd /d "%~dp0"
title CClear - 打包 Electron 安装包

where node >nul 2>nul
if errorlevel 1 (
  echo [错误] 未检测到 Node.js，请先安装 Node.js 18+ 并加入 PATH。
  pause
  exit /b 1
)

rem electron-builder 的 extraResources 依赖 PyInstaller 产出的后端
if not exist dist\CClear-backend.exe (
  echo [错误] 未找到 dist\CClear-backend.exe，请先运行 build-exe.bat 打包后端。
  pause
  exit /b 1
)

cd electron
if not exist node_modules (
  echo 首次运行，正在安装 Electron 依赖...
  call npm install
  if errorlevel 1 (
    echo 依赖安装失败。
    cd ..
    pause
    exit /b 1
  )
)

echo 正在打包 Electron 安装包 ...
call npm run dist
if errorlevel 1 (
  echo.
  echo 打包失败，请查看上面的错误信息。
  cd ..
  pause
  exit /b 1
)

cd ..
echo.
echo 打包完成: electron\dist\CClear-Setup-*.exe
echo 双击安装，桌面与开始菜单会创建快捷方式。
pause
