"""重複起動防止

2段階で守る:

1. **OSファイルロック** (LocalInstanceLock)
   - 同一マシン上での多重起動を確実に防ぐ。プロセスが死ねば OS が自動解放。
   - Windows は msvcrt.locking、それ以外は fcntl.flock を使用。

2. **Redis SET NX** (consumer.py 側で heartbeat キーに対して実施)
   - 別マシンで同じ worker_name を使ってしまった場合を検知する。
   - こちらは consumer.py 側に実装。
"""
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


class AlreadyRunning(RuntimeError):
    """別の swim-worker プロセスが既に起動していることを示す例外"""
    pass


def get_lock_path() -> Path:
    """ロックファイルの配置場所。

    install.sh 経由で systemd unit (ProtectSystem=strict) 配下で動く場合、
    /opt/swim-worker/ は読み取り専用で /opt/swim-worker/data/ のみ書き込み可能。
    そのため、`data/` サブディレクトリが存在すれば優先してそこに置く。

    優先順:
      1. PyInstaller frozen + exe 隣に data/ がある (install.sh 配置)
         → {exe.parent}/data/swim-worker.lock
      2. PyInstaller frozen で data/ がない (exe 単体配置)
         → {exe.parent}/swim-worker.lock
      3. それ以外 (CLI/Docker/開発)
         → {cwd}/swim-worker.lock
    """
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent
        data_dir = base / "data"
        if data_dir.is_dir():
            return data_dir / "swim-worker.lock"
        return base / "swim-worker.lock"
    return Path.cwd() / "swim-worker.lock"


class LocalInstanceLock:
    """OS ファイルロックで同一マシン上の重複起動を防ぐ。

    使い方:
        lock = LocalInstanceLock()
        lock.acquire()  # AlreadyRunning が送出される可能性
        try:
            ...main work...
        finally:
            lock.release()
    """
    def __init__(self, lock_path: Path | None = None) -> None:
        self._lock_path = lock_path or get_lock_path()
        self._fd: int | None = None

    @property
    def path(self) -> Path:
        return self._lock_path

    def acquire(self) -> None:
        """ロックを取得する。既に他プロセスが保持していれば AlreadyRunning。"""
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            self._acquire_windows()
        else:
            self._acquire_unix()
        # PID を記録 (デバッグ用、ロックは OS が管理するので中身は目安)
        try:
            if sys.platform != "win32":
                os.ftruncate(self._fd, 0)
            os.lseek(self._fd, 0, os.SEEK_SET)
            os.write(self._fd, f"{os.getpid()}\n".encode())
        except OSError:
            pass

    def _acquire_unix(self) -> None:
        import fcntl
        fd = os.open(str(self._lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise AlreadyRunning(
                f"別の swim-worker プロセスが既に起動しています (lock: {self._lock_path})"
            )
        self._fd = fd

    def _acquire_windows(self) -> None:
        import msvcrt
        fd = os.open(str(self._lock_path), os.O_CREAT | os.O_RDWR)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            os.close(fd)
            raise AlreadyRunning(
                f"別の swim-worker プロセスが既に起動しています (lock: {self._lock_path})"
            )
        self._fd = fd

    def release(self) -> None:
        """ロックを解放する。プロセス終了時に OS が自動解放もするので、
        必須ではないが行儀良く呼ぶのが望ましい。"""
        if self._fd is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt
                try:
                    os.lseek(self._fd, 0, os.SEEK_SET)
                    msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl
                try:
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            try:
                os.close(self._fd)
            except OSError:
                pass
        finally:
            self._fd = None
