# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for Spotfire Report Validator GUI.

Produces a single portable .exe with no console window.

Build (on Windows):
    pip install -r requirements.txt pyinstaller
    pyinstaller spotfire-validator-gui.spec --clean --noconfirm

Output:
    dist/SpotfireValidator.exe
"""

import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

# Collect Jinja2 and PySide6 data files
datas = []
datas += collect_data_files('jinja2')
datas += collect_data_files('PySide6')

hiddenimports = [
    'jinja2.ext',
    'jinja2.exceptions',
] + collect_submodules('PySide6')

a = Analysis(
    ['gui.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        'tkinter',
        'matplotlib',
        'numpy.testing',
        'pytest',
        'IPython',
        'notebook',
    ],
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    name='SpotfireValidator',
    console=False,          # GUI app — no console window
    onefile=True,           # single portable .exe
    icon=None,              # set to 'icon.ico' if you have one
    strip=False,
    upx=True,               # compress if UPX is available
    upx_exclude=[],
    runtime_tmpdir=None,
)