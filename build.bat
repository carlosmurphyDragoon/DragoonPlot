@echo off
REM Build script for Windows
REM Creates a single .exe file

echo === DragoonPlot Build Script (Windows) ===

REM Check dependencies
echo Checking dependencies...
python -c "import dearpygui" 2>nul || (echo ERROR: dearpygui not installed. Run: pip install dearpygui && exit /b 1)
python -c "import serial" 2>nul || (echo ERROR: pyserial not installed. Run: pip install pyserial && exit /b 1)
python -c "import PyInstaller" 2>nul || (echo ERROR: pyinstaller not installed. Run: pip install pyinstaller && exit /b 1)

REM Clean previous builds
echo Cleaning previous builds...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist __pycache__ rmdir /s /q __pycache__

REM Build
echo Building executable...
pyinstaller DragoonPlot.spec

REM Check result
if exist "dist\DragoonPlot.exe" (
    echo.
    echo === Build successful! ===
    echo Executable: dist\DragoonPlot.exe
    echo.
    echo To run: dist\DragoonPlot.exe
) else (
    echo ERROR: Build failed!
    exit /b 1
)
