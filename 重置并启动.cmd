@echo off
rem  Double-click me to WIPE the demo data (vds.db + all node data) and start over.
rem  Accounts are re-created; NO demo files are created (upload your own).
rem  Node token (nodes\token.txt) is kept.
rem
rem  It ALSO resets the node count back to the default (4) AND the ports back to
rem  their defaults (backend 8000 / frontend 5173 / nodes 9101, 9102, ...) by
rem  rewriting nodes/deploy.json -- "reset" means factory state.  The old ports
rem  are accumulated into stop_ports, so a stack still running on them can still be
rem  stopped.  Add --nodes N to override the count.
rem  No questions are asked any more.
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
title VDS - reset
where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] "python" is not on PATH. Install Python 3.12 or add it to PATH.
  pause
  exit /b 1
)
python scripts\start_all.py --reset %*
set "RC=%ERRORLEVEL%"
if "%RC%"=="0" exit /b 0
echo.
echo [reset exited with code %RC%] -- see the output above for the reason.
pause
exit /b %RC%
