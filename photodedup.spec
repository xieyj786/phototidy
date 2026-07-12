# -*- mode: python ; coding: utf-8 -*-
import importlib.util

from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = []


def collect_if_available(module_name):
    if importlib.util.find_spec(module_name) is None:
        return
    tmp_ret = collect_all(module_name)
    datas.extend(tmp_ret[0])
    binaries.extend(tmp_ret[1])
    hiddenimports.extend(tmp_ret[2])


for module_name in ('PIL', 'pillow_heif', 'tkinter'):
    collect_if_available(module_name)


a = Analysis(
    ['photodedup.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
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
    [],
    exclude_binaries=True,
    name='PhotoDedup',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='PhotoDedup',
)
app = BUNDLE(
    coll,
    name='PhotoDedup.app',
    icon=None,
    bundle_identifier=None,
)
