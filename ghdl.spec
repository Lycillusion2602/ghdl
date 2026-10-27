# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 构建配置。

转换引擎里 Pillow / pillow-heif / pypdfium2 带二进制扩展和 DLL，
只靠 import 分析容易漏（尤其 pillow-heif 的 libheif 和 pypdfium2 的 pdfium.dll），
所以这里显式 collect_all。LibreOffice / ffmpeg 是外部程序，不打进包。
"""
from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], []
for _pkg in ("PIL", "pillow_heif", "pypdf", "pypdfium2", "pypdfium2_raw"):
    try:
        _d, _b, _h = collect_all(_pkg)
    except Exception:
        continue
    datas += _d
    binaries += _b
    hiddenimports += _h

a = Analysis(
    ['server.py'],
    pathex=[],
    binaries=binaries,
    datas=[('static', 'static')] + datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 这些是 Pillow 可选插件拖进来的大件，转换用不到；排掉能少几十 MB
    excludes=['tkinter', 'numpy', 'matplotlib', 'scipy', 'pandas', 'PyQt5', 'PySide6'],
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
    name='ghdl',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='static/logo.ico',
)
