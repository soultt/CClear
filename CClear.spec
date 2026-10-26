# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: build a single-file, windowed CClear-backend.exe.

    python -m PyInstaller --noconfirm --clean CClear.spec

The UI is rendered by the embedded Microsoft Edge WebView2 runtime, so the
resulting exe behaves like a native desktop program and never opens a browser.
"""

import os

APP_NAME = "CClear"
ICON = "CClear.ico"

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        # uvicorn resolves its loop/protocol implementations dynamically
        "uvicorn.logging",
        "uvicorn.loops.auto",
        "uvicorn.loops.asyncio",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan.on",
        # embedded window (WebView2)
        "webview",
        "webview.platforms.winforms",
        "webview.platforms.edgechromium",
        "clr",
        "pythonnet",
        "cffi",
        "_cffi_backend",
        "psutil",
    ],
    hookspath=[os.path.join("packaging", "hooks")],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "unittest",
        "pydoc_data",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
        "gi",
        "cefpython3",
        "cv2",
        "numpy",
        "pandas",
        "matplotlib",
        "PIL",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name=APP_NAME + "-backend",   # 后端进程名与应用壳 CClear.exe 区分开
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # GUI program: no console window
    uac_admin=True,         # require admin: cleaning needs elevated rights
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON,
)
