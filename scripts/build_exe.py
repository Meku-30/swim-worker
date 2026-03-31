#!/usr/bin/env python3
"""クロスプラットフォーム exe ビルド

使い方:
  pip install pyinstaller
  python scripts/build_exe.py

出力: dist/swim-worker (Linux/Mac) or dist/swim-worker.exe (Windows)
"""
import platform
import PyInstaller.__main__

name = "swim-worker"
args = [
    "swim_worker/__main__.py",
    "--onefile",
    "--name", name,
    "--hidden-import", "redis",
    "--hidden-import", "redis.asyncio",
    "--hidden-import", "hiredis",
    "--hidden-import", "pydantic_settings",
    "--hidden-import", "dotenv",
]

print(f"Building {name} for {platform.system()}...")
PyInstaller.__main__.run(args)
print(f"Done! Output: dist/{name}")
