"""調査用スクリプトの共通部品 (資格情報・一時 Cookie・出力ファイル)

- パスワードはコマンドラインで受け取らない (ps・シェルの履歴に残るため)。
  環境変数 SWIM_PASSWORD か、無ければ getpass で聞く
- Cookie は一時ディレクトリ (0700) に置き、終わったら消す (Worker の data/ を触らない)
- 出力ファイルは 0600 で作る (セッション Cookie を含むことがある)
"""
import contextlib
import getpass
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def add_repo_to_path() -> None:
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))


def swim_credentials(user: str = "") -> tuple[str, str]:
    """(SWIM ID, パスワード)。ID は引数か SWIM_USERNAME、パスワードは SWIM_PASSWORD か入力"""
    user = user or os.environ.get("SWIM_USERNAME", "")
    if not user:
        user = input("SWIM ID: ").strip()
    secret = os.environ.get("SWIM_PASSWORD", "")
    if not secret:
        secret = getpass.getpass("SWIM パスワード: ")
    if not user or not secret:
        sys.exit("SWIM ID とパスワードが必要です")
    return user, secret


@contextlib.contextmanager
def temp_cookie_file():
    """一時ディレクトリの Cookie ファイルのパス (終わったらディレクトリごと消す)"""
    d = tempfile.mkdtemp(prefix="swim-dev-")
    try:
        yield os.path.join(d, "cookies.json")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def write_private_json(path: str | os.PathLike, data) -> None:
    """0600 で JSON を書く (既にあれば 0600 にしてから上書き)"""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        if os.name != "nt":
            os.fchmod(f.fileno(), 0o600)
        json.dump(data, f, indent=2, ensure_ascii=False)
