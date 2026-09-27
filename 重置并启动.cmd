@echo off
rem  Double-click me to WIPE the demo data (vds.db + all node data) and start over.
rem  Accounts are re-created; NO demo files are created (upload your own).
rem  Node token (nodes\token.txt) is kept.
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
title VDS - reset
python scripts\start_all.py --reset %*
