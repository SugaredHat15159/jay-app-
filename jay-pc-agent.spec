# -*- mode: python ; coding: utf-8 -*-
import os
from PyInstaller.utils.hooks import collect_dynamic_libs, collect_data_files
block_cipher = None

# sounddevice ships the PortAudio shared library as package data; make sure
# PyInstaller bundles it (and numpy's runtime).
sd_bins = collect_dynamic_libs('sounddevice')
sd_data = collect_data_files('sounddevice')

# openWakeWord needs TWO kinds of ONNX models at runtime:
#  - shared FEATURE models (melspectrogram.onnx, embedding_model.onnx) that Model()
#    loads from openwakeword/resources/models/ by default
#  - the WAKE models (hey_jarvis_v0.1.onnx, ...) which ww.py resolves from ww_models/
# Newer openwakeword releases download these into a user cache instead of shipping
# them in the wheel, so collect_data_files misses them. fetch_ww_models.py gathers
# ALL of them into ./ww_models; here we route each kind to where it must live.
oww_data = collect_data_files('openwakeword')
ort_bins = collect_dynamic_libs('onnxruntime')
_FEATURE = {'melspectrogram.onnx', 'embedding_model.onnx'}
_have = set(os.listdir('ww_models')) if os.path.isdir('ww_models') else set()
ww_models = [(os.path.join('ww_models', f), 'ww_models')
             for f in _have if f.endswith('.onnx') and f not in _FEATURE]
ww_features = [(os.path.join('ww_models', f), os.path.join('openwakeword', 'resources', 'models'))
               for f in _have if f in _FEATURE]

# Qt WebEngine (Chromium) needs its process exe, .pak resources, icudtl.dat and
# locales bundled. PyInstaller's PySide6 hook collects these when the WebEngine
# modules are imported; the hiddenimports below make that explicit.
a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=sd_bins + ort_bins,
    datas=sd_data + oww_data + ww_models + ww_features,
    hiddenimports=[
        'paho.mqtt.client',
        'pynput.keyboard._win32',
        'pynput.mouse._win32',
        'sounddevice',
        'numpy',
        'requests',
        'openwakeword',
        'openwakeword.model',
        'onnxruntime',
        'PySide6.QtWebEngineWidgets',
        'PySide6.QtWebEngineCore',
        'PySide6.QtWebChannel',
        'PySide6.QtNetwork',
        'PySide6.QtPrintSupport',
        'PySide6.QtQuick',
        'PySide6.QtQml',
        'PySide6.QtOpenGL',
        'PySide6.QtPositioning',
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
    icon='icon.ico',
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
