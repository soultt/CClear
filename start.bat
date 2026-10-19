@echo off
chcp 65001 >nul
cd /d "%~dp0"
title CClear - C 盘空间管家

where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 未检测到 Python，请先安装 Python 3.9+ 并加入 PATH。
  echo        下载地址: https://www.python.org/downloads/windows/
  pause
  exit /b 1
)

echo 正在启动 CClear，界面将在独立窗口中打开 ...
python main.py %*

if errorlevel 1 (
  echo.
  echo [提示] 启动失败。若提示缺少依赖，请执行:
  echo        python -m pip install -r requirements.txt
  pause
)
