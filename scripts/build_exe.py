#!/usr/bin/env python3
"""exe ビルド

使い方:
  pip install pyinstaller
  python scripts/build_exe.py

出力:
  Windows: dist/swim-worker-gui.exe (GUI版)
  Mac:     dist/swim-worker (GUI版)
  Linux:   dist/swim-worker (CLI版)
"""
import platform
import PyInstaller.__main__

common_args = [
    "--hidden-import", "redis",
    "--hidden-import", "redis.asyncio",
    "--hidden-import", "hiredis",
    "--hidden-import", "pydantic_settings",
    "--hidden-import", "dotenv",
    # curl_cffi は C拡張 (_cffi_backend) を持つため念のため明示
    "--hidden-import", "curl_cffi",
    "--hidden-import", "curl_cffi.requests",
    "--hidden-import", "_cffi_backend",
    # PyInstallerがswim_worker.__init__.pyを確実に含めるため
    "--collect-submodules", "swim_worker",
]

system = platform.system()

if system in ("Windows", "Darwin"):
    # Windows / macOS: GUI版
    pystray_backend = "pystray._win32" if system == "Windows" else "pystray._darwin"
    gui_args = [
        "--hidden-import", "pystray",
        "--hidden-import", pystray_backend,
        "--hidden-import", "PIL",
        "--hidden-import", "PIL.Image",
        "--hidden-import", "PIL.ImageDraw",
    ]
    name = "swim-worker-gui" if system == "Windows" else "swim-worker"
    print(f"Building {name} ({system} GUI)...")
    PyInstaller.__main__.run([
        "swim_worker/gui_main.py",
        "--onefile",
        "--name", name,
        "--windowed",
        *common_args,
        *gui_args,
    ])
else:
    # Linux: CLI版
    print("Building swim-worker (CLI)...")
    PyInstaller.__main__.run([
        "swim_worker/__main__.py",
        "--onefile",
        "--name", "swim-worker",
        *common_args,
    ])

print("Done!")
