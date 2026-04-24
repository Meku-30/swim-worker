"""SWIM レスポンスパーサー (Coordinator の coordinator/parsers/ と同期)

Worker が SWIM API レスポンスをパースして構造化リストに変換する。
Coordinator は受け取ったパース済みデータを直接 store するだけ
(result["format"] == "parsed" の場合)。

各 parser ファイルは Coordinator のものと完全一致している必要がある
(scripts/sync_parsers.sh でコピー、scripts/check_parsers_synced.sh で検証)。
parse() 系関数は DB 依存なし、store() 系関数は Worker からは呼ばない
(関数内 import なので Worker 環境に sqlalchemy/models がなくても問題なし)。

job 別パース有効化は Redis set `swim:parse_enabled` で動的制御。
Coordinator の swim-admin parse-enable/disable で切替。
"""
import time

from . import airport, airspace, flight, notam, pirep, weather

# job_type → parser callable
# Coordinator の coordinator/result_handler.py:24-34 と必ず一致させる。
# ただし実際にパースするかは Redis の whitelist で決まる (supports() 参照)。
_PARSERS = {
    "collect_notams": notam.parse,
    "collect_pireps": pirep.parse,
    "collect_pkg_weather": weather.parse_pkg,
    "collect_airspace_data": airspace.parse,
    "collect_airport_profiles": airport.parse_detail,
    "collect_flight_foids": flight.parse_foids,
    "collect_flight_details": flight.parse_details,
    "collect_aip": airport.parse_aip,
    "collect_airports": airport.parse_list,
}

# Redis whitelist キー (Coordinator が管理)
PARSE_ENABLED_KEY = "swim:parse_enabled"

# Worker 内キャッシュ: (ttl_until, set[str])
# Redis SISMEMBER を execute_task 毎に叩くと帯域を食うため、60 秒キャッシュする
_cache_expires_at: float = 0.0
_cache_enabled: set[str] = set()
_CACHE_TTL = 60.0


async def _refresh_cache(redis_client) -> None:
    """Redis から whitelist を取得してキャッシュに反映"""
    global _cache_expires_at, _cache_enabled
    try:
        members = await redis_client.smembers(PARSE_ENABLED_KEY)
        _cache_enabled = {m.decode() if isinstance(m, bytes) else m for m in (members or set())}
    except Exception:
        # Redis エラー時は前回値を維持 (失敗時は安全側に倒れる)
        pass
    _cache_expires_at = time.monotonic() + _CACHE_TTL


async def supports(job_type: str, redis_client=None) -> bool:
    """この job_type を Worker 側でパースするか判定。

    redis_client=None の場合は常に False (raw 送信 = 現状維持、安全側)。
    Redis whitelist に入っている job_type のみ True。
    """
    if redis_client is None:
        return False
    if time.monotonic() >= _cache_expires_at:
        await _refresh_cache(redis_client)
    return job_type in _cache_enabled


def parse_for_job_type(job_type: str, data: dict) -> list[dict]:
    """job_type に対応する parser を呼んで結果を返す。

    SWIM API レスポンスの "ret" ラッパーがあれば剥がしてから parser に渡す
    (coordinator.result_handler._unwrap_ret と同じ挙動)。
    対応していない job_type で呼び出されると KeyError。
    """
    parser = _PARSERS[job_type]
    if isinstance(data, dict) and "ret" in data and isinstance(data["ret"], dict):
        data = data["ret"]
    return parser(data)
