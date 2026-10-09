"""GUI 版の自動更新 (ダウンロード・検証・差し替えヘルパーの起動・snooze)。tkinter 非依存。

流れ: download_url() → download_and_verify() → launch_update_helper() → GUI が終了
→ ヘルパー (.bat / .sh) が exe を差し替えて再起動し、起動確認できなければロールバック。
"""
import contextlib
import hashlib
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from swim_worker.gui_helpers import (
    build_macos_update_script,
    build_windows_update_script,
    encode_bat,
    pyinstaller_clean_env,
)
from swim_worker.settings_store import load_json, save_json
from swim_worker.update_verify import UpdateVerifyError, parse_sha256sums

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
#
# 流れ: fetch_checksums() で SHA256SUMS を取る → 期待するハッシュが分かってから本体を
# ストリームで .part に書きつつハッシュを計算 → 一致したら os.replace で置く。
# W3 (署名) は fetch_checksums() の中で SHA256SUMS.sig を取って検証し、通らなければ例外に
# すればよい (本体のダウンロードより前に止まる)。

CONNECT_STALL_TIMEOUT = 60.0    # 接続・無通信がこれだけ続いたら諦める (秒)
MAX_DOWNLOAD_SEC = 30 * 60      # 全体の上限 (遅い回線でも 30 分)
MAX_ASSET_SIZE = 300 * 1024 * 1024
MAX_SUMS_SIZE = 64 * 1024
PROGRESS_INTERVAL_SEC = 0.2


class CurlFetcher:
    """curl_cffi で GitHub から取る (証明書は curl_cffi 同梱の CA を使う)。

    stream=True のときの timeout は「接続」と「無通信 (1 B/s 未満が続く)」の上限になり、
    全体の時間では切らない (大きい exe を遅い回線で落とせるように)。全体の上限は呼ぶ側で見る。
    """

    def __init__(self, impersonate="chrome136", timeout: float = CONNECT_STALL_TIMEOUT):
        self._impersonate = impersonate
        self._timeout = timeout

    def _session(self):
        from curl_cffi.requests import Session
        kw = {"timeout": self._timeout}
        if self._impersonate:
            kw["impersonate"] = self._impersonate
        return Session(**kw)

    def get_text(self, url: str, max_bytes: int = MAX_SUMS_SIZE) -> str:
        with self._session() as client:
            resp = client.get(url, allow_redirects=True)
            if resp.status_code != 200:
                raise RuntimeError(f"{url.rsplit('/', 1)[-1]} 取得失敗: status={resp.status_code}")
            if len(resp.content) > max_bytes:
                raise RuntimeError(f"{url.rsplit('/', 1)[-1]} が大きすぎます")
            return resp.content.decode("utf-8")

    @contextlib.contextmanager
    def stream(self, url: str):
        """(status, Content-Length または None, チャンクの iterator) を返す"""
        with self._session() as client:
            resp = client.get(url, allow_redirects=True, stream=True)
            try:
                total = resp.headers.get("content-length")
                try:
                    total = int(total) if total is not None else None
                except ValueError:
                    total = None
                yield resp.status_code, total, resp.iter_content()
            finally:
                resp.close()


def release_base_url(download_url: str) -> str:
    return download_url.rsplit("/", 1)[0]


def fetch_checksums(fetcher, base_url: str) -> str:
    """同じリリースの SHA256SUMS を取る。

    W3: ここで `SHA256SUMS.sig` も取り、埋め込んだ公開鍵で検証して通らなければ例外にする。
    """
    return fetcher.get_text(f"{base_url}/SHA256SUMS")


def download_and_verify(download_url: str, dest: Path, *, fetcher=None, on_progress=None,
                        on_phase=None, clock=time.monotonic) -> int:
    """exe をストリームでダウンロードし、SHA256SUMS と一致したら dest に置く。

    途中で失敗したら何も残さない。戻り値は書いたバイト数。
    on_progress(済んだバイト数, 全体のバイト数 or None)・on_phase(text) は
    このスレッド (GUI のメインスレッドではない) から呼ばれる。
    """
    fetcher = fetcher or CurlFetcher()
    asset_name = download_url.rsplit("/", 1)[-1]
    if on_phase:
        on_phase("チェックサムを取得しています")
    sums_text = fetch_checksums(fetcher, release_base_url(download_url))
    expected = parse_sha256sums(sums_text).get(asset_name)
    if not expected:
        raise UpdateVerifyError(f"SHA256SUMS に {asset_name} のエントリがありません")

    if on_phase:
        on_phase("新しいバージョンを取得しています")
    part = dest.with_name(dest.name + ".part")
    digest = hashlib.sha256()
    done = 0
    started = clock()
    last_report = None
    try:
        with fetcher.stream(download_url) as (status, total, chunks), part.open("wb") as f:
            if status != 200:
                raise RuntimeError(f"ダウンロード失敗: status={status}")
            if total is not None and total > MAX_ASSET_SIZE:
                raise RuntimeError(f"ダウンロードが大きすぎます: {total} bytes")
            for chunk in chunks:
                if not chunk:
                    continue
                done += len(chunk)
                if done > MAX_ASSET_SIZE:
                    raise RuntimeError(f"ダウンロードが大きすぎます: {done} bytes 超")
                now = clock()
                if now - started > MAX_DOWNLOAD_SEC:
                    raise RuntimeError("ダウンロードに時間がかかりすぎたため中止しました")
                f.write(chunk)
                digest.update(chunk)
                if on_progress and (last_report is None or now - last_report >= PROGRESS_INTERVAL_SEC):
                    last_report = now
                    on_progress(done, total)
            f.flush()
            os.fsync(f.fileno())
        if on_progress:
            on_progress(done, total if total is not None else done)
        if done < MIN_ASSET_SIZE:
            raise RuntimeError(f"ダウンロードサイズ異常: {done} bytes")
        if on_phase:
            on_phase("整合性を検証しています")
        actual = digest.hexdigest()
        if actual != expected:
            raise UpdateVerifyError(
                f"SHA256 不一致: {asset_name} (expected={expected[:16]}…, actual={actual[:16]}…)")
        logger.info("SHA256 検証 OK: %s (%s…)", asset_name, actual[:16])
        os.replace(part, dest)
    except BaseException:
        try:
            part.unlink()
        except OSError:
            pass
        raise
    return done


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
