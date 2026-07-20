@echo off
title Bid Bot
cd /d "%~dp0"

rem The environment lives outside OneDrive; see install-dependencies.bat for why.
set "VENV=%LOCALAPPDATA%\BidBot\venv"

rem One file to click. If the packages aren't there yet, install them first, then
rem start. After the first run this just launches straight away.
"%VENV%\Scripts\python.exe" -c "import mss, numpy, PIL, win32api, tkinter" >nul 2>&1
if errorlevel 1 (
    echo.
    echo   First run - installing the Python packages this needs.
    echo   ^(Nothing to do with calibrating; it happens once.^)
    echo.
    call "%~dp0install-dependencies.bat"
    "%VENV%\Scripts\python.exe" -c "import mss, numpy, PIL, win32api, tkinter" >nul 2>&1
    if errorlevel 1 (
        echo   Install didn't finish - see the messages above.
        pause
        exit /b 1
    )
)

rem pythonw: no console window hanging around behind the tool.
start "" "%VENV%\Scripts\pythonw.exe" "%~dp0ui.py"
