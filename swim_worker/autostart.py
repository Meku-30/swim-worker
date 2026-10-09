"""OS ログイン時の自動起動 (Windows のスタートアップ .bat / macOS の LaunchAgent plist)。

tkinter 非依存。ファイルの中身の組み立てを関数にして単体テストできるようにしている。
"""
import logging
import os
import plistlib
import sys
from pathlib import Path

from swim_worker.gui_helpers import bat_escape, encode_bat

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


def launch_command() -> list[str]:
    """自動起動で実行するコマンド (frozen なら exe、開発環境なら python -m swim_worker.gui)"""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-m", "swim_worker.gui"]


def build_plist(args: list[str], working_dir: Path) -> bytes:
    """LaunchAgent の plist (plistlib が XML のエスケープをする)"""
    return plistlib.dumps({
        "Label": PLIST_LABEL,
        "ProgramArguments": list(args),
        "RunAtLoad": True,
        "WorkingDirectory": str(working_dir),
    })


def build_bat(args: list[str], working_dir: Path) -> str:
    """スタートアップの .bat (パスの % は %% にする)"""
    cmd = " ".join(f'"{bat_escape(a)}"' for a in args)
    return f'@echo off\r\ncd /d "{bat_escape(str(working_dir))}"\r\nstart "" {cmd}\r\n'


def file_bytes(platform: str, args: list[str], working_dir: Path,
               ansi_codec: str = "mbcs") -> bytes:
    """自動起動ファイルとして書く中身 (バイト列)"""
    if platform == "darwin":
        return build_plist(args, working_dir)
    # .bat はシステムの ANSI コードページで書く (書けない文字があれば UTF-8 + chcp 65001)
    return encode_bat(build_bat(args, working_dir), ansi_codec)


def is_enabled(platform: str | None = None) -> bool:
    if not supported(platform):
        return False
    return startup_path(platform).exists()


def enable(path: Path, platform: str, args: list[str], working_dir: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(file_bytes(platform, args, working_dir))
    logger.info("自動起動を有効にしました")


def disable(path: Path) -> None:
    if path.exists():
        path.unlink()
    logger.info("自動起動を無効にしました")


def needs_rewrite(path: Path, platform: str, args: list[str], working_dir: Path) -> bool:
    """自動起動ファイルが今の exe・フォルダと違う中身か (exe を移動した・古い形式)"""
    if not path.exists():
        return False
    try:
        return path.read_bytes() != file_bytes(platform, args, working_dir)
    except Exception:
        return False
