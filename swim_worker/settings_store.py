"""GUI の設定の読み書き (.env と gui_settings.json)。tkinter 非依存。

.env は GUI と CLI で共通の形式 (KEY=VALUE)。GUI はここで読み書きし、Worker には
値をメモリで渡す (os.environ には書かない)。
"""
import json
import logging
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
REDIS_PORT = "6380"


def load_env(path: Path) -> tuple[dict[str, str], bool | None]:
    """.env を読み、(欄の値, AUTO_CONNECT) を返す。ファイルが無ければ ({}, None)"""
    fields: dict[str, str] = {}
    auto_connect: bool | None = None
    if not path.exists():
        return fields, auto_connect
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key == "AUTO_CONNECT":
            auto_connect = value.lower() == "true"
            continue
        field = ENV_TO_FIELD.get(key)
        if field:
            fields[field] = value
    return fields, auto_connect


def save_env(path: Path, fields: dict[str, str], auto_connect: bool) -> None:
    """欄の値と AUTO_CONNECT を .env に書く"""
    lines = []
    for field, env_key in FIELD_TO_ENV.items():
        value = fields.get(field, "").strip()
        lines.append(f"{env_key}={value}")
    lines.append(f"REDIS_PORT={REDIS_PORT}")
    lines.append(f"AUTO_CONNECT={'true' if auto_connect else 'false'}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def set_auto_connect(path: Path, value: bool) -> bool:
    """.env の AUTO_CONNECT だけを書き換える。.env が無ければ何もしない (False)"""
    if not path.exists():
        return False
    lines = path.read_text(encoding="utf-8").splitlines()
    new_lines = [l for l in lines if not l.strip().startswith("AUTO_CONNECT=")]
    new_lines.append(f"AUTO_CONNECT={'true' if value else 'false'}")
    path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
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
    """JSON ファイル書き込み (親ディレクトリ作成込み)"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
