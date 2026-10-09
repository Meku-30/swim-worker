"""GUI 版の自動更新 (ダウンロード・検証・差し替えヘルパーの起動・snooze)。tkinter 非依存。

流れ: download_url() → download_and_verify() → launch_update_helper() → GUI が終了
→ ヘルパー (.bat / .sh) が exe を差し替えて再起動し、起動確認できなければロールバック。
"""
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from swim_worker.gui_helpers import (
    build_macos_update_script,
    build_windows_update_script,
    encode_bat,
    pyinstaller_clean_env,
)
from swim_worker.settings_store import load_json, save_json

logger = logging.getLogger(__name__)

RELEASE_DOWNLOAD_BASE = "https://github.com/Meku-30/swim-worker/releases/download"
ASSET_NAMES = {
    "win32": "swim-worker-windows.exe",
    "darwin": "swim-worker-macos",
}
SNOOZE_DURATION_HOURS = 24
STALE_UPDATE_FILE_SEC = 3 * 24 * 3600  # 3 日
MIN_ASSET_SIZE = 1024 * 1024  # 1MB未満は異常


def download_url(version: str, platform: str | None = None) -> str | None:
    """バージョンタグからダウンロードURLを組み立てる (GitHub APIを使わない)"""
    asset = ASSET_NAMES.get(platform or sys.platform)
    if asset is None:
        return None
    return f"{RELEASE_DOWNLOAD_BASE}/v{version}/{asset}"


def new_exe_path(base: Path, platform: str | None = None) -> Path:
    """ダウンロード先 (exeと同じディレクトリ)"""
    if (platform or sys.platform) == "win32":
        return base / "swim-worker-gui.new.exe"
    return base / "swim-worker.new"


# --- snooze (「後で」を選んだ版を一定時間聞かない) ---

class SnoozeStore:
    def __init__(self, path: Path, hours: int = SNOOZE_DURATION_HOURS):
        self.path = path
        self.hours = hours

    def is_snoozed(self, version: str, now: datetime | None = None) -> bool:
        """指定バージョンに対して現在 snooze 期間中かを返す。

        別バージョンや期限切れの snooze ファイルが残っていれば自動削除 (ゴミ掃除)。
        """
        snooze = load_json(self.path)
        if not snooze:
            return False
        if snooze.get("version") != version:
            self.clear()
            return False
        until_str = snooze.get("until")
        if not until_str:
            self.clear()
            return False
        try:
            until = datetime.fromisoformat(until_str)
        except (TypeError, ValueError):
            self.clear()
            return False
        if (now or datetime.now(timezone.utc)) >= until:
            self.clear()
            return False
        return True

    def set(self, version: str, now: datetime | None = None) -> None:
        """「後で」選択時、このバージョンを一定時間スキップする。"""
        until = (now or datetime.now(timezone.utc)) + timedelta(hours=self.hours)
        try:
            save_json(self.path, {"version": version, "until": until.isoformat()})
            logger.info("アップデート v%s を %d 時間スキップしました", version, self.hours)
        except Exception as e:
            logger.debug("snooze 保存失敗 (無視): %s", e)

    def clear(self) -> None:
        try:
            if self.path.exists():
                self.path.unlink()
        except Exception:
            pass

    def cleanup_expired(self, now: datetime | None = None) -> None:
        snooze = load_json(self.path)
        until_str = snooze.get("until") if snooze else None
        if not until_str:
            return
        try:
            until = datetime.fromisoformat(until_str)
            if (now or datetime.now(timezone.utc)) >= until:
                self.clear()
        except (TypeError, ValueError):
            self.clear()


def cleanup_stale_update_files(base: Path, now_ts: float | None = None) -> None:
    """3 日以上古い `*.old` / `*.new` / `*.new.exe` を削除 (起動時の軽量ハウスキープ)"""
    now_ts = datetime.now().timestamp() if now_ts is None else now_ts
    for pattern in ("*.old", "*.new", "*.new.exe"):
        for f in base.glob(pattern):
            try:
                if now_ts - f.stat().st_mtime > STALE_UPDATE_FILE_SEC:
                    f.unlink()
                    logger.debug("古いアップデートファイルを削除: %s", f.name)
            except Exception:
                pass


