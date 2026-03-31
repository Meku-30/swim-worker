#!/usr/bin/env python3
"""exe ビルド

使い方:
  pip install pyinstaller
  python scripts/build_exe.py

出力:
  全OS: dist/swim-worker (CLI版)
  Windowsのみ: dist/swim-worker-gui.exe (GUI版)
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

# CLI版（全OS）
print("Building swim-worker (CLI)...")
PyInstaller.__main__.run([
    "swim_worker/__main__.py",
    "--onefile",
    "--name", "swim-worker",
    *common_args,
])

# GUI版（Windowsのみ）
if platform.system() == "Windows":
    print("Building swim-worker-gui (Windows GUI)...")
    PyInstaller.__main__.run([
        "swim_worker/gui_main.py",
        "--onefile",
        "--name", "swim-worker-gui",
        "--windowed",
        *common_args,
    ])

print("Done!")
