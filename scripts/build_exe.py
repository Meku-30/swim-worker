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

system = platform.system()

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

# 全プラットフォーム共通の stdlib excludes。バイナリサイズを数 MB 削減する。
# GUI 依存 (tkinter/PIL/pystray) は GUI 版で必要なので除外しない。
# 以下は GUI 版でも CLI 版でも全く使っていない stdlib モジュール。
common_excludes = [
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
    # 教育/GUI 用途 (本 Worker は使わない)
    "--exclude-module", "turtle",
    "--exclude-module", "turtledemo",
    "--exclude-module", "idlelib",
    "--exclude-module", "tkinter.tix",  # 非推奨 tkinter サブコンポーネント
    # ターミナル UI (GUI 版にも CLI 版にも不要)
    "--exclude-module", "curses",
    "--exclude-module", "readline",
    # その他
    "--exclude-module", "webbrowser",
    # 以下は exclude しない (依存チェーンで必要):
    #   email, html — importlib.metadata → redis が間接依存
    #   xml, pyexpat, xmlrpc — pydantic/依存が dynamic import する可能性
    #   unicodedata, _decimal — pydantic の数値/文字列処理
    #   distutils, pydoc, pydoc_data — setuptools/importlib が参照する可能性
]

# --strip: bundled shared libraries のデバッグシンボルを除去。Python tracebacks は維持。
#   Linux: ✅ 効果大 (18MB 級の削減実績)
#   macOS: ✅ BSD strip で安全
#   Windows: ❌ PyInstaller 公式が「not recommended」(PE 形式、AV 誤検知増)
strip_args = [] if system == "Windows" else ["--strip"]

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
        *strip_args,           # macOS のみ適用 (Windows はスキップ)
        *common_args,
        *common_excludes,      # GUI 版でも未使用な stdlib モジュールを除外
        *gui_args,
    ])
else:
    # Linux: CLI版 (Linux 実行ファイルにはアイコン埋め込みの概念なし)
    # GUI 依存 (PIL/Pillow, pystray, tkinter) は CLI では未使用なのでバイナリから除外する。
    # __main__.py は icon.py/gui.py をインポートしないので実行時に問題なし。
    print("Building swim-worker (CLI)...")
    cli_only_excludes = [
        # Linux CLI 専用: GUI 関連を追加で exclude
        "--exclude-module", "PIL",
        "--exclude-module", "pystray",
        "--exclude-module", "tkinter",
        "--exclude-module", "swim_worker.gui",
        "--exclude-module", "swim_worker.gui_main",
        "--exclude-module", "swim_worker.icon",
    ]
    PyInstaller.__main__.run([
        "swim_worker/__main__.py",
        "--onefile",
        "--name", "swim-worker",
        *strip_args,           # Linux は常に strip 適用
        *common_args,
        *common_excludes,
        *cli_only_excludes,
    ])

print("Done!")
