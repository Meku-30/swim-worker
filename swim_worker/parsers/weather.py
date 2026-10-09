"""PKG気象パーサー (METAR/TAF/ATIS/RWY-INFO)

DB 依存なし (Worker と同じ内容)。DB への保存は coordinator/store.py。
"""
import json
import logging
import re
from datetime import datetime

from . import diagnostics
from .common import parse_compact_utc

logger = logging.getLogger(__name__)

_JOB_TYPE = "collect_pkg_weather"
_KNOWN_TOP_KEYS = {"weatherDTO"}
_KNOWN_DTO_KEYS = {"metarSpeciInfoList", "tafInfoList", "atisInfoList", "useRunwayList"}
_IGNORED_TOP_KEYS = {
    "msgHeader", "ctrlInfo", "ctrlHeader", "errorInfoDTO",  # プロトコル用メタデータ
    "shapeDisplayList",  # 地図表示用シンボル定義 (気象データではない)
    # 気象画面の地図に NOTAM 範囲を重ね描きするための NOTAM リスト。座標・半径・高度・本文は
    # collect_notams (USV001) の raw_data に全件入っており重複。固有なのは drawInfo (変換済み座標)
    # と lineDataList (面 NOTAM の多角形) のみで用途なし → 無視 (2026-09-12 決定、6,135 サンプル調査)
    "notamDTOList",
}

_CLOSE_ATIS_PATTERN = re.compile(
    r"ATIS\s+\w{4}(?:\s+[A-Z])?\s*\n\s*CLOSE\s*$", re.DOTALL
)

_parse_dt = parse_compact_utc


def _extract_atis_letter(body: str) -> str | None:
    lines = body.strip().split("\n")
    if len(lines) >= 2:
        second = lines[1].strip()
        if second and second[0].isalpha() and len(second) == 1:
            return second
        if second.startswith("M ") or second.startswith("MS "):
            parts = second.split()
            for p in parts:
                if len(p) == 1 and p.isalpha():
                    return p
    match = re.search(r"ATIS\s+\w{4}\s+([A-Z])\s", body)
    if match:
        return match.group(1)
    return None


def _parse_rwy_info(plain_data: str) -> tuple[list[str], str | None, str | None, str | None]:
    approach_types = []
    lines = plain_data.split("\n")
    in_apch = False
    for line in lines:
        if "(APCH)" in line:
            m = re.search(r"\(APCH\)\s+(.+)", line)
            if m:
                approach_types.append(m.group(1).strip())
                in_apch = True
        elif in_apch:
            stripped = line.strip()
            if not stripped or re.match(r"(LDG|DEP|USING)\s+RWY", stripped):
                in_apch = False
            else:
                approach_types.append(stripped)
        else:
            in_apch = False
    ldg_match = re.search(r"LDG\s+RWY\s+(.+?)(?:\n|$)", plain_data)
    ldg_rwy = ldg_match.group(1).strip() if ldg_match else None
    dep_match = re.search(r"DEP\s+RWY\s+(.+?)(?:\n|$)", plain_data)
    dep_rwy = dep_match.group(1).strip() if dep_match else None
    using_match = re.search(r"USING\s+RWY\s+(.+?)(?:\n|$)", plain_data)
    runway_in_use = using_match.group(1).strip() if using_match else ldg_rwy
    return approach_types, runway_in_use, ldg_rwy, dep_rwy


def parse_pkg(raw_data: dict) -> list[dict]:
    """PKG気象レスポンスからweather/atis/runway_infoレコードを生成"""
    diagnostics.check_unknown_keys(_JOB_TYPE, raw_data, _KNOWN_TOP_KEYS, _IGNORED_TOP_KEYS)
    records = []
    weather_dto = raw_data.get("weatherDTO") or {}
    diagnostics.check_unknown_keys(_JOB_TYPE, weather_dto, _KNOWN_DTO_KEYS)

    # METAR/SPECI
    for item in (weather_dto.get("metarSpeciInfoList") or []):
        body = (item.get("body_DATA") or "").strip()
        if not body:
            continue
        wx_type = "SPECI" if body.startswith("SPECI") else "METAR"
        records.append({"_type": "weather", "icao_code": item.get("location"), "type": wx_type,
            "raw_text": body, "observed_at": _parse_dt(item.get("observed_DATE"))})

    # TAF
    for item in (weather_dto.get("tafInfoList") or []):
        body = (item.get("body_DATA") or "").strip()
        if not body:
            continue
        records.append({"_type": "weather", "icao_code": item.get("location"), "type": "TAF",
            "raw_text": body, "observed_at": _parse_dt(item.get("observed_DATE"))})

    # ATIS
    for item in (weather_dto.get("atisInfoList") or []):
        body = (item.get("body_DATA") or "").strip()
        if not body:
            continue
        records.append({"_type": "atis", "icao_code": item.get("location"),
            "atis_letter": _extract_atis_letter(body), "content": body,
            "issued_at": _parse_dt(item.get("observed_DATE"))})

    # RWY-INFO
    for item in (weather_dto.get("useRunwayList") or []):
        plain = (item.get("plain_DATA") or "").strip()
        if not plain:
            continue
        apch, rwy_use, ldg, dep = _parse_rwy_info(plain)
        records.append({"_type": "runway_info", "icao_code": item.get("location"),
            "runway_number": item.get("runway_NUMBER"), "approach_types": json.dumps(apch),
            "runway_in_use": rwy_use, "ldg_rwy": ldg, "dep_rwy": dep,
            "plain_data": plain, "observed_at": _parse_dt(item.get("observed_DATE"))})

    return records


def extract_atis_status(parsed_records: list[dict]) -> dict:
    """parse_pkg()の出力からATIS状態情報を抽出する。

    Returns:
        {"atis_icaos": set, "atis_close_icaos": set,
         "atis_routine_icaos": set, "atis_issued_at": dict}
    """
    atis_icaos: set[str] = set()
    atis_close_icaos: set[str] = set()
    atis_routine_icaos: set[str] = set()
    atis_issued_at: dict[str, datetime] = {}

    for r in parsed_records:
        if r.get("_type") != "atis":
            continue
        icao = r.get("icao_code", "")
        content = r.get("content", "")
        issued_at = r.get("issued_at")

        if _CLOSE_ATIS_PATTERN.search(content):
            atis_close_icaos.add(icao)
            continue

        atis_icaos.add(icao)
        if issued_at:
            atis_issued_at[icao] = issued_at

        # routine判定: 2行目が M or MS で始まるか
        lines = content.strip().split("\n")
        if len(lines) >= 2:
            second = lines[1].strip()
            if second.startswith("M"):
                atis_routine_icaos.add(icao)

    return {
        "atis_icaos": atis_icaos,
        "atis_close_icaos": atis_close_icaos,
        "atis_routine_icaos": atis_routine_icaos,
        "atis_issued_at": atis_issued_at,
    }


def extract_routine_metar_airports(parsed_records: list[dict]) -> set[str]:
    """parse_pkg()の出力から通常METAR（非SPECI）が存在する空港コードを返す。

    SPECIモードの解除判定に使用: SWIMデータで通常METARが出ていれば
    当該空港のSPECI状態は解消されたとみなす。
    """
    return {
        r["icao_code"] for r in parsed_records
        if r.get("_type") == "weather" and r.get("type") == "METAR" and r.get("icao_code")
    }
