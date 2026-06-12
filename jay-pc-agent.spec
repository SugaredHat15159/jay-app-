# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_dynamic_libs, collect_data_files
block_cipher = None

# sounddevice ships the PortAudio shared library as package data; make sure
# PyInstaller bundles it (and numpy's runtime).
sd_bins = collect_dynamic_libs('sounddevice')
sd_data = collect_data_files('sounddevice')

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=sd_bins,
    datas=sd_data,
    hiddenimports=[
        'paho.mqtt.client',
        'pynput.keyboard._win32',
        'pynput.mouse._win32',
        'sounddevice',
        'numpy',
        'requests',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='JAY PC Agent',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='JAY PC Agent',
)
