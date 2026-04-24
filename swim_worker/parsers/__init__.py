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
# Coordinator の coordinator/parsers/__init__.py の _PARSERS と完全一致させる。
# 実際にパースするかは Redis の whitelist で決まる (supports() 参照)。
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
# Worker 単位で parse を無効化するための per-job set のプレフィックス
# 例: `swim:parse_disabled_workers:collect_pkg_weather` = {"hyuga", "hyuga-main"}
#     → 上記 2 Worker は global で parse ON でも個別に除外される
PARSE_DISABLED_WORKERS_PREFIX = "swim:parse_disabled_workers"

# Worker 内キャッシュ: (ttl_until, set[str])
# Redis SISMEMBER を execute_task 毎に叩くと帯域を食うため、60 秒キャッシュする
_cache_expires_at: float = 0.0
_cache_enabled: set[str] = set()  # global enabled minus per-worker disabled for this worker
_CACHE_TTL = 60.0


async def _refresh_cache(redis_client, worker_name: str | None = None) -> None:
    """Redis から whitelist + per-worker disable set を取得してキャッシュに反映。

    有効 job = (global enabled) - (この Worker が個別 disable された job)
    """
    global _cache_expires_at, _cache_enabled
    try:
        members = await redis_client.smembers(PARSE_ENABLED_KEY)
        enabled = {m.decode() if isinstance(m, bytes) else m for m in (members or set())}
        # Worker 個別除外の適用
        if worker_name:
            for jt in list(enabled):
                key = f"{PARSE_DISABLED_WORKERS_PREFIX}:{jt}"
                try:
                    is_disabled = await redis_client.sismember(key, worker_name)
                except Exception:
                    is_disabled = False
                if is_disabled:
                    enabled.discard(jt)
        _cache_enabled = enabled
    except Exception:
        # Redis エラー時は前回値を維持 (安全側)
        pass
    _cache_expires_at = time.monotonic() + _CACHE_TTL


async def supports(job_type: str, redis_client=None,
                   worker_name: str | None = None) -> bool:
    """この job_type を Worker 側でパースするか判定。

    redis_client=None の場合は常に False (raw 送信 = 現状維持、安全側)。
    worker_name を渡すと per-worker 除外 (swim:parse_disabled_workers:...) も適用。
    """
    if redis_client is None:
        return False
    if time.monotonic() >= _cache_expires_at:
        await _refresh_cache(redis_client, worker_name=worker_name)
    return job_type in _cache_enabled


def _extract_queried_airport(task_params: dict | None) -> str | None:
    """collect_flight_foids 用: task body から対象空港 ICAO を取り出す"""
    if not task_params:
        return None
    body = task_params.get("body") or {}
    conds = body.get("flightInformationSearchConditionsDTO") or {}
    return conds.get("airportCode")


def parse_for_job_type(job_type: str, data: dict,
                       task_params: dict | None = None) -> list[dict]:
    """job_type に対応する parser を呼んで結果を返す。

    SWIM API レスポンスの "ret" ラッパーがあれば剥がしてから parser に渡す
    (coordinator.result_handler._unwrap_ret と同じ挙動)。
    collect_flight_foids は queried_airport が必要なため task_params から抽出して渡す。
    対応していない job_type で呼び出されると KeyError。
    """
    parser = _PARSERS[job_type]
    if isinstance(data, dict) and "ret" in data and isinstance(data["ret"], dict):
        data = data["ret"]
    if job_type == "collect_flight_foids":
        return parser(data, _extract_queried_airport(task_params))
    return parser(data)
