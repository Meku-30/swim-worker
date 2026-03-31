#!/usr/bin/env python3
"""exe ビルド

使い方:
  pip install pyinstaller
  python scripts/build_exe.py

出力:
  Windows: dist/swim-worker-gui.exe (GUI版のみ)
  Mac/Linux: dist/swim-worker (CLI版)
"""
import platform
import PyInstaller.__main__

common_args = [
    "--hidden-import", "redis",
    "--hidden-import", "redis.asyncio",
    "--hidden-import", "hiredis",
    "--hidden-import", "pydantic_settings",
    "--hidden-import", "dotenv",
]

if platform.system() == "Windows":
    # Windows: GUI版のみ
    gui_args = [
        "--hidden-import", "pystray",
        "--hidden-import", "pystray._win32",
        "--hidden-import", "PIL",
        "--hidden-import", "PIL.Image",
        "--hidden-import", "PIL.ImageDraw",
    ]
    print("Building swim-worker-gui (Windows GUI)...")
    PyInstaller.__main__.run([
        "swim_worker/gui_main.py",
        "--onefile",
        "--name", "swim-worker-gui",
        "--windowed",
        *common_args,
        *gui_args,
    ])
else:
    # Mac/Linux: CLI版
    print("Building swim-worker (CLI)...")
    PyInstaller.__main__.run([
        "swim_worker/__main__.py",
        "--onefile",
        "--name", "swim-worker",
        *common_args,
    ])

print("Done!")
