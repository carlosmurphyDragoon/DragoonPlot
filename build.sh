#!/bin/bash
# Build script for Linux
# Creates a single executable file

set -e

echo "=== DragoonPlot Build Script (Linux) ==="

# Check dependencies
echo "Checking dependencies..."
python3 -c "import dearpygui" 2>/dev/null || { echo "ERROR: dearpygui not installed. Run: pip install dearpygui"; exit 1; }
python3 -c "import serial" 2>/dev/null || { echo "ERROR: pyserial not installed. Run: pip install pyserial"; exit 1; }
python3 -c "import PyInstaller" 2>/dev/null || { echo "ERROR: pyinstaller not installed. Run: pip install pyinstaller"; exit 1; }

# Clean previous builds
echo "Cleaning previous builds..."
rm -rf build/ dist/ __pycache__/

# Build
echo "Building executable..."
pyinstaller serial_monitor.spec

# Check result
if [ -f "dist/DragoonPlot" ]; then
    echo ""
    echo "=== Build successful! ==="
    echo "Executable: dist/DragoonPlot"
    echo "Size: $(du -h dist/DragoonPlot | cut -f1)"
    echo ""
    echo "To run: ./dist/DragoonPlot"
else
    echo "ERROR: Build failed!"
    exit 1
fi
