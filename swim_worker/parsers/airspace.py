"""SIGMET/気象状態パーサー

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


async def store(session_factory, records: list[dict]) -> int:
    if not records:
        return 0
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=2)

    from sqlalchemy import select
    from coordinator.db.models import Weather
    async with session_factory() as session:
        existing_rows = await session.execute(
            select(Weather.icao_code, Weather.type, Weather.observed_at)
            .where(Weather.collected_at > cutoff)
        )
        existing = {
            (r[0], r[1], r[2].replace(tzinfo=timezone.utc) if r[2] and r[2].tzinfo is None else r[2])
            for r in existing_rows.all()
        }

    count = 0
    async with session_factory() as session:
        for r in records:
            key = (r["icao_code"], r["type"], r["observed_at"])
            if key in existing:
                continue
            existing.add(key)
            session.add(Weather(**r, collected_at=now))
            count += 1
        await session.commit()
    return count
