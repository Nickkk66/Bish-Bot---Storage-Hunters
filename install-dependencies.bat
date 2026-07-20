@echo off
setlocal enabledelayedexpansion
title Bid Bot - installing dependencies
cd /d "%~dp0"

echo.
echo   Bid Bot - installing dependencies
echo   ================================
echo.
echo   This only installs the Python packages the tool needs.
echo   It has nothing to do with calibrating or picking regions.
echo.

rem The py launcher is the reliable way to find Python on Windows; plain "python"
rem may be the Microsoft Store stub, which silently does nothing useful.
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (
    where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
    echo   [X] Python isn't installed, or isn't on PATH.
    echo.
    echo       Get it from  https://www.python.org/downloads/
    echo       During install, TICK "Add python.exe to PATH".
    echo       Then run this file again.
    echo.
    pause
    exit /b 1
)

for /f "tokens=2" %%v in ('%PY% --version 2^>^&1') do set "PYVER=%%v"
echo   [.] Found Python !PYVER!

%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)" >nul 2>&1
if errorlevel 1 (
    echo   [X] Python !PYVER! is too old - 3.9 or newer is needed.
    echo       Install a current version from https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

rem tkinter ships with python.org builds but can be missing from trimmed installs,
rem and it is not pip-installable -- so say so plainly rather than failing later.
%PY% -c "import tkinter" >nul 2>&1
if errorlevel 1 (
    echo   [X] This Python has no tkinter, which the window needs.
    echo       Re-run the Python installer and enable "tcl/tk and IDLE",
    echo       or install from python.org rather than the Microsoft Store.
    echo.
    pause
    exit /b 1
)

rem A private environment, NOT your system Python. Installing into the system Python
rem means this tool's versions fight every other project's: doing exactly that here
rem upgraded numpy and instantly broke an installed scipy.
rem
rem It lives in LocalAppData rather than beside this file because this folder is inside
rem OneDrive. OneDrive syncs and locks the thousands of small files a venv contains,
rem which makes installs fail with "Access is denied", and it would upload ~200MB of
rem packages to the cloud for nothing.
set "VENV=%LOCALAPPDATA%\BidBot\venv"
set "VPY=%VENV%\Scripts\python.exe"

if not exist "%VPY%" (
    echo   [.] Creating a private environment...
    echo       %VENV%
    %PY% -m venv "%VENV%"
    if errorlevel 1 (
        echo   [X] Couldn't create the environment.
        echo.
        pause
        exit /b 1
    )
)

echo   [.] Installing packages ^(mss, numpy, pillow, pywin32^)...
echo.
"%VPY%" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 (
    echo.
    echo   [X] pip failed - full output:
    "%VPY%" -m pip install --disable-pip-version-check -r requirements.txt
    echo.
    echo       If it says "Access is denied", the tool is probably still running.
    echo       Close it ^(or press F8^) and try again.
    echo.
    pause
    exit /b 1
)

echo   [.] Checking everything imports...
"%VPY%" -c "import mss, numpy, PIL, win32api, win32con, win32gui, tkinter" 2>nul
if errorlevel 1 (
    echo   [X] Something still won't import. Full error:
    "%VPY%" -c "import mss, numpy, PIL, win32api, win32con, win32gui, tkinter"
    echo.
    pause
    exit /b 1
)

echo.
echo   [OK] Packages installed, in their own environment.
echo        Nothing on your system Python was changed.
echo.
echo   You can close this - run.bat starts the tool.
echo.
