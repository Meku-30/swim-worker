"""PKG気象パーサー (METAR/TAF/ATIS/RWY-INFO)

parse_pkg() / extract_* は DB 依存なし → Worker でも使える (フルパース移行用)。
store_pkg() のみ SQLAlchemy + models を関数内 import で使用する。
"""
import json
import logging
import re
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

_CLOSE_ATIS_PATTERN = re.compile(
    r"ATIS\s+\w{4}(?:\s+[A-Z])?\s*\n\s*CLOSE\s*$", re.DOTALL
)


def _parse_dt(s: str | None) -> str | None:
    """YYYYMMDDhhmm → ISO 8601 (UTC) 文字列 (JSON-safe)"""
    if not s or len(s) < 12:
        return None
    try:
        return datetime(int(s[:4]), int(s[4:6]), int(s[6:8]),
                        int(s[8:10]), int(s[10:12]), tzinfo=timezone.utc).isoformat()
    except (ValueError, IndexError):
        return None


_DT_FIELDS = ("observed_at", "issued_at")


def _coerce_dt(value):
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _ensure_utc(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


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
    records = []
    weather_dto = raw_data.get("weatherDTO") or {}

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


async def store_pkg(session_factory, records: list[dict]) -> int:
    if not records:
        return 0
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=2)

    # Load existing keys for dedup
    from sqlalchemy import select
    from coordinator.db.models import Atis, RunwayInfo, Weather
    # JSON 経由の str datetime を復元
    for r in records:
        for f in _DT_FIELDS:
            if f in r:
                r[f] = _coerce_dt(r.get(f))
    async with session_factory() as session:
        wx_rows = await session.execute(
            select(Weather.icao_code, Weather.type, Weather.observed_at).where(Weather.collected_at > cutoff))
        existing_wx = {(r[0], r[1], _ensure_utc(r[2])) for r in wx_rows.all()}
        atis_rows = await session.execute(
            select(Atis.icao_code, Atis.issued_at, Atis.atis_letter).where(Atis.collected_at > cutoff))
        existing_atis = {(r[0], _ensure_utc(r[1]), r[2]) for r in atis_rows.all()}
        rwy_rows = await session.execute(
            select(RunwayInfo.icao_code, RunwayInfo.observed_at).where(RunwayInfo.collected_at > cutoff))
        existing_rwy = {(r[0], _ensure_utc(r[1])) for r in rwy_rows.all()}

    count = 0
    async with session_factory() as session:
        for r in records:
            rt = r["_type"]
            fields = {k: v for k, v in r.items() if k != "_type"}
            if rt == "weather":
                key = (fields["icao_code"], fields["type"], fields["observed_at"])
                if key in existing_wx:
                    continue
                existing_wx.add(key)
                session.add(Weather(**fields, collected_at=now))
                count += 1
            elif rt == "atis":
                key = (fields["icao_code"], fields["issued_at"], fields["atis_letter"])
                if key in existing_atis:
                    continue
                existing_atis.add(key)
                session.add(Atis(**fields, collected_at=now))
                count += 1
            elif rt == "runway_info":
                key = (fields["icao_code"], fields["observed_at"])
                if key in existing_rwy:
                    continue
                existing_rwy.add(key)
                session.add(RunwayInfo(**fields, collected_at=now))
                count += 1
        await session.commit()
    return count
