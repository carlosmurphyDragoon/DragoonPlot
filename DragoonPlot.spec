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

# Bundle branding assets
branding_datas = []
if os.path.exists('branding'):
    branding_datas.append(('branding', 'branding'))

# Icon path for executable
icon_path = 'branding/Dragoon-icon.ico' if os.path.exists('branding/Dragoon-icon.ico') else None

a = Analysis(
    ['dragoonplot.py'],
    pathex=[],
    binaries=[],
    datas=dfu_datas + branding_datas,
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
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_path,
)
