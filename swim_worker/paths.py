"""Worker が読み書きするファイルの置き場所 (CLI / GUI / Docker 共通)

置き場所の決め方はここだけに書く。各モジュールが個別に `sys.frozen` を見て
組み立てると、片方だけ変えたときに場所がずれる (ロック・Cookie・起動マーカーなど)。

- PyInstaller (frozen): 実行ファイルのあるディレクトリ。書き込むものは `data/` の下
  (install.sh の systemd unit は `data/` だけを書き込み可能にしている)
- それ以外 (開発・Docker・`python -m swim_worker`): カレントディレクトリ
  (Docker は WORKDIR=/app なので /app/data)

どの関数もディレクトリは作らない。書く側が作る。
"""
import sys
from pathlib import Path

DATA_DIR_NAME = "data"
STARTUP_MARKER_NAME = ".startup_ok"
LAST_INSTANCE_TOKEN_NAME = ".last_instance_token"
COOKIE_FILE_NAME = ".swim_cookies.json"
LOCK_FILE_NAME = "swim-worker.lock"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def base_dir() -> Path:
    """実行ファイルのあるディレクトリ (frozen) またはカレントディレクトリ"""
    if is_frozen():
        return Path(sys.executable).parent
    return Path.cwd()


def data_dir() -> Path:
    return base_dir() / DATA_DIR_NAME


def startup_marker_path() -> Path:
    """起動成功マーカー。更新後のロールバック判定 (GUI のヘルパー・install.sh --auto) が見る"""
    return data_dir() / STARTUP_MARKER_NAME


def last_instance_token_path() -> Path:
    """前回プロセスの instance_token (自分の前世代が残した heartbeat の lock を回収するため)"""
    return data_dir() / LAST_INSTANCE_TOKEN_NAME


def cookie_file_path(override: str = "") -> Path:
    """SWIM の Cookie の保存先。override (COOKIE_FILE) があればそれ"""
    if override:
        return Path(override)
    return data_dir() / COOKIE_FILE_NAME


def lock_path() -> Path:
    """同一マシンでの多重起動を防ぐロックファイル。

    frozen は常に `data/` の下 (systemd の ProtectSystem=strict では `data/` しか書けない。
    初回と 2 回目で場所が変わると多重起動を防げないので、`data/` の有無で分岐しない)。
    それ以外は従来どおりカレントディレクトリ直下。
    """
    if is_frozen():
        return data_dir() / LOCK_FILE_NAME
    return Path.cwd() / LOCK_FILE_NAME
