@echo off
REM Build script for Windows
REM Creates a single .exe file with all dependencies bundled
REM
REM Prerequisites: Python 3.8+ installed and in PATH
REM Run: build.bat

setlocal enabledelayedexpansion

echo.
echo ========================================
echo   DragoonPlot Build Script (Windows)
echo ========================================
echo.

REM Check if Python is available
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found in PATH
    echo.
    echo Please install Python 3.8+ from https://python.org
    echo Make sure to check "Add Python to PATH" during installation
    exit /b 1
)

echo [1/4] Checking Python version...
for /f "tokens=2" %%i in ('python --version 2^>^&1') do set PYVER=%%i
echo       Python %PYVER% found

REM Install dependencies automatically
echo.
echo [2/4] Installing dependencies...
python -m pip install --quiet --upgrade pip
python -m pip install --quiet dearpygui pyserial pyinstaller

if errorlevel 1 (
    echo ERROR: Failed to install dependencies
    echo Try running: pip install dearpygui pyserial pyinstaller
    exit /b 1
)
echo       Dependencies installed

REM Clean previous builds
echo.
echo [3/4] Cleaning previous builds...
if exist build rmdir /s /q build 2>nul
if exist dist rmdir /s /q dist 2>nul
if exist __pycache__ rmdir /s /q __pycache__ 2>nul
echo       Clean complete

REM Check for dfu-util (optional but recommended)
if not exist "dfu-util.exe" (
    echo.
    echo WARNING: dfu-util.exe not found in project directory
    echo          DFU flashing will not work in the built executable
    echo          Download from: https://sourceforge.net/projects/dfu-util/
)

REM Build
echo.
echo [4/4] Building executable...
python -m PyInstaller --noconfirm DragoonPlot.spec

if errorlevel 1 (
    echo.
    echo ERROR: PyInstaller build failed!
    exit /b 1
)

REM Check result
if exist "dist\DragoonPlot.exe" (
    echo.
    echo ========================================
    echo   BUILD SUCCESSFUL!
    echo ========================================
    echo.
    echo   Executable: dist\DragoonPlot.exe
    echo.
    for %%A in ("dist\DragoonPlot.exe") do echo   Size: %%~zA bytes
    echo.
    echo   To run: dist\DragoonPlot.exe
    echo.
) else (
    echo.
    echo ERROR: Build failed - executable not created
    exit /b 1
)

endlocal
