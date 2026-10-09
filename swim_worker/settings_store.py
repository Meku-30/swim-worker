"""GUI の設定の読み書き (.env・パスワード・gui_settings.json)。tkinter 非依存。

- .env は GUI と CLI で共通の形式 (KEY=VALUE)。値は単引用符で囲んで書き
  (python-dotenv と同じ解釈: `\\` と `\'` だけをエスケープ)、手で足したキー・コメントは残す。
  書き換えは同じフォルダの一時ファイル (0600) に書いてから os.replace で置き換える
- パスワード (Redis・SWIM) は、Windows (資格情報マネージャー)・macOS (キーチェーン) では
  keyring に置き、.env には書かない。keyring が使えない・失敗したときは .env に書く
- 既存の .env にパスワードがあれば、keyring に移して読み戻せたら .env から消す
  (失敗したら .env に残す)
- GUI は Worker にパスワードをメモリで渡す (os.environ・.env を経由しない)。
  CLI (pydantic-settings) は今まで通り .env だけを読む
"""
import json
import logging
import os
import re
import sys
import tempfile
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

# 画面の欄 → .env のキー
FIELD_TO_ENV = {
    "redis_host": "REDIS_HOST",
    "redis_username": "REDIS_USERNAME",
    "redis_password": "REDIS_PASSWORD",
    "swim_username": "SWIM_USERNAME",
    "swim_password": "SWIM_PASSWORD",
    "worker_name": "WORKER_NAME",
}
ENV_TO_FIELD = {v: k for k, v in FIELD_TO_ENV.items()}
SECRET_FIELDS = ("redis_password", "swim_password")
REDIS_PORT = "6380"

KEYRING_SERVICE = "swim-worker"
# keyring のユーザー名の頭に付ける ID (.env に置く。秘密ではない)。フォルダごとに別の値に
# なるので、同じ PC の別フォルダの Worker とパスワードが混ざらない。.env と一緒に動く
SECRET_STORE_ID_KEY = "SWIM_WORKER_SECRET_ID"

_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def default_keyring():
    """Windows・macOS で使える keyring のバックエンド。使えなければ None (.env に保存)"""
    if sys.platform not in ("win32", "darwin"):
        return None
    try:
        import keyring
        from keyring.backends import fail
        backend = keyring.get_keyring()
    except Exception as e:
        logger.warning("keyring を使えないため、パスワードは .env に保存します: %s", e)
        return None
    if isinstance(backend, fail.Keyring) or getattr(backend, "priority", 0) <= 0:
        logger.warning("keyring のバックエンドが無いため、パスワードは .env に保存します")
        return None
    return backend


def quote_value(value: str) -> str:
    """.env に書く値 (単引用符。python-dotenv と同じく \\ と \' だけをエスケープ)"""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _unquote(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] == "'":
        return re.sub(r"\\([\\'])", r"\1", raw[1:-1])
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        return re.sub(r'\\([\\"])', r"\1", raw[1:-1]).replace("\\n", "\n")
    # 引用符なし: v1.1 までの GUI が書いた形。値はそのまま (# もコメント扱いしない)
    return raw


def read_env_values(path: Path) -> dict[str, str]:
    """.env を {KEY: 値} にする (1 行 1 キー。後に書いたものが勝つ)"""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _LINE_RE.match(line)
        if m:
            values[m.group(1)] = _unquote(m.group(2))
    return values


