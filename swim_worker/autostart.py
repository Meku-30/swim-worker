"""OS ログイン時の自動起動 (Windows のスタートアップ .bat / macOS の LaunchAgent plist)。

tkinter 非依存。ファイルの中身の組み立てを関数にして単体テストできるようにしている。
"""
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

PLIST_LABEL = "org.swim-worker"
BAT_NAME = "SWIM Worker.bat"


def supported(platform: str | None = None) -> bool:
    return (platform or sys.platform) in ("win32", "darwin")


def startup_path(platform: str | None = None) -> Path:
    """プラットフォーム別の自動起動ファイルのパス"""
    platform = platform or sys.platform
    if platform == "darwin":
        return Path.home() / "Library" / "LaunchAgents" / f"{PLIST_LABEL}.plist"
    startup = (Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows"
               / "Start Menu" / "Programs" / "Startup")
    return startup / BAT_NAME


def launch_command() -> str:
    """自動起動で実行するもの (frozen なら exe)"""
    if getattr(sys, "frozen", False):
        return sys.executable
    return "python -m swim_worker"


def build_plist(exe_path: str, working_dir: Path) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{PLIST_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{exe_path}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>WorkingDirectory</key>
    <string>{working_dir}</string>
</dict>
</plist>
"""


def build_bat(exe_path: str, working_dir: Path) -> str:
    return f'@echo off\r\ncd /d "{working_dir}"\r\nstart "" "{exe_path}"\r\n'


def file_bytes(platform: str, exe_path: str, working_dir: Path) -> bytes:
    """自動起動ファイルとして書く中身 (バイト列)"""
    if platform == "darwin":
        return build_plist(exe_path, working_dir).encode("utf-8")
    # Windows .bat はシステムの ANSI コードページで読まれる
    # パスに日本語が含まれる場合UTF-8だと文字化けするため mbcs (日本語Windows=CP932) で書き込む
    return build_bat(exe_path, working_dir).encode("mbcs", errors="replace")


def is_enabled(platform: str | None = None) -> bool:
    if not supported(platform):
        return False
    return startup_path(platform).exists()


def enable(path: Path, platform: str, exe_path: str, working_dir: Path) -> None:
    path.write_bytes(file_bytes(platform, exe_path, working_dir))
    logger.info("自動起動を有効にしました")


def disable(path: Path) -> None:
    if path.exists():
        path.unlink()
    logger.info("自動起動を無効にしました")


def needs_path_update(path: Path, platform: str, exe_path: str) -> bool:
    """自動起動ファイル内の exe パスが今の exe と違うか (exe を移動した場合)"""
    if not path.exists():
        return False
    # .bat はシステムコードページ (mbcs)、plist は UTF-8
    read_encoding = "utf-8" if platform == "darwin" else "mbcs"
    try:
        content = path.read_text(encoding=read_encoding, errors="replace")
    except Exception:
        return False
    return exe_path not in content
