@echo off
chcp 65001 >nul
cd /d "%~dp0"
title CClear - 启动
python main.py
pause