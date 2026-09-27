@echo off
rem  Double-click me to stop everything (nodes 9101-9104, backend 8000, frontend 5173).
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
title VDS - stop
python scripts\start_all.py --stop
pause
