"""SIGMET/気象状態パーサー

DB 依存なし (Worker と同じ内容)。DB への保存は coordinator/store.py。
"""
import json
import logging

from . import diagnostics
from .common import parse_compact_utc

logger = logging.getLogger(__name__)

_JOB_TYPE = "collect_airspace_data"
_KNOWN_KEYS = {"airportWeatherConditionResult", "sigmetList"}

_parse_dt = parse_compact_utc


def parse(raw_data: dict) -> list[dict]:
    diagnostics.check_unknown_keys(_JOB_TYPE, raw_data, _KNOWN_KEYS)
    records = []
    # Weather conditions
    for cond in (raw_data.get("airportWeatherConditionResult") or []):
        records.append({"icao_code": cond.get("aerodromeCode"), "type": "CONDITION",
            "raw_text": json.dumps(cond, ensure_ascii=False, default=str),
            "observed_at": _parse_dt(cond.get("timeOfObservation"))})
    # SIGMETs
    for sig in (raw_data.get("sigmetList") or []):
        info = sig.get("imdbSigmet") or {}
        records.append({"icao_code": info.get("location"), "type": "SIGMET",
            "raw_text": info.get("bodyDataInformation") or json.dumps(info, ensure_ascii=False, default=str),
            "observed_at": _parse_dt(info.get("timeOfObservation"))})
    return records
