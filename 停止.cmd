@echo off
rem  Double-click me to stop everything (ALL storage nodes, the backend and the
rem  frontend).  Which ports?  They come from nodes/deploy.json -- both the ones
rem  configured now and every port recorded as used so far (stop_ports),
rem  plus the default span as a safety net.  Only OUR processes are killed
rem  (matched by command line), so a stranger sitting on the same port is left
rem  alone and reported instead.
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
title VDS - stop
python scripts\start_all.py --stop
pause
