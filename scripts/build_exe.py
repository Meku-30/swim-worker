#!/usr/bin/env python3
"""クロスプラットフォーム exe ビルド

使い方:
  pip install pyinstaller
  python scripts/build_exe.py

出力: dist/swim-worker, dist/swim-worker-gui (Linux/Mac)
      dist/swim-worker.exe, dist/swim-worker-gui.exe (Windows)
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

# CLI版
print("Building swim-worker (CLI)...")
PyInstaller.__main__.run([
    "swim_worker/__main__.py",
    "--onefile",
    "--name", "swim-worker",
    *common_args,
])

# GUI版
print("Building swim-worker-gui...")
gui_args = [
    "swim_worker/gui_main.py",
    "--onefile",
    "--name", "swim-worker-gui",
    *common_args,
]
if platform.system() == "Windows":
    gui_args.append("--windowed")  # コンソール非表示

PyInstaller.__main__.run(gui_args)

print("Done!")
