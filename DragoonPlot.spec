# -*- mode: python ; coding: utf-8 -*-
import sys
import os

# Bundle dfu-util on Windows only
dfu_datas = []
if sys.platform == 'win32':
    # Check if dfu-util files exist in project directory
    if os.path.exists('dfu-util.exe'):
        dfu_datas.append(('dfu-util.exe', '.'))
    if os.path.exists('libusb-1.0.dll'):
        dfu_datas.append(('libusb-1.0.dll', '.'))

a = Analysis(
    ['dragoonplot.py'],
    pathex=[],
    binaries=[],
    datas=dfu_datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='DragoonPlot',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
