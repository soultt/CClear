@echo off
chcp 65001 >nul
cd /d "%~dp0"
title CClear - 以管理员权限启动
python main.py --elevate
pause