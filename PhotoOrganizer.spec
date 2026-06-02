# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        # cv2 / opencv
        'cv2',
        'numpy',
        # watchdog Windows backend
        'watchdog.observers.winapi',
        'watchdog.observers.read_directory_changes',
        'watchdog.events',
        # PIL
        'PIL',
        'PIL.Image',
        'PIL.ExifTags',
        'PIL.JpegImagePlugin',
        'PIL.PngImagePlugin',
        'PIL.BmpImagePlugin',
        'PIL.GifImagePlugin',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter',
        'matplotlib',
        'scipy',
        'IPython',
        'pandas',
        'PyQt5',
        'PyQt6',
        'wx',
    ],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='PhotoOrganizer',
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