# --- ダウンロードと検証 ---

def download_and_verify(download_url: str, dest: Path, *, on_phase=None) -> int:
    """exe をダウンロードし、同じリリースの SHA256SUMS で検証してから dest に書く。

    戻り値: 書いたバイト数。on_phase(text) は段階の表示用 (別スレッドから呼ばれる)。
    """
    from curl_cffi.requests import Session, BrowserType
    from swim_worker.update_verify import verify_sha256
    asset_name = download_url.rsplit("/", 1)[-1]
    sums_url = download_url.rsplit("/", 1)[0] + "/SHA256SUMS"
    with Session(impersonate=BrowserType.chrome136, timeout=120.0) as client:
        # stream=False で全体をメモリに読み込む (curl_cffi では stream=True の扱いが不安定)
        resp = client.get(download_url, allow_redirects=True)
        if resp.status_code != 200:
            raise RuntimeError(f"ダウンロード失敗: status={resp.status_code}")
        content = resp.content
        if not content or len(content) < MIN_ASSET_SIZE:
            raise RuntimeError(f"ダウンロードサイズ異常: {len(content) if content else 0} bytes")
        # 同じ release の SHA256SUMS で整合性検証 (install.sh と同等)。
        # 破損・改竄された DL をそのまま exe として起動しないため必須。
        if on_phase:
            on_phase("整合性を検証しています")
        sums_resp = client.get(sums_url, allow_redirects=True)
        if sums_resp.status_code != 200:
            raise RuntimeError(f"SHA256SUMS 取得失敗: status={sums_resp.status_code}")
        digest = verify_sha256(content, sums_resp.text, asset_name)
        logger.info("SHA256 検証 OK: %s (%s…)", asset_name, digest[:16])
        with dest.open("wb") as f:
            f.write(content)
    return dest.stat().st_size


# --- 差し替えヘルパーの起動 ---

def launch_update_helper(*, base: Path, current_exe: Path, new_exe: Path,
                         new_version: str, platform: str | None = None) -> None:
    """差し替えスクリプトを書いて、GUI から切り離したプロセスで起動する"""
    platform = platform or sys.platform
    old_exe = current_exe.with_suffix(current_exe.suffix + ".old")
    startup_ok = base / "data" / ".startup_ok"
    rollback_marker = base / "data" / ".update_rollback.json"
    log_path = base / "swim-worker-update.log"
    if platform == "win32":
        script_path = base / "swim-worker-update.bat"
        script = build_windows_update_script(
            base=base, current_exe=current_exe, new_exe=new_exe, old_exe=old_exe,
            startup_ok=startup_ok, rollback_marker=rollback_marker,
            log_path=log_path, new_version=new_version,
        )
        # システム ANSI (日本語 Windows は CP932) で書く。書けない文字は UTF-8 + chcp 65001
        script_path.write_bytes(encode_bat(script))
        # PyInstaller 6.9+ の "Failed to load Python DLL" 対策として
        # 子プロセスの環境変数から _PYI_* を除去し、PYINSTALLER_RESET_ENVIRONMENT を設定
        # 参考: https://pyinstaller.org/en/stable/runtime-information.html
        CREATE_NO_WINDOW = 0x08000000  # コンソールは持つが非表示
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        CREATE_BREAKAWAY_FROM_JOB = 0x01000000
        subprocess.Popen(
            ["cmd", "/c", str(script_path)],
            creationflags=(CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
                           | CREATE_BREAKAWAY_FROM_JOB),
            env=pyinstaller_clean_env(os.environ),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    else:  # darwin
        script_path = base / "swim-worker-update.sh"
        script = build_macos_update_script(
            current_exe=current_exe, new_exe=new_exe, old_exe=old_exe,
            startup_ok=startup_ok, rollback_marker=rollback_marker,
            log_path=log_path, new_version=new_version,
        )
        script_path.write_text(script, encoding="utf-8")
        os.chmod(script_path, 0o755)
        subprocess.Popen(
            ["bash", str(script_path)],
            env=pyinstaller_clean_env(os.environ),
            start_new_session=True,
            close_fds=True,
        )
