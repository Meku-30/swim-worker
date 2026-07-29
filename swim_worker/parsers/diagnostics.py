"""パーサー診断ユーティリティ (調査用、恒久機能ではない)

SWIM の生レスポンスに、既知のパース対象キー以外の未知カテゴリが出現した場合や、
`or` チェーンで択一しているキーが複数同時に非空だった場合 (データロスの恐れ) を検出し、
/app/data/{job_type}_unknown_samples/ に永続保存する。

ログだけだとローテーションで消えるため、後から確認できるようファイル保存する。
DB 依存なし → Worker でも使える (他 parser の parse() から同じ要領で呼び出し可能)。
"""
import json
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def _save_sample(job_type: str, tag: str, payload: dict) -> None:
    directory = f"/app/data/{job_type}_unknown_samples"
    try:
        os.makedirs(directory, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        path = os.path.join(directory, f"{tag}_{ts}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, default=str, indent=2)
        logger.info("%s: 診断サンプルを保存: %s", job_type, path)
    except OSError:
        logger.exception("%s: 診断サンプルの保存に失敗", job_type)


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
    logger.info("%s: 未知カテゴリに実データを検出: keys=%s", job_type, list(unknown.keys()))
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
    logger.warning("%s: 複数の既知キーが同時に非空 (orチェーンでデータロスの恐れ): keys=%s",
                    job_type, list(populated.keys()))
    _save_sample(job_type, "key_collision", populated)
