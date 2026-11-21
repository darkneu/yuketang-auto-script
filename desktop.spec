# Native Qt portable application. Never include .env, account caches, or CLI credentials.
from pathlib import Path
from PyInstaller.utils.hooks import copy_metadata

root = Path(SPECPATH)
a = Analysis(
    [str(root / 'desktop.py')], pathex=[str(root)],
    binaries=[], datas=copy_metadata('PySide6-Essentials') + copy_metadata('shiboken6')
        + [(str(root / 'assets' / name), 'assets') for name in ('up_arrow.svg', 'down_arrow.svg', 'folder.svg', 'settings.svg')],
    hiddenimports=['qrcode.image.pil', 'PIL.ImageQt', 'PySide6.QtSvg'],
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=['PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets',
              'PySide6.QtQml', 'PySide6.QtQuick', 'tkinter', 'pytest'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='YuKeTang',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          icon=str(root / 'assets' / 'app.ico'),
          console=False, disable_windowed_traceback=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='YuKeTang')
