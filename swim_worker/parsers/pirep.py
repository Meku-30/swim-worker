"""PIREPパーサー

parse() は DB 依存なし → Worker でも使える (フルパース移行用)。
store() のみ SQLAlchemy + models を関数内 import で使用する。
"""
import json
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


def _parse_dt(s: str | None) -> datetime | None:
    if not s or len(s) < 12:
        return None
    try:
        return datetime(int(s[:4]), int(s[4:6]), int(s[6:8]), int(s[8:10]), int(s[10:12]), tzinfo=timezone.utc)
    except (ValueError, IndexError):
        return None


def parse(raw_data: dict) -> list[dict]:
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


async def store(session_factory, records: list[dict]) -> int:
    if not records:
        return 0
    from sqlalchemy import select
    from coordinator.db.models import Pirep
    cns = [r["control_number"] for r in records]
    async with session_factory() as session:
        existing = await session.execute(
            select(Pirep.control_number).where(Pirep.control_number.in_(cns))
        )
        existing_ids = {row[0] for row in existing.all()}
    new_records = [r for r in records if r["control_number"] not in existing_ids]
    if not new_records:
        return 0
    async with session_factory() as session:
        for r in new_records:
            session.add(Pirep(**{k: v for k, v in r.items()}, collected_at=datetime.now(timezone.utc)))
        await session.commit()
    return len(new_records)
