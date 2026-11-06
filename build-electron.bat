@echo off
cd /d "%~dp0"
chcp 65001 >nul

REM 构建 NSIS 安装包（安装版）
echo [1/2] 正在构建安装版...
npx electron-builder --win nsis --publish never
if errorlevel 1 (
    echo 安装版构建失败
    exit /b 1
)

REM 构建便携版（覆盖同名文件前先删除旧的）
echo [2/2] 正在构建便携版...
del /f /q "dist\CClear-Portable-1.4.0.exe" 2>nul
npx electron-builder --win portable --publish never
if errorlevel 1 (
    echo 便携版构建失败
    exit /b 1
)

REM 将便携 exe 重命名为 zip（兼容旧格式）
echo 正在打包便携版 zip...
powershell -NoProfile -Command "Compress-Archive -Path 'dist\win-unpacked\CClear.exe','dist\win-unpacked\resources' -DestinationPath 'dist\CClear-Portable-1.4.0.zip' -Force"
if errorlevel 1 (
    echo 便携版 zip 打包失败
    exit /b 1
)

echo.
echo 构建完成:
echo   安装版: dist\CClear-Setup-1.4.0.exe
echo   便携版: dist\CClear-Portable-1.4.0.zip
pause
