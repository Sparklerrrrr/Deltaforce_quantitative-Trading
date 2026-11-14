@echo off
rem ---------------------------------------------------------------------------
rem Build DFQ.exe (the graphical interface) on Windows.
rem Double-click this file, or run it from the project root.
rem
rem Result: DFQ.exe in the project root (next to the scripts folder), so that
rem it shares config.yaml and data\ with the scheduled tasks.
rem (ASCII only on purpose: cmd.exe reads .bat files in the system code page.)
rem ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
    echo [1/4] Creating virtual environment .venv ...
    python -m venv .venv || goto :fail
)
set "PY=.venv\Scripts\python.exe"

echo [2/4] Installing dependencies and PyInstaller ...
"%PY%" -m pip install -q -r requirements.txt || goto :fail
"%PY%" -m pip install -q "pyinstaller>=6.16" || goto :fail

echo [3/4] Checking that tkinter is available ...
"%PY%" -c "import tkinter" || (
    echo tkinter is missing. Reinstall Python from python.org and keep "tcl/tk and IDLE" checked.
    goto :fail
)

echo [4/4] Building DFQ.exe (takes a minute) ...
"%PY%" -m PyInstaller --noconfirm --clean --onefile --windowed --name DFQ ^
    --paths . --collect-submodules dfq ^
    --add-data "%CD%\config.example.yaml;." ^
    --distpath build\dist --workpath build\work --specpath build ^
    scripts\dfq_gui_entry.py || goto :fail

copy /y "build\dist\DFQ.exe" "DFQ.exe" >nul || goto :fail
echo.
echo Done: %CD%\DFQ.exe
echo Double-click DFQ.exe to open the interface.
pause
exit /b 0

:fail
echo.
echo Build FAILED. See the messages above.
pause
exit /b 1
