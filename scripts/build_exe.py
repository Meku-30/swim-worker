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
import subprocess
import sys
from pathlib import Path

import PyInstaller.__main__

ROOT = Path(__file__).parent.parent

# ビルド前にアイコンファイル (.ico / .icns) を生成する。
# アイコンのソースは swim_worker/icon.py の Python コードで、
# generate_icons.py がそこから .ico/.icns を書き出す。
print("Generating icon files...")
subprocess.run(
    [sys.executable, str(ROOT / "scripts" / "generate_icons.py")],
    check=True,
)

ICON_ICO = ROOT / "swim_worker" / "resources" / "icon.ico"
ICON_ICNS = ROOT / "swim_worker" / "resources" / "icon.icns"

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
    # 実行ファイルに埋め込むアイコン (.ico on Windows, .icns on macOS)
    icon_path = ICON_ICO if system == "Windows" else ICON_ICNS
    if icon_path.exists():
        gui_args.extend(["--icon", str(icon_path)])
    else:
        print(f"[WARN] アイコンファイルが見つかりません: {icon_path}")
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
    # Linux: CLI版 (Linux 実行ファイルにはアイコン埋め込みの概念なし)
    print("Building swim-worker (CLI)...")
    PyInstaller.__main__.run([
        "swim_worker/__main__.py",
        "--onefile",
        "--name", "swim-worker",
        *common_args,
    ])

print("Done!")
