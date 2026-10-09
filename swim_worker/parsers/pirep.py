"""PIREPパーサー

DB 依存なし (Worker と同じ内容)。DB への保存は coordinator/store.py。
"""
import json
import logging

from . import diagnostics
from .common import parse_compact_utc

logger = logging.getLogger(__name__)

_JOB_TYPE = "collect_pireps"
_TURBULENCE_KEY_CANDIDATES = ["turbulenceList", "turbulencePirepList", "pirepList"]
_IGNORED_KEYS = {
    "msgHeader", "ctrlInfo", "ctrlHeader", "xmlBody", "errorInfoDTO",
    "shapeDisplayList",  # 地図表示用シンボル定義 (PIREPデータではない)
}

_parse_dt = parse_compact_utc


_KNOWN_KEYS = {"turbulenceList", "turbulencePirepList", "pirepList", "airepSpecialList"}


def parse(raw_data: dict) -> list[dict]:
    # 一時診断 (2026-07-29 追加、調査後に削除予定): 未パースの生カテゴリ検出
    diagnostics.check_unknown_keys(_JOB_TYPE, raw_data, _KNOWN_KEYS, _IGNORED_KEYS)
    diagnostics.check_key_collision(_JOB_TYPE, raw_data, _TURBULENCE_KEY_CANDIDATES)

    records = []
    # Turbulence PIREPs
    for item in (raw_data.get("turbulenceList") or raw_data.get("turbulencePirepList") or raw_data.get("pirepList") or []):
        info = item.get("imdbTurbulenceInformation") or {}
        positions = item.get("imdbTurbulenceInformationPosition") or []
        cn = info.get("pirepControlNumber")
        if not cn:
            continue
        pos = positions[0] if positions else {}
        records.append({
            "control_number": cn,
            "body": info.get("bodyData") or json.dumps(info, ensure_ascii=False, default=str),
            "turbulence_strength": info.get("turbulenceStrength"),
            "icing_strength": info.get("icingStrength"),
            "latitude": pos.get("latitude"),
            "longitude": pos.get("longitude"),
            "altitude": pos.get("observationAltitude"),
            "altitude_indicator": pos.get("observationAltitudeIndicator"),
            "observed_at": _parse_dt(info.get("timeOfObservation")),
            "effective_end": _parse_dt(info.get("effectiveEndTime")),
            "raw_data": item,
        })
    # Special PIREPs (airepSpecial)
    for item in (raw_data.get("airepSpecialList") or []):
        info = item.get("imdbAirepSpecial") or {}
        positions = item.get("imdbAirepSpecialPosition") or []
        cn = info.get("pirepControlNumber")
        if not cn:
            continue
        pos = positions[0] if positions else {}
        records.append({
            "control_number": cn,
            "body": info.get("bodyDataInformation") or json.dumps(info, ensure_ascii=False, default=str),
            "turbulence_strength": info.get("turbulenceType"),
            "icing_strength": None,
            "latitude": pos.get("latitude"),
            "longitude": pos.get("longitude"),
            "altitude": pos.get("observationAltitude"),
            "altitude_indicator": pos.get("observationAltitudeIndicator"),
            "observed_at": _parse_dt(info.get("timeOfObservation")),
            "effective_end": _parse_dt(info.get("effectiveEndTime")),
            "raw_data": item,
        })
    return records
