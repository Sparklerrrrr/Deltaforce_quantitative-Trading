@echo off
rem ---------------------------------------------------------------------------
rem dfq launcher for Windows Task Scheduler.  (ASCII only on purpose: cmd.exe
rem reads .bat files in the system code page, so non-ASCII text here could break.)
rem
rem Usage:  run_dfq.bat <subcommand> [args...]      e.g.  run_dfq.bat run
rem
rem - switches to the project root (spaces in the path are fine)
rem - appends all output to data\logs\task-YYYYMMDD.log
rem - secrets come from USER environment variables; never put secrets in this file
rem ---------------------------------------------------------------------------
setlocal
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0.."
if not exist "data\logs" mkdir "data\logs"

set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set "TODAY=%%i"
echo ==== %date% %time% dfq %* ====>> "data\logs\task-%TODAY%.log"
"%PY%" -m dfq %* >> "data\logs\task-%TODAY%.log" 2>&1
exit /b %errorlevel%
