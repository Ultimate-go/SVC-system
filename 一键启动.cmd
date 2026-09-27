@echo off
rem  Double-click me. Asks how many storage nodes to start (Enter = 4), then
rem  starts them + backend + frontend and opens the browser.
rem  Docs: README.md  ->  section "One-click start"
chcp 65001 >nul
rem  Belt and braces: also tell Python to use UTF-8 (covers redirected/piped output,
rem  where Python falls back to the locale encoding instead of the console codepage).
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
title VDS
where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] "python" is not on PATH. Install Python 3.12 or add it to PATH.
  pause
  exit /b 1
)
rem  ---------------------------------------------------------------------------
rem  Node count is chosen interactively -- but the prompt lives in Python
rem  (scripts/start_all.py), NOT here.  Reason:
rem
rem    cmd.exe + "chcp 65001" + non-ASCII bytes in a .cmd file is broken: the
rem    parser loses its position mid-line and starts executing fragments of a
rem    "rem" comment as commands.  Measured: a Chinese rem line produced
rem        '...' is not recognized as an internal or external command
rem    and then cmd resumed at the wrong place in the file.
rem
rem  So this file stays PURE ASCII and only forwards its arguments.
rem  start_all.py asks "how many nodes" when --nodes was not given AND stdin
rem  is a real console (i.e. when you double-clicked this file); piping or
rem  passing --nodes skips the question.
rem  ---------------------------------------------------------------------------
python scripts\start_all.py %*
set "RC=%ERRORLEVEL%"
if "%RC%"=="0" exit /b 0
echo.
echo [launcher exited with code %RC%] -- see the output above for the reason.
echo note: a storage node going down no longer takes the backend/frontend with
echo       it (so you can demo degraded mode). Add --strict to restore the old
echo       "any exit stops everything" behaviour.
pause
exit /b %RC%
