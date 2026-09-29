@echo off
rem  Double-click me. Starts the storage nodes + backend + frontend and opens
rem  the browser. NO questions asked: the node count comes from the deployment
rem  config (nodes/deploy.json), which the admin UI writes.
rem  Docs: README.md  ->  the one-click-start section (section titles are in
rem  Chinese; this file deliberately stays pure ASCII, see the note below).
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
rem  Node count is NOT asked here (nor in Python) any more -- it lives in
rem  nodes/deploy.json and the admin UI writes it.
rem  The SAME file also holds the ports (backend / frontend / one per storage
rem  node); this launcher only reads them.  "Ports" card on the devices page.
rem
rem  Historical note: the prompt used to live in Python (scripts/start_all.py)
rem  rather than here.  Reason:
rem
rem    cmd.exe + "chcp 65001" + non-ASCII bytes in a .cmd file is broken: the
rem    parser loses its position mid-line and starts executing fragments of a
rem    "rem" comment as commands.  Measured: a Chinese rem line produced
rem        '...' is not recognized as an internal or external command
rem    and then cmd resumed at the wrong place in the file.
rem
rem  So this file stays PURE ASCII and only forwards its arguments.
rem  start_all.py reads the node count from nodes/deploy.json when --nodes was
rem  not given; --nodes wins and is written back to that file.
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
