@echo off
chcp 65001 >nul
cd /d "%~dp0"
title CClear - 管理员模式

where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 未检测到 Python，请先安装 Python 3.9+ 并加入 PATH。
  pause
  exit /b 1
)

echo 正在以管理员权限启动 CClear（会弹出 UAC 确认窗口）...
python main.py --elevate %*
if errorlevel 1 pause
