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
    # GUI 依存 (PIL/Pillow, pystray, tkinter) は CLI では未使用なのでバイナリから除外する。
    # __main__.py は icon.py/gui.py をインポートしないので実行時に問題なし。
    print("Building swim-worker (CLI)...")
    cli_exclude_args = [
        # GUI 関連
        "--exclude-module", "PIL",
        "--exclude-module", "pystray",
        "--exclude-module", "tkinter",
        "--exclude-module", "swim_worker.gui",
        "--exclude-module", "swim_worker.gui_main",
        "--exclude-module", "swim_worker.icon",
        # 使っていない stdlib モジュール (bundle されると数百KB〜数MB を占める)
        # メールプロトコル系 — 本 Worker は SWIM HTTPS + Redis TCP のみ
        "--exclude-module", "smtplib",
        "--exclude-module", "imaplib",
        "--exclude-module", "poplib",
        "--exclude-module", "mailbox",
        "--exclude-module", "nntplib",
        "--exclude-module", "telnetlib",
        "--exclude-module", "ftplib",
        # テスト / 開発
        "--exclude-module", "unittest",
        "--exclude-module", "doctest",
        # 教育/GUI 用途
        "--exclude-module", "turtle",
        "--exclude-module", "turtledemo",
        "--exclude-module", "idlelib",
        "--exclude-module", "tkinter.tix",
        # ターミナル UI
        "--exclude-module", "curses",
        "--exclude-module", "readline",
        # その他
        "--exclude-module", "webbrowser",
        # 以下は exclude しない (依存チェーンで必要):
        #   email, html — importlib.metadata が使う (redis から間接依存)
        #   xml, pyexpat, xmlrpc — pydantic/依存が dynamic import する可能性
        #   unicodedata, _decimal — pydantic が数値/文字列正規化で使う可能性
        #   distutils, pydoc, pydoc_data — setuptools/importlib が参照する可能性
    ]
    PyInstaller.__main__.run([
        "swim_worker/__main__.py",
        "--onefile",
        "--name", "swim-worker",
        # --strip: bundled shared libraries のデバッグシンボルを除去
        # (libpython3.11.so, curl_cffi の .so 等)。機能影響なし、C レベルクラッシュ時の
        # 関数名が出なくなるのみ。Python tracebacks には影響しない。
        "--strip",
        *common_args,
        *cli_exclude_args,
    ])

print("Done!")
