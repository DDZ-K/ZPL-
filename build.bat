@echo off
cd /d "%~dp0"

set "BUILD_ENV=C:\Users\Public\zpl_build_env"
set "BUILD_PY=%BUILD_ENV%\Scripts\python.exe"
set "BUILD_PYINSTALLER=%BUILD_ENV%\Scripts\pyinstaller.exe"

if not exist "%BUILD_PY%" (
    python -m venv "%BUILD_ENV%"
    if errorlevel 1 (
        pause
        exit /b 1
    )
)

"%BUILD_PY%" -m pip show pyinstaller PyQt5 pywin32 >nul 2>nul
if errorlevel 1 (
    "%BUILD_PY%" -m pip install PyQt5 pywin32 pyinstaller
    if errorlevel 1 (
        pause
        exit /b 1
    )
)

"%BUILD_PYINSTALLER%" --noconfirm --clean --onefile --windowed --name="ZPLLabelPrinter" --icon=app.ico --add-data "app.ico;." --hidden-import win32print --hidden-import pywintypes main.py
if errorlevel 1 (
    pause
    exit /b 1
)

echo Build complete. EXE is in dist\ZPL??????.exe
pause