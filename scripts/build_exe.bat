@echo off
rem ---------------------------------------------------------------------------
rem Build DFQ (the desktop app) and its installer on Windows.
rem Double-click this file, or run it from the project root.
rem
rem Results (both under dist\, which git ignores):
rem   dist\DFQ\                 the program folder (DFQ.exe + _internal\)
rem   dist\DFQ-Setup-x.y.z.exe  the installer (needs Inno Setup; skipped if not found)
rem
rem Folder build (--onedir), not a single exe: a single exe unpacks itself into
rem C:\Users\...\AppData\Local\Temp on every start. The folder build runs in place,
rem so everything the program writes stays inside its install folder.
rem (ASCII only on purpose: cmd.exe reads .bat files in the system code page.)
rem
rem Isolation: only the two templates (config.example.yaml, .env.example) are
rem bundled. Your own config.yaml, .env and data\ are never copied, and the
rem program folder is scanned for personal files before the installer is made.
rem Build releases from a clean checkout (see docs\release process doc).
rem
rem Unattended use (CI, release workflow):
rem   set CI=true                    no "press any key" at the end
rem   set DFQ_REQUIRE_INSTALLER=1    fail when Inno Setup is missing
rem   set DFQ_ISCC=path\ISCC.exe     use this Inno Setup compiler (6.5 or newer)
rem ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
    echo [1/5] Creating virtual environment .venv ...
    python -m venv .venv || goto :fail
)
set "PY=.venv\Scripts\python.exe"

echo [2/5] Installing dependencies and PyInstaller ...
"%PY%" -m pip install -q -r requirements.txt || goto :fail
rem PyInstaller is pinned: a build tool runs with full access to the build machine, so take no surprises.
"%PY%" -m pip install -q "pyinstaller==6.22.3" || goto :fail

echo [3/5] Checking that the window component (pywebview + WebView2) is available ...
"%PY%" -c "import webview" || (
    echo pywebview is missing. Run: .venv\Scripts\pip install -r requirements.txt
    goto :fail
)

echo [4/5] Building the program folder (takes a minute) ...
"%PY%" -m PyInstaller --noconfirm --clean --onedir --windowed --name DFQ --icon "%CD%\dfq.ico" ^
    --paths . --collect-submodules dfq ^
    --add-data "%CD%\config.example.yaml;." ^
    --add-data "%CD%\dfq\webui\static;dfq\webui\static" ^
    --add-data "%CD%\dfq.ico;." ^
    --add-data "%CD%\.env.example;." ^
    --distpath build\dist --workpath build\work --specpath build ^
    scripts\dfq_gui_entry.py || goto :fail
if exist "dist\DFQ" rmdir /s /q "dist\DFQ"
if not exist "dist" mkdir "dist"
xcopy /e /i /q /y "build\dist\DFQ" "dist\DFQ" >nul || goto :fail
rem Personal files (config.yaml, .env, data\, *.db, logs, mails) must never reach the installer.
"%PY%" scripts\scan_secrets.py --dir "dist\DFQ" || (
    echo The program folder dist\DFQ contains personal files or secrets. See the list above.
    goto :fail
)

echo [5/5] Building the installer ...
for /f "usebackq delims=" %%v in (`%PY% -c "import dfq; print(dfq.__version__)"`) do set "VER=%%v"
set "ISCC="
if defined DFQ_ISCC set "ISCC=%DFQ_ISCC%"
if not defined ISCC for /f "usebackq delims=" %%i in (`powershell -NoProfile -Command "$k = 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*','HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*','HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*'; Get-ItemProperty $k -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName -like 'Inno Setup*' -and $_.InstallLocation } | ForEach-Object { Join-Path $_.InstallLocation 'ISCC.exe' } | Where-Object { Test-Path $_ } | Select-Object -First 1"`) do set "ISCC=%%i"
if not defined ISCC (
    echo Inno Setup was not found, so the installer was skipped.
    echo Install Inno Setup 6.5 or newer from https://jrsoftware.org/isinfo.php, or set DFQ_ISCC to the full path of ISCC.exe.
    if "%DFQ_REQUIRE_INSTALLER%"=="1" goto :fail
    goto :done
)
"%ISCC%" /Q "/DAppVersion=%VER%" "/DSourceDir=%CD%\dist\DFQ" "/DRepoDir=%CD%" "/DOutputDir=%CD%\dist" "installer\DFQ.iss" || goto :fail
echo Installer: %CD%\dist\DFQ-Setup-%VER%.exe

:done
echo.
echo Done. Program folder: %CD%\dist\DFQ
if not defined CI pause
exit /b 0

:fail
echo.
echo Build FAILED. See the messages above.
if not defined CI pause
exit /b 1