def _write_private(path: Path, text: str) -> None:
    """同じフォルダの一時ファイル (0600) に書いてから置き換える"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if os.name != "nt":
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def update_env_file(path: Path, updates: dict[str, str | None]) -> None:
    """.env の指定キーだけを書き換える (None は削除)。他の行・コメントはそのまま残す。

    updates の値は書く値そのもの (quote_value 済み)。無いキーは末尾に足す。
    """
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    out: list[str] = []
    done: set[str] = set()
    for line in lines:
        m = _LINE_RE.match(line) if not line.lstrip().startswith("#") else None
        key = m.group(1) if m else None
        if key in updates:
            if key not in done and updates[key] is not None:
                out.append(f"{key}={updates[key]}")
            done.add(key)  # 同じキーの 2 行目以降は消す
            continue
        out.append(line)
    for key, value in updates.items():
        if key not in done and value is not None:
            out.append(f"{key}={value}")
    _write_private(path, "\n".join(out) + "\n")


class SettingsStore:
    """GUI の設定欄の値を .env と keyring に読み書きする"""

    _AUTO = object()

    def __init__(self, env_path: Path, keyring=_AUTO):
        self.env_path = env_path
        self._keyring = default_keyring() if keyring is SettingsStore._AUTO else keyring

    @property
    def uses_keyring(self) -> bool:
        return self._keyring is not None

    def _keyring_user(self, store_id: str, env_key: str) -> str:
        return f"{store_id}:{env_key}"

    def _store_id(self, values: dict[str, str], updates: dict) -> str:
        store_id = values.get(SECRET_STORE_ID_KEY) or ""
        if not re.fullmatch(r"[0-9a-f]{32}", store_id):
            store_id = uuid.uuid4().hex
            updates[SECRET_STORE_ID_KEY] = quote_value(store_id)
        return store_id

    def _keyring_set(self, store_id: str, env_key: str, value: str) -> bool:
        """keyring に置いて読み戻しで確かめる。空なら消す。成功したら True"""
        user = self._keyring_user(store_id, env_key)
        try:
            if value == "":
                try:
                    self._keyring.delete_password(KEYRING_SERVICE, user)
                except Exception:
                    pass  # 元から無い
                return True
            self._keyring.set_password(KEYRING_SERVICE, user, value)
            return self._keyring.get_password(KEYRING_SERVICE, user) == value
        except Exception as e:
            logger.warning("keyring への保存に失敗 (%s): %s", env_key, type(e).__name__)
            return False

    def load(self) -> tuple[dict[str, str], bool | None]:
        """(欄の値, AUTO_CONNECT) を返す。.env に残っているパスワードは keyring へ移す"""
        values = read_env_values(self.env_path)
        fields = {field: values.get(env_key, "") for field, env_key in FIELD_TO_ENV.items()}
        auto = values.get("AUTO_CONNECT")
        auto_connect = None if auto is None else auto.strip().lower() == "true"
        if self._keyring is None:
            return fields, auto_connect

        updates: dict[str, str | None] = {}
        store_id = values.get(SECRET_STORE_ID_KEY) or ""
        if any(values.get(FIELD_TO_ENV[f]) for f in SECRET_FIELDS):
            store_id = self._store_id(values, updates)
        for field in SECRET_FIELDS:
            env_key = FIELD_TO_ENV[field]
            in_env = values.get(env_key)
            if in_env:
                # 移行: keyring に置いて読み戻せたら .env から消す
                if self._keyring_set(store_id, env_key, in_env):
                    updates[env_key] = None
                    logger.info("%s を .env から OS の資格情報ストアに移しました", env_key)
                fields[field] = in_env
                continue
            if not store_id:
                continue
            try:
                got = self._keyring.get_password(
                    KEYRING_SERVICE, self._keyring_user(store_id, env_key))
            except Exception as e:
                logger.warning("keyring から %s を読めません: %s", env_key, type(e).__name__)
                got = None
            if got is not None:
                fields[field] = got
        if any(v is None for v in updates.values()) and self.env_path.exists():
            try:
                update_env_file(self.env_path, updates)
            except Exception as e:
                logger.warning(".env の書き換えに失敗 (パスワードは .env に残ります): %s", e)
        return fields, auto_connect

    def save(self, fields: dict[str, str], auto_connect: bool) -> str:
        """欄の値を保存する。パスワードの置き場所 ("keyring" / "env") を返す"""
        updates: dict[str, str | None] = {}
        for field, env_key in FIELD_TO_ENV.items():
            if field in SECRET_FIELDS:
                continue
            updates[env_key] = quote_value(fields.get(field, "").strip())
        updates["REDIS_PORT"] = quote_value(REDIS_PORT)
        updates["AUTO_CONNECT"] = quote_value("true" if auto_connect else "false")

        where = "keyring" if self._keyring is not None else "env"
        store_id = ""
        if self._keyring is not None:
            store_id = self._store_id(read_env_values(self.env_path), updates)
        for field in SECRET_FIELDS:
            env_key = FIELD_TO_ENV[field]
            value = fields.get(field, "")  # パスワードは strip しない
            if self._keyring is not None and self._keyring_set(store_id, env_key, value):
                updates[env_key] = None
            else:
                if self._keyring is not None:
                    where = "env"
                updates[env_key] = quote_value(value)
        update_env_file(self.env_path, updates)
        return where

    def set_auto_connect(self, value: bool) -> bool:
        """.env の AUTO_CONNECT だけを書き換える。.env が無ければ何もしない (False)"""
        if not self.env_path.exists():
            return False
        update_env_file(self.env_path, {"AUTO_CONNECT": quote_value("true" if value else "false")})
        return True


def load_json(path: Path) -> dict:
    """JSON ファイル読み込み (存在しない/壊れていれば空 dict)"""
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.debug("設定ファイル読み込み失敗 %s: %s", path, e)
        return {}


def save_json(path: Path, data: dict) -> None:
    """JSON ファイル書き込み (親ディレクトリ作成込み・一時ファイルから置き換え)"""
    _write_private(path, json.dumps(data, ensure_ascii=False, indent=2))
