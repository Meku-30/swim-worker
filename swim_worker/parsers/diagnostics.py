"""パーサー診断ユーティリティ (調査用、恒久機能ではない)

SWIM の生レスポンスに、既知のパース対象キー以外の未知カテゴリが出現した場合や、
`or` チェーンで択一しているキーが複数同時に非空だった場合 (データロスの恐れ) を検出し、
環境変数 SWIM_PARSER_DIAG_DIR 配下の {job_type}_unknown_samples/ に永続保存する。

ログだけだとローテーションで消えるため、後から確認できるようファイル保存する。
DB 依存なし → Worker でも使える (他 parser の parse() から同じ要領で呼び出し可能)。

保存先は **明示オプトイン** (SWIM_PARSER_DIAG_DIR 未設定なら保存せず DEBUG ログのみ)。
Coordinator コンテナだけが設定する。このモジュールは Worker にも同一内容で配布されるが、
Worker では診断を収集しない (Windows GUI で C:\\app\\data が作られたり、systemd の
ProtectSystem=strict 下で保存失敗のスタックトレースが出ていた 2026-09-12 の修正)。
"""
import json
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_DIAG_DIR_ENV = "SWIM_PARSER_DIAG_DIR"


def _diag_base_dir() -> str | None:
    """診断サンプルの保存先。未設定 (Worker 等) なら None"""
    value = os.environ.get(_DIAG_DIR_ENV, "").strip()
    return value or None


def _log_level() -> int:
    """診断が有効なら INFO、無効 (Worker) なら DEBUG で出す (利用者の画面に出さない)"""
    return logging.INFO if _diag_base_dir() else logging.DEBUG


def _save_sample(job_type: str, tag: str, payload: dict) -> None:
    base = _diag_base_dir()
    if base is None:
        logger.debug("%s: 診断サンプル保存はスキップ (%s 未設定)", job_type, _DIAG_DIR_ENV)
        return
    directory = os.path.join(base, f"{job_type}_unknown_samples")
    try:
        os.makedirs(directory, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        path = os.path.join(directory, f"{tag}_{ts}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, default=str, indent=2)
        logger.info("%s: 診断サンプルを保存: %s", job_type, path)
    except OSError as e:
        # 診断は本処理に影響させない。スタックトレースは出さず 1 行で
        logger.warning("%s: 診断サンプルの保存に失敗 (%s): %s", job_type, directory, e)


def check_unknown_keys(job_type: str, raw_data: dict, known_keys: set[str],
                        ignored_keys: set[str] = frozenset()) -> None:
    """known_keys/ignored_keys 以外のキーに実データ (non-null/non-empty) があれば保存する"""
    if not isinstance(raw_data, dict):
        return
    unknown = {
        k: v for k, v in raw_data.items()
        if k not in known_keys and k not in ignored_keys and v not in (None, [], {})
    }
    if not unknown:
        return
    logger.log(_log_level(), "%s: 未知カテゴリに実データを検出: keys=%s", job_type, list(unknown.keys()))
    _save_sample(job_type, "unknown_keys", unknown)


def check_key_collision(job_type: str, raw_data: dict, candidate_keys: list[str]) -> None:
    """`a or b or c` のように択一しているキー群のうち、複数が同時に非空だった場合を検出する。
    最初に見つかったキーだけが使われる実装では、後続キーのデータが静かに失われるため。
    """
    if not isinstance(raw_data, dict):
        return
    populated = {k: raw_data.get(k) for k in candidate_keys if raw_data.get(k)}
    if len(populated) <= 1:
        return
    level = logging.WARNING if _diag_base_dir() else logging.DEBUG
    logger.log(level, "%s: 複数の既知キーが同時に非空 (orチェーンでデータロスの恐れ): keys=%s",
               job_type, list(populated.keys()))
    _save_sample(job_type, "key_collision", populated)
